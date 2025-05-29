from fastapi import FastAPI, HTTPException, Path, status, Depends, Body, Query
from pydantic import BaseModel, Field, validator
import redis
import redis.asyncio as aioredis # Для асинхронної роботи з Redis
import json 
from typing import List, Dict, Any, Optional
import os
import httpx 
import datetime
import logging
from prometheus_fastapi_instrumentator import Instrumentator
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError, retry_if_exception_type
import pybreaker

# --- Налаштування логування ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(), 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Моделі Pydantic ---
class CartItemBase(BaseModel):
    product_sku: str = Field(..., description="SKU товару", example="SKU-SUPERX-12PRO-BLK")
    quantity: int = Field(..., gt=0, le=100, description="Кількість товару (від 1 до 100)", example=2)

class CartItemCreate(CartItemBase):
    pass

class CartItemUpdate(BaseModel):
    quantity: int = Field(..., gt=0, le=100, description="Нова кількість товару (від 1 до 100)", example=3)

class CartItemResponse(CartItemBase):
    name: Optional[str] = Field(None, description="Назва товару (з каталогу)", example="Смартфон SuperX 12 Pro")
    price_per_item: Optional[float] = Field(None, description="Ціна за одиницю товару (з каталогу)", example=34999.99)
    item_total_price: Optional[float] = Field(None, description="Загальна вартість цієї позиції (кількість * ціна)", example=69999.98)

class CartResponse(BaseModel):
    user_id: str = Field(..., description="Ідентифікатор користувача", example="user123")
    items: List[CartItemResponse] = Field(default_factory=list, description="Список товарів у кошику")
    total_items_count: int = Field(0, description="Загальна кількість товарів у кошику (сума quantity)", example=5)
    grand_total_price: float = Field(0.0, description="Загальна вартість всіх товарів у кошику", example=75499.90)
    last_updated_at: Optional[datetime.datetime] = Field(None, description="Час останнього оновлення кошика")

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user_abc_123",
                "items": [
                    {
                        "product_sku": "SKU001", "quantity": 2, "name": "Товар 1", 
                        "price_per_item": 100.00, "item_total_price": 200.00
                    },
                    {
                        "product_sku": "SKU002", "quantity": 1, "name": "Товар 2", 
                        "price_per_item": 50.50, "item_total_price": 50.50
                    }
                ],
                "total_items_count": 3,
                "grand_total_price": 250.50,
                "last_updated_at": "2024-05-30T14:30:00Z"
            }
        }
    }

# --- Глобальні змінні та конфігурація ---
app = FastAPI(
    title="Сервіс Кошика Покупця",
    description="API для управління кошиками користувачів в інтернет-магазині.",
    version="1.2.0"
)

Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# Конфігурація з змінних середовища
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB_CART", "0")) # Окремий DB для кошиків
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") # Пароль для Redis (якщо є)
REDIS_CART_TTL_SECONDS = int(os.getenv("REDIS_CART_TTL_SECONDS", "2592000")) # 30 днів TTL для кошика

PRODUCT_CATALOG_SERVICE_URL = os.getenv("PRODUCT_CATALOG_SERVICE_URL", "http://product-catalog-service:8000")
LOGGING_SERVICE_URL = os.getenv("LOGGING_SERVICE_URL")
SERVICE_NAME = "CartService"
HTTP_CLIENT_TIMEOUT = float(os.getenv("HTTP_CLIENT_TIMEOUT", "3.0")) # Коротший таймаут для залежностей

# Redis клієнт (буде ініціалізований асинхронно)
redis_pool: Optional[aioredis.Redis] = None

# HTTP клієнт для взаємодії з іншими сервісами
http_client: Optional[httpx.AsyncClient] = None

# Circuit Breakers
log_service_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name=f"{SERVICE_NAME}_LoggingServiceBreaker")
product_catalog_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=45, name=f"{SERVICE_NAME}_ProductCatalogServiceBreaker")
redis_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name=f"{SERVICE_NAME}_RedisBreaker") # CB для Redis

# --- Допоміжні функції ---

@log_service_breaker
@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=0.5, max=3),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
async def _log_to_service_async(level: str, message: str, details: Optional[Dict[str, Any]] = None):
    if not LOGGING_SERVICE_URL or not http_client:
        logger.warning(f"Резервне логування (сервіс логування/клієнт недоступний): Level: {level}, Message: {message}, Details: {details}")
        return
    log_payload = {
        "service_name": SERVICE_NAME, "level": level.upper(), "message": message,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z", "details": details or {}
    }
    try:
        response = await http_client.post(f"{LOGGING_SERVICE_URL}/logs", json=log_payload)
        response.raise_for_status()
    except Exception as e: # Ловимо всі помилки тут, щоб не переривати основний потік через помилку логування
        logger.error(f"Не вдалося надіслати лог до сервісу: {str(e)}. Оригінальний лог: {log_payload}")
        # Не перевикидаємо помилку, щоб основна операція могла продовжитися, якщо логування не критичне
        # Але Circuit Breaker все одно спрацює на цю функцію

async def get_redis_connection() -> aioredis.Redis:
    """Забезпечує отримання активного з'єднання з Redis."""
    if not redis_pool:
        # Цей випадок не мав би траплятися після успішного startup
        await _log_to_service_async("CRITICAL", "Спроба отримати з'єднання Redis, але пул не ініціалізовано.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс Redis тимчасово недоступний (пул не створено).")
    return redis_pool

# --- Обробники подій FastAPI ---
@app.on_event("startup")
async def startup_event_async():
    global redis_pool, http_client
    logger.info(f"Запуск сервісу '{SERVICE_NAME}'...")
    http_client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)

    try:
        # Ініціалізація пулу з'єднань Redis з retry логікою
        @retry(
            stop=stop_after_attempt(5), 
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(redis.exceptions.ConnectionError),
            reraise=True
        )
        async def _connect_to_redis():
            logger.info(f"Спроба підключення до Redis: {REDIS_HOST}:{REDIS_PORT}, DB: {REDIS_DB}")
            # aioredis.from_url є рекомендованим способом створення клієнта/пулу
            redis_url = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
            if REDIS_PASSWORD:
                redis_url = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
            
            redis_pool = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True) # decode_responses=True для рядків
            await redis_pool.ping() # Перевірка з'єднання
            logger.info("Успішно підключено до Redis та створено пул з'єднань.")

        await _connect_to_redis()
        await _log_to_service_async("INFO", "Сервіс кошика запущено, успішно підключено до Redis.")
    except RetryError as e:
        logger.critical(f"Критична помилка: не вдалося підключитися до Redis після кількох спроб: {e.last_attempt.exception()}. Сервіс не зможе працювати з кошиками.")
        await _log_to_service_async("CRITICAL", f"Не вдалося підключитися до Redis при старті: {str(e.last_attempt.exception())}")
        redis_pool = None # Явно вказуємо, що пул не створено
    except Exception as e:
        logger.critical(f"Неочікувана помилка при підключенні до Redis: {str(e)}", exc_info=True)
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка Redis при старті: {str(e)}")
        redis_pool = None

@app.on_event("shutdown")
async def shutdown_event_async():
    if http_client:
        await http_client.aclose()
        logger.info("HTTP клієнт закрито.")
    if redis_pool:
        await redis_pool.close() # Закриваємо пул з'єднань aioredis
        logger.info("Пул з'єднань Redis закрито.")
    
    try:
        await _log_to_service_async("INFO", f"Сервіс '{SERVICE_NAME}' зупинено.")
    except Exception as e: # Ловимо будь-які помилки при фінальному логуванні
        logger.warning(f"Не вдалося надіслати фінальний лог при зупинці: {str(e)}")


# --- Функції взаємодії з іншими сервісами ---
@product_catalog_breaker
@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=0.2, max=2), # Коротші затримки для швидкого отримання деталей
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
async def get_product_details_async(product_sku: str) -> Optional[Dict[str, Any]]:
    if not PRODUCT_CATALOG_SERVICE_URL or not http_client:
        await _log_to_service_async("ERROR", "PRODUCT_CATALOG_SERVICE_URL не налаштовано або HTTP клієнт не ініціалізовано.", {"product_sku": product_sku})
        return None
    
    request_url = f"{PRODUCT_CATALOG_SERVICE_URL}/products/{product_sku}"
    log_details = {"product_sku": product_sku, "request_url": request_url}
    await _log_to_service_async("DEBUG", f"Запит деталей товару {product_sku} з каталогу.", log_details)
    
    try:
        response = await http_client.get(request_url)
        if response.status_code == status.HTTP_404_NOT_FOUND:
            await _log_to_service_async("WARNING", f"Товар {product_sku} не знайдено в каталозі.", {**log_details, "status_code": 404})
            return None
        response.raise_for_status()
        product_data = response.json()
        await _log_to_service_async("DEBUG", f"Отримано деталі товару {product_sku}.", {**log_details, "response_status": response.status_code})
        return product_data
    except httpx.HTTPStatusError as e:
        await _log_to_service_async("ERROR", f"HTTP помилка ({e.response.status_code}) при отриманні деталей товару {product_sku}: {e.response.text}", {**log_details, "error": str(e)})
        # Не перевикидаємо тут, щоб Circuit Breaker спрацював на рівні декоратора
        # Якщо CB відкритий, ця функція все одно не буде викликана.
        # Якщо це остання спроба retry, помилка буде перевикинута декоратором retry.
        raise # Важливо перевикинути для retry/CB
    except httpx.RequestError as e:
        await _log_to_service_async("ERROR", f"Мережева помилка при отриманні деталей товару {product_sku}: {str(e)}", {**log_details, "error": str(e)})
        raise
    except Exception as e: # Інші неочікувані помилки
        await _log_to_service_async("ERROR", f"Неочікувана помилка при отриманні деталей товару {product_sku}: {str(e)}", {**log_details, "error": str(e)}, exc_info=True)
        raise

# --- Ендпоінти API ---
@app.post(
    "/carts/{user_id}/items", 
    response_model=CartItemResponse, # Повертаємо доданий/оновлений товар
    status_code=status.HTTP_201_CREATED, 
    summary="Додати або оновити товар у кошику"
)
async def add_or_update_item_in_cart(
    user_id: str = Path(..., description="Ідентифікатор користувача", example="user_abc_123"), 
    item_request: CartItemCreate = Body(..., description="Товар для додавання/оновлення в кошику")
):
    """
    Додає товар до кошика користувача. Якщо товар вже існує, оновлює його кількість.
    Інформація про назву та ціну товару отримується з Сервісу Каталогу.
    """
    redis_conn = await get_redis_connection() # Отримуємо з'єднання з пулу
    log_details = {"user_id": user_id, "product_sku": item_request.product_sku, "requested_quantity": item_request.quantity}
    await _log_to_service_async("INFO", "Спроба додати/оновити товар у кошику.", log_details)

    product_sku = item_request.product_sku
    requested_quantity = item_request.quantity
    
    try:
        product_details = await get_product_details_async(product_sku)
    except pybreaker.CircuitBreakerError:
        await _log_to_service_async("ERROR", "Сервіс каталогу товарів недоступний (Circuit Breaker).", log_details)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс каталогу товарів тимчасово недоступний.")
    except RetryError:
        await _log_to_service_async("ERROR", f"Не вдалося отримати деталі товару {product_sku} після кількох спроб.", log_details)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Не вдалося зв'язатися з сервісом каталогу товарів.")
    
    if not product_details:
        await _log_to_service_async("WARNING", f"Товар {product_sku} не знайдено в каталозі при додаванні до кошика.", log_details)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар з SKU '{product_sku}' не знайдено.")

    cart_key = f"cart:{user_id}"
    
    try:
        # Використовуємо транзакцію Redis (pipeline) для атомарності операцій з одним товаром
        async with redis_conn.pipeline(transaction=True) as pipe:
            existing_item_json = await pipe.hget(cart_key, product_sku)
            
            current_quantity_in_cart = 0
            if existing_item_json:
                existing_item_data = json.loads(existing_item_json) # decode_responses=True, тому це вже str
                current_quantity_in_cart = existing_item_data.get("quantity", 0)
            
            # Якщо товар додається, то quantity - це кількість для додавання.
            # Якщо це оновлення, то requested_quantity - це нова загальна кількість.
            # Для простоти, припустимо, що цей ендпоінт завжди додає до існуючої кількості,
            # або встановлює, якщо товару немає.
            # Для більш чіткого оновлення потрібен окремий PUT ендпоінт.
            # Тут ми просто перезаписуємо або додаємо.
            # Для логіки "додати N до існуючої кількості":
            # final_quantity = current_quantity_in_cart + requested_quantity
            # Для логіки "встановити кількість N":
            final_quantity = requested_quantity # Для цього ендпоінту, це нова кількість

            if final_quantity <= 0: # Якщо оновлення призводить до 0 або менше, видаляємо товар
                 await pipe.hdel(cart_key, product_sku)
                 await _log_to_service_async("INFO", f"Товар {product_sku} видалено з кошика {user_id} через оновлення кількості до {final_quantity}.", {**log_details, "final_quantity": final_quantity})
                 # Повертаємо 204 No Content або іншу відповідь
                 # Для узгодженості з CartItemResponse, можливо, краще повернути порожній об'єкт або помилку
                 # Тут ми просто не повертаємо товар, якщо він видалений
                 return HTTPException(status_code=status.HTTP_204_NO_CONTENT)


            item_data_to_store = {
                "quantity": final_quantity,
                "name": product_details.get("name", "N/A"),
                "price_per_item": product_details.get("price", {}).get("amount", 0.0)
            }
            await pipe.hset(cart_key, product_sku, json.dumps(item_data_to_store))
            await pipe.expire(cart_key, REDIS_CART_TTL_SECONDS) # Оновлюємо TTL кошика при кожній зміні
            await pipe.execute() # Виконуємо транзакцію

        await _log_to_service_async("INFO", f"Товар {product_sku} успішно додано/оновлено в кошику {user_id}.", {**log_details, "final_quantity": final_quantity})
        
        return CartItemResponse(
            product_id=product_sku,
            quantity=final_quantity,
            name=item_data_to_store["name"],
            price_per_item=item_data_to_store["price_per_item"],
            item_total_price=round(final_quantity * item_data_to_store["price_per_item"], 2) if item_data_to_store["price_per_item"] else None
        )

    except redis.exceptions.RedisError as e:
        await _log_to_service_async("ERROR", f"Помилка Redis при роботі з кошиком {user_id}: {str(e)}", log_details)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Помилка сервісу кошика (Redis).")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка при додаванні товару до кошика {user_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка сервера.")


@app.get("/carts/{user_id}", response_model=CartResponse, summary="Отримати вміст кошика користувача")
async def get_user_cart_endpoint(
    user_id: str = Path(..., description="Ідентифікатор користувача", example="user_abc_123"),
    resolve_details: bool = Query(True, description="Чи потрібно отримувати актуальні назви/ціни з каталогу")
):
    redis_conn = await get_redis_connection()
    log_details = {"user_id": user_id, "resolve_details": resolve_details}
    await _log_to_service_async("DEBUG", "Запит на отримання кошика користувача.", log_details)

    cart_key = f"cart:{user_id}"
    response_items: List[CartItemResponse] = []
    grand_total_price = 0.0
    total_items_count = 0

    try:
        # @redis_breaker # Можна додати CB для операцій Redis
        raw_cart_items = await redis_conn.hgetall(cart_key)
        if not raw_cart_items:
            await _log_to_service_async("INFO", f"Кошик для користувача {user_id} порожній або не існує.", log_details)
            return CartResponse(user_id=user_id, items=[], total_items_count=0, grand_total_price=0.0, last_updated_at=None) # Повертаємо порожній кошик

        for product_sku, item_json_str in raw_cart_items.items():
            try:
                item_data_stored = json.loads(item_json_str) # decode_responses=True, тому це вже str
                quantity = item_data_stored.get("quantity", 0)
                
                if quantity <= 0: # Пропускаємо невалідні записи
                    await _log_to_service_async("WARNING", f"Знайдено товар {product_sku} з кількістю <= 0 в кошику {user_id}, ігнорується.", {**log_details, "product_sku": product_sku})
                    continue

                name = item_data_stored.get("name")
                price = item_data_stored.get("price_per_item")

                if resolve_details: # Якщо потрібно оновити дані з каталогу
                    try:
                        product_details_live = await get_product_details_async(product_sku)
                        if product_details_live:
                            name = product_details_live.get("name", name) # Оновлюємо, якщо є нове
                            price = product_details_live.get("price", {}).get("amount", price)
                            # Опціонально: оновити дані в Redis, якщо вони змінилися
                            if name != item_data_stored.get("name") or price != item_data_stored.get("price_per_item"):
                                item_data_stored["name"] = name
                                item_data_stored["price_per_item"] = price
                                await redis_conn.hset(cart_key, product_sku, json.dumps(item_data_stored))
                                await _log_to_service_async("INFO", f"Оновлено дані товару {product_sku} в кошику {user_id} з каталогу.", {**log_details, "product_sku": product_sku})
                        else: # Товар більше не існує в каталозі
                            await _log_to_service_async("WARNING", f"Товар {product_sku} з кошика {user_id} не знайдено в каталозі. Можливо, його варто видалити з кошика.", {**log_details, "product_sku": product_sku})
                            # Тут можна додати логіку видалення товару з кошика або позначення його як "недоступний"
                            # Для простоти, поки що залишимо його з останніми відомими даними, але без ціни для розрахунку
                            price = None # Не враховувати в загальну суму, якщо товар не знайдено
                    except (pybreaker.CircuitBreakerError, RetryError) as e_catalog:
                        await _log_to_service_async("WARNING", f"Не вдалося оновити деталі для товару {product_sku} з каталогу (залежність недоступна): {str(e_catalog)}", {**log_details, "product_sku": product_sku})
                        # Використовуємо старі дані з Redis, якщо вони є

                item_total = (quantity * price) if price is not None else 0.0
                response_items.append(CartItemResponse(
                    product_id=product_sku,
                    quantity=quantity,
                    name=name or "Назва невідома",
                    price_per_item=price,
                    item_total_price=round(item_total, 2) if price is not None else None
                ))
                if price is not None:
                    grand_total_price += item_total
                total_items_count += quantity
            
            except json.JSONDecodeError:
                await _log_to_service_async("ERROR", f"Помилка декодування JSON для товару {product_sku} в кошику {user_id}.", {**log_details, "product_sku": product_sku})
            except Exception as e_item: # Ловимо помилки обробки окремого товару
                 await _log_to_service_async("ERROR", f"Помилка обробки товару {product_sku} в кошику {user_id}: {str(e_item)}", {**log_details, "product_sku": product_sku}, exc_info=True)


        # Отримання часу останнього оновлення (якщо зберігається, наприклад, окремим ключем або TTL)
        # Для простоти, тут не реалізовано, але можна додати ключ типу cart:{user_id}:meta
        # last_updated_timestamp = await redis_conn.get(f"cart:{user_id}:last_updated")
        # last_updated_at = datetime.datetime.fromisoformat(last_updated_timestamp) if last_updated_timestamp else None
        # Для прикладу, просто повернемо None
        last_updated_at = None 

        await _log_to_service_async("INFO", f"Кошик для користувача {user_id} успішно отримано.", {**log_details, "items_count_response": len(response_items), "total_price_response": grand_total_price})
        return CartResponse(
            user_id=user_id, 
            items=response_items, 
            total_items_count=total_items_count,
            grand_total_price=round(grand_total_price, 2),
            last_updated_at=last_updated_at
        )

    except redis.exceptions.RedisError as e:
        await _log_to_service_async("ERROR", f"Помилка Redis при отриманні кошика {user_id}: {str(e)}", log_details)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Помилка сервісу кошика (Redis).")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка при отриманні кошика {user_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка сервера.")


@app.delete(
    "/carts/{user_id}/items/{product_sku}", 
    status_code=status.HTTP_204_NO_CONTENT, 
    summary="Видалити конкретний товар з кошика"
)
async def remove_item_from_cart_endpoint(
    user_id: str = Path(..., description="Ідентифікатор користувача"), 
    product_sku: str = Path(..., description="SKU товару для видалення")
):
    redis_conn = await get_redis_connection()
    log_details = {"user_id": user_id, "product_sku": product_sku}
    await _log_to_service_async("INFO", "Спроба видалити товар з кошика.", log_details)

    cart_key = f"cart:{user_id}"
    try:
        # @redis_breaker
        deleted_count = await redis_conn.hdel(cart_key, product_sku)
        if deleted_count == 0:
            await _log_to_service_async("WARNING", f"Спроба видалити неіснуючий товар {product_sku} з кошика {user_id}.", log_details)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар '{product_sku}' не знайдено в кошику користувача '{user_id}'.")
        
        await redis_conn.expire(cart_key, REDIS_CART_TTL_SECONDS) # Оновлюємо TTL
        await _log_to_service_async("INFO", f"Товар {product_sku} успішно видалено з кошика {user_id}.", log_details)
        # Статус 204 не повинен повертати тіло відповіді
        return 
    except redis.exceptions.RedisError as e:
        await _log_to_service_async("ERROR", f"Помилка Redis при видаленні товару з кошика {user_id}: {str(e)}", log_details)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Помилка сервісу кошика (Redis).")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка при видаленні товару з кошика {user_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка сервера.")


@app.delete(
    "/carts/{user_id}", 
    status_code=status.HTTP_204_NO_CONTENT, 
    summary="Повністю очистити кошик користувача"
)
async def clear_user_cart_endpoint(user_id: str = Path(..., description="Ідентифікатор користувача")):
    redis_conn = await get_redis_connection()
    log_details = {"user_id": user_id}
    await _log_to_service_async("INFO", "Спроба очистити кошик користувача.", log_details)
    
    cart_key = f"cart:{user_id}"
    try:
        # @redis_breaker
        deleted_key_count = await redis_conn.delete(cart_key) # Команда delete повертає кількість видалених ключів
        if deleted_key_count == 0:
            await _log_to_service_async("INFO", f"Кошик для користувача {user_id} вже був порожній або не існував.", log_details)
            # Все одно повертаємо 204, оскільки результат - порожній кошик
        else:
            await _log_to_service_async("INFO", f"Кошик користувача {user_id} успішно очищено.", log_details)
        return
    except redis.exceptions.RedisError as e:
        await _log_to_service_async("ERROR", f"Помилка Redis при очищенні кошика {user_id}: {str(e)}", log_details)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Помилка сервісу кошика (Redis).")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка при очищенні кошика {user_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка сервера.")

    
@app.get("/health", summary="Перевірка стану сервісу кошика")
async def health_check_endpoint():
    redis_status = "disconnected"
    redis_details = {}
    if redis_pool:
        try:
            # @redis_breaker # Можна додати CB і для ping
            await redis_pool.ping()
            redis_status = "connected"
            # Можна додати більше інформації, наприклад, кількість ключів (обережно на продакшені)
            # info = await redis_pool.info()
            # redis_details["redis_version"] = info.get("redis_version")
            # redis_details["connected_clients"] = info.get("connected_clients")
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as e:
            redis_status = "connection_error"
            redis_details["error"] = str(e)
            logger.error(f"Health check: помилка з'єднання з Redis: {e}")
        except Exception as e:
            redis_status = "error"
            redis_details["error"] = str(e)
            logger.error(f"Health check: неочікувана помилка Redis: {e}")
            
    logging_service_status = "unknown"
    if LOGGING_SERVICE_URL and http_client:
        try:
            response = await http_client.get(f"{LOGGING_SERVICE_URL}/health", timeout=1.0)
            logging_service_status = "connected" if response.status_code == 200 else f"error_status_{response.status_code}"
        except Exception:
            logging_service_status = "unreachable"
    
    product_catalog_status = "unknown"
    if PRODUCT_CATALOG_SERVICE_URL and http_client:
        try:
            response = await http_client.get(f"{PRODUCT_CATALOG_SERVICE_URL}/health", timeout=1.0)
            product_catalog_status = "connected" if response.status_code == 200 else f"error_status_{response.status_code}"
        except Exception:
            product_catalog_status = "unreachable"

    log_cb_state = "CLOSED" if not log_service_breaker.opened else "OPEN"
    product_cb_state = "CLOSED" if not product_catalog_breaker.opened else "OPEN"
    
    overall_status = "healthy"
    if redis_status != "connected" or \
       (LOGGING_SERVICE_URL and logging_service_status != "connected") or \
       (PRODUCT_CATALOG_SERVICE_URL and product_catalog_status != "connected"):
        overall_status = "degraded"

    return {
        "service_status": overall_status,
        "service_name": SERVICE_NAME,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "dependencies": {
            "redis": {"status": redis_status, **redis_details},
            "logging_service": {"status": logging_service_status, "url": LOGGING_SERVICE_URL or "N/A"},
            "product_catalog_service": {"status": product_catalog_status, "url": PRODUCT_CATALOG_SERVICE_URL or "N/A"}
        },
        "circuit_breakers": {
            "logging_service": {"state": log_cb_state, "failures": log_service_breaker.fail_counter},
            "product_catalog_service": {"state": product_cb_state, "failures": product_catalog_breaker.fail_counter}
        }
    }
