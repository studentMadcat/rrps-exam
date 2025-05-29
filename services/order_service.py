from fastapi import FastAPI, HTTPException, Depends, Path, Body, Query, status
from pydantic import BaseModel, Field, conint, constr
from typing import List, Dict, Any, Optional, Union
import sqlalchemy
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
import databases 
import os
import httpx 
import datetime
import logging
from prometheus_fastapi_instrumentator import Instrumentator
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError, retry_if_exception_type
import pybreaker
import asyncio # Для паралельного виконання запитів

# --- Налаштування логування ---
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Моделі Pydantic ---
class Address(BaseModel):
    street: str = Field(..., max_length=255, example="вул. Хрещатик, 1")
    city: str = Field(..., max_length=100, example="Київ")
    postal_code: str = Field(..., max_length=20, example="01001")
    country: str = Field(..., max_length=100, example="Україна")
    details: Optional[str] = Field(None, max_length=500, example="кв. 10, під'їзд 2")

class OrderItemBase(BaseModel):
    product_sku: str = Field(..., description="SKU товару", example="SKU-SUPERX-12PRO-BLK")
    quantity: conint(gt=0) = Field(..., description="Кількість товару", example=1)
    # Ціна буде отримана з сервісу каталогу або кошика на момент створення замовлення

class OrderItemCreateInternal(OrderItemBase): # Для внутрішнього використання при створенні
    price_at_purchase: float = Field(..., description="Ціна за одиницю на момент покупки")
    product_name: Optional[str] = Field(None, description="Назва товару (для зручності)")

class OrderItemResponse(OrderItemBase):
    id: int
    order_id: int
    price_at_purchase: float = Field(..., description="Ціна за одиницю на момент покупки")
    product_name: Optional[str] = Field(None, description="Назва товару")
    item_total_price: float = Field(..., description="Загальна вартість позиції")

class OrderCreateRequest(BaseModel):
    user_id: constr(min_length=1) = Field(..., description="Ідентифікатор користувача", example="user-123-xyz")
    shipping_address: Address = Field(..., description="Адреса доставки")
    # payment_method_id: Optional[str] = Field(None, description="ID методу оплати") # Для майбутнього розширення
    customer_notes: Optional[str] = Field(None, max_length=1000, description="Примітки клієнта до замовлення")

class OrderStatusUpdate(BaseModel):
    status: str = Field(..., description="Новий статус замовлення", example="shipped")
    # Можна додати поле для коментаря до зміни статусу
    # admin_comment: Optional[str] = None

class OrderResponse(BaseModel):
    id: int = Field(..., description="Унікальний ID замовлення")
    user_id: str = Field(..., description="Ідентифікатор користувача")
    status: str = Field(..., description="Поточний статус замовлення", example="pending_payment")
    total_amount: float = Field(..., description="Загальна сума замовлення")
    currency: str = Field(default="UAH", description="Валюта замовлення") # Припускаємо одну валюту для простоти
    created_at: datetime.datetime = Field(..., description="Час створення замовлення")
    updated_at: datetime.datetime = Field(..., description="Час останнього оновлення замовлення")
    shipping_address: Address = Field(..., description="Адреса доставки")
    items: List[OrderItemResponse] = Field(..., description="Список товарів у замовленні")
    customer_notes: Optional[str] = Field(None, description="Примітки клієнта")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": 101, "user_id": "user-123-xyz", "status": "processing",
                "total_amount": 3599.98, "currency": "UAH",
                "created_at": "2024-05-30T10:00:00Z", "updated_at": "2024-05-30T10:05:00Z",
                "shipping_address": {"street": "вул. Шевченка, 10", "city": "Львів", "postal_code": "79000", "country": "Україна", "details": "офіс 5"},
                "items": [
                    {"id": 201, "order_id": 101, "product_sku": "SKU001", "quantity": 2, "price_at_purchase": 1500.00, "product_name": "Книга 'Цікава Історія'", "item_total_price": 3000.00},
                    {"id": 202, "order_id": 101, "product_sku": "SKU004", "quantity": 1, "price_at_purchase": 599.98, "product_name": "Чашка з Лого", "item_total_price": 599.98}
                ],
                "customer_notes": "Будь ласка, зателефонуйте перед доставкою."
            }
        }
    }

# --- Глобальні змінні та конфігурація ---
app = FastAPI(
    title="Сервіс Замовлень",
    description="API для управління замовленнями в інтернет-магазині.",
    version="1.3.0"
)
Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# Конфігурація
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://order_user:order_password@postgresql:5432/order_db")
CART_SERVICE_URL = os.getenv("CART_SERVICE_URL", "http://cart-service:8000")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8000")
LOGGING_SERVICE_URL = os.getenv("LOGGING_SERVICE_URL")
SERVICE_NAME = "OrderService"
HTTP_CLIENT_TIMEOUT = float(os.getenv("HTTP_CLIENT_TIMEOUT", "7.0")) # Таймаут для HTTP клієнта

# SQLAlchemy та Databases налаштування
database = databases.Database(DATABASE_URL) # Асинхронний об'єкт бази даних
metadata = sqlalchemy.MetaData() # Метадані для SQLAlchemy

# SQLAlchemy моделі таблиць
orders_table = sqlalchemy.Table(
    "orders", metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True, index=True, autoincrement=True),
    sqlalchemy.Column("user_id", sqlalchemy.String(255), index=True, nullable=False),
    sqlalchemy.Column("status", sqlalchemy.String(50), default="pending_payment", nullable=False, index=True),
    sqlalchemy.Column("total_amount", sqlalchemy.Numeric(10, 2), nullable=False), # Numeric для точності грошей
    sqlalchemy.Column("currency", sqlalchemy.String(3), default="UAH", nullable=False),
    sqlalchemy.Column("created_at", sqlalchemy.DateTime, default=datetime.datetime.utcnow, nullable=False),
    sqlalchemy.Column("updated_at", sqlalchemy.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False),
    sqlalchemy.Column("shipping_address", sqlalchemy.JSON, nullable=False),
    sqlalchemy.Column("customer_notes", sqlalchemy.Text, nullable=True)
)

order_items_table = sqlalchemy.Table(
    "order_items", metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True, index=True, autoincrement=True),
    sqlalchemy.Column("order_id", sqlalchemy.Integer, sqlalchemy.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True),
    sqlalchemy.Column("product_sku", sqlalchemy.String(100), nullable=False, index=True), # Змінено з product_id
    sqlalchemy.Column("product_name", sqlalchemy.String(255), nullable=True), # Для зручності, денормалізація
    sqlalchemy.Column("quantity", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("price_at_purchase", sqlalchemy.Numeric(10, 2), nullable=False) # Ціна за одиницю
)

# Створення таблиць (зазвичай робиться через Alembic у продакшені)
# Для прототипу, можна створити при старті, якщо їх немає
engine = sqlalchemy.create_engine(DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://")) # Синхронний для create_all
# metadata.create_all(bind=engine) # Розкоментувати для створення таблиць при першому запуску

# HTTP клієнт
http_client: Optional[httpx.AsyncClient] = None

# Circuit Breakers
log_service_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name=f"{SERVICE_NAME}_LoggingServiceBreaker")
cart_service_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=45, name=f"{SERVICE_NAME}_CartServiceBreaker")
notification_service_breaker = pybreaker.CircuitBreaker(fail_max=2, reset_timeout=60, name=f"{SERVICE_NAME}_NotificationServiceBreaker")
db_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name=f"{SERVICE_NAME}_DatabaseBreaker") # CB для операцій з БД

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
        logger.warning(f"Резервне логування: Level: {level}, Message: {message}, Details: {details}")
        return
    log_payload = {
        "service_name": SERVICE_NAME, "level": level.upper(), "message": message,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z", "details": details or {}
    }
    try:
        response = await http_client.post(f"{LOGGING_SERVICE_URL}/logs", json=log_payload)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Не вдалося надіслати лог до сервісу: {str(e)}. Оригінальний лог: {log_payload}")
        raise

# --- Обробники подій FastAPI ---
@app.on_event("startup")
async def startup_event_async():
    global http_client
    logger.info(f"Запуск сервісу '{SERVICE_NAME}'...")
    http_client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)
    
    @db_breaker
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=10), reraise=True)
    async def _connect_db():
        try:
            await database.connect()
            logger.info(f"Успішно підключено до PostgreSQL: {DATABASE_URL.split('@')[-1]}") # Не логуємо пароль
            await _log_to_service_async("INFO", "Сервіс замовлень підключено до PostgreSQL.")
        except Exception as e: # databases.exceptions.ConnectionClosed, etc.
            logger.critical(f"Помилка підключення до PostgreSQL: {str(e)}")
            raise # Для спрацювання retry/CB
    
    try:
        await _connect_db()
    except (pybreaker.CircuitBreakerError, RetryError) as e:
        logger.critical(f"Критична помилка: не вдалося підключитися до PostgreSQL після кількох спроб: {str(e)}. Сервіс може бути непрацездатним.")
        await _log_to_service_async("CRITICAL", f"Не вдалося підключитися до PostgreSQL при старті: {str(e)}")
    except Exception as e:
        logger.critical(f"Неочікувана помилка при старті (DB): {str(e)}", exc_info=True)
        await _log_to_service_async("CRITICAL", f"Неочікувана помилка DB при старті: {str(e)}")


@app.on_event("shutdown")
async def shutdown_event_async():
    if http_client:
        await http_client.aclose()
    if database.is_connected:
        await database.disconnect()
        logger.info("З'єднання з PostgreSQL закрито.")
    try:
        await _log_to_service_async("INFO", f"Сервіс '{SERVICE_NAME}' зупинено.")
    except Exception as e:
        logger.warning(f"Не вдалося надіслати фінальний лог при зупинці: {str(e)}")

# --- Функції взаємодії з іншими сервісами ---
@cart_service_breaker
@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=0.5, max=4),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
async def get_cart_data_from_service(user_id: str) -> Optional[Dict[str, Any]]:
    if not CART_SERVICE_URL or not http_client:
        await _log_to_service_async("ERROR", "CART_SERVICE_URL не налаштовано або HTTP клієнт не ініціалізовано.", {"user_id": user_id})
        return None
    
    request_url = f"{CART_SERVICE_URL}/carts/{user_id}?resolve_details=true" # Запитуємо деталі товарів
    log_details = {"user_id": user_id, "request_url": request_url}
    await _log_to_service_async("DEBUG", f"Запит даних кошика для користувача {user_id}.", log_details)
    
    try:
        response = await http_client.get(request_url)
        if response.status_code == status.HTTP_404_NOT_FOUND:
            await _log_to_service_async("INFO", f"Кошик для користувача {user_id} не знайдено в сервісі кошика.", {**log_details, "status_code": 404})
            return None # Кошик не знайдено - це не помилка для цього запиту
        response.raise_for_status()
        cart_data = response.json()
        await _log_to_service_async("DEBUG", f"Дані кошика для {user_id} успішно отримано.", {**log_details, "item_count": len(cart_data.get("items", []))})
        return cart_data
    except httpx.HTTPStatusError as e:
        await _log_to_service_async("ERROR", f"HTTP помилка ({e.response.status_code}) при отриманні кошика {user_id}: {e.response.text}", {**log_details, "error": str(e)})
        raise
    except httpx.RequestError as e:
        await _log_to_service_async("ERROR", f"Мережева помилка при отриманні кошика {user_id}: {str(e)}", {**log_details, "error": str(e)})
        raise
    except Exception as e:
        await _log_to_service_async("ERROR", f"Неочікувана помилка при отриманні кошика {user_id}: {str(e)}", {**log_details, "error": str(e)}, exc_info=True)
        raise

@cart_service_breaker # Використовуємо той самий CB, що й для get_cart_data
@retry(
    stop=stop_after_attempt(2), # Менше спроб для операції, що не є критичною для створення замовлення
    wait=wait_exponential(multiplier=1, min=0.5, max=2),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=False # Не перевикидаємо помилку, просто логуємо, якщо не вдалося
)
async def clear_cart_in_service(user_id: str):
    if not CART_SERVICE_URL or not http_client:
        await _log_to_service_async("ERROR", "CART_SERVICE_URL не налаштовано (clear_cart).", {"user_id": user_id})
        return
    request_url = f"{CART_SERVICE_URL}/carts/{user_id}"
    log_details = {"user_id": user_id, "request_url": request_url}
    await _log_to_service_async("INFO", f"Спроба очистити кошик для користувача {user_id}.", log_details)
    try:
        response = await http_client.delete(request_url)
        response.raise_for_status()
        await _log_to_service_async("INFO", f"Кошик для користувача {user_id} успішно очищено через сервіс кошика.", log_details)
    except Exception as e: # Ловимо всі помилки, щоб не перервати основний процес
        await _log_to_service_async("WARNING", f"Не вдалося очистити кошик {user_id} через сервіс кошика: {str(e)}", {**log_details, "error": str(e)})

@notification_service_breaker
@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=False # Не перевикидаємо, щоб не блокувати основний процес через помилку нотифікації
)
async def send_order_notification_async(payload: Dict[str, Any]):
    if not NOTIFICATION_SERVICE_URL or not http_client:
        await _log_to_service_async("ERROR", "NOTIFICATION_SERVICE_URL не налаштовано (send_order_notification).", {"payload_type": payload.get("type")})
        return
    log_details = {"payload_type": payload.get("type"), "recipient": payload.get("recipient")}
    await _log_to_service_async("INFO", "Надсилання запиту на нотифікацію про замовлення.", log_details)
    try:
        response = await http_client.post(f"{NOTIFICATION_SERVICE_URL}/notifications/send", json=payload)
        response.raise_for_status()
        await _log_to_service_async("INFO", "Запит на нотифікацію про замовлення успішно надіслано.", log_details)
    except Exception as e:
        await _log_to_service_async("WARNING", f"Не вдалося надіслати нотифікацію про замовлення: {str(e)}", {**log_details, "error": str(e)})

# --- Ендпоінти API ---
@app.post(
    "/orders", 
    response_model=OrderResponse, 
    status_code=status.HTTP_201_CREATED, 
    summary="Створити нове замовлення"
)
async def create_new_order_endpoint(order_request: OrderCreateRequest = Body(...)):
    user_id = order_request.user_id
    log_details_main = {"user_id": user_id, "shipping_address": order_request.shipping_address.model_dump()}
    await _log_to_service_async("INFO", f"Початок обробки запиту на створення замовлення для користувача {user_id}.", log_details_main)

    cart_data: Optional[Dict[str, Any]] = None
    try:
        cart_data = await get_cart_data_from_service(user_id)
    except pybreaker.CircuitBreakerError:
        await _log_to_service_async("ERROR", f"Сервіс кошика недоступний (CB) при створенні замовлення для {user_id}.", log_details_main)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс кошика тимчасово недоступний. Спробуйте пізніше.")
    except RetryError:
        await _log_to_service_async("ERROR", f"Не вдалося отримати дані кошика для {user_id} після кількох спроб.", log_details_main)
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Не вдалося зв'язатися з сервісом кошика. Спробуйте пізніше.")
    except Exception as e: # Інші неочікувані помилки від get_cart_data_from_service
        await _log_to_service_async("ERROR", f"Неочікувана помилка при отриманні даних кошика для {user_id}: {str(e)}", log_details_main, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка при отриманні даних кошика.")


    if not cart_data or not cart_data.get("items"):
        await _log_to_service_async("WARNING", f"Спроба створити замовлення з порожнім кошиком для {user_id}.", log_details_main)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Кошик порожній. Неможливо створити замовлення.")

    cart_items_raw = cart_data.get("items", [])
    calculated_total_amount = float(cart_data.get("grand_total_price", 0.0)) # Використовуємо ціну з кошика

    if not cart_items_raw: # Додаткова перевірка
        await _log_to_service_async("WARNING", f"Кошик для {user_id} не містить товарів, хоча дані кошика отримано.", log_details_main)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Кошик не містить товарів.")
    
    # Імітація перевірки наявності товарів та їх кількості (можна додати реальні запити до каталогу)
    # for item in cart_items_raw:
    #     # product_stock = await get_stock_from_catalog(item['product_sku'])
    #     # if product_stock < item['quantity']:
    #     #     raise HTTPException(status_code=409, detail=f"Товару {item['product_sku']} недостатньо на складі.")
    #     pass

    # Імітація процесу оплати
    payment_successful = True # Для прототипу завжди успішно
    await _log_to_service_async("INFO", f"Імітація обробки платежу для замовлення користувача {user_id}.", {**log_details_main, "total_amount": calculated_total_amount})
    if not payment_successful:
        await _log_to_service_async("WARNING", f"Платіж для замовлення {user_id} не успішний.", log_details_main)
        # Можна створити замовлення зі статусом 'payment_failed' або повернути помилку
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="Помилка обробки платежу.")

    order_id: Optional[int] = None
    try:
        async with database.transaction(): # Атомарна операція для створення замовлення та його позицій
            order_values = {
                "user_id": user_id,
                "status": "pending_payment" if not payment_successful else "processing", # Приклад статусу
                "total_amount": calculated_total_amount,
                "currency": "UAH", # Припускаємо одну валюту
                "shipping_address": order_request.shipping_address.model_dump(),
                "customer_notes": order_request.customer_notes,
                "created_at": datetime.datetime.utcnow(),
                "updated_at": datetime.datetime.utcnow()
            }
            order_query = orders_table.insert().values(**order_values)
            order_id = await database.execute(order_query)
            await _log_to_service_async("DEBUG", f"Створено запис замовлення ID {order_id} в БД.", {**log_details_main, "order_id": order_id})

            order_items_to_insert = []
            for item_raw in cart_items_raw:
                # 'product_id' з кошика - це SKU
                order_items_to_insert.append({
                    "order_id": order_id,
                    "product_sku": item_raw["product_sku"],
                    "product_name": item_raw.get("name"), # Зберігаємо назву для зручності
                    "quantity": item_raw["quantity"],
                    "price_at_purchase": item_raw.get("price_per_item", 0.0) 
                })
            
            if order_items_to_insert:
                items_insert_query = order_items_table.insert()
                await database.execute_many(query=items_insert_query, values=order_items_to_insert)
                await _log_to_service_async("DEBUG", f"Додано {len(order_items_to_insert)} товарів до замовлення ID {order_id}.", {**log_details_main, "order_id": order_id})
            else: # Малоймовірно, якщо cart_items_raw не порожній
                await _log_to_service_async("WARNING", f"Немає товарів для додавання до замовлення ID {order_id}.", {**log_details_main, "order_id": order_id})


    except SQLAlchemyError as e: # Обробка помилок бази даних
        await _log_to_service_async("ERROR", f"Помилка SQLAlchemy при створенні замовлення для {user_id}: {str(e)}", log_details_main, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Помилка бази даних при створенні замовлення: {str(e)}")
    except Exception as e: # Інші непередбачені помилки під час транзакції
        await _log_to_service_async("CRITICAL", f"Непередбачена помилка під час транзакції створення замовлення для {user_id}: {str(e)}", log_details_main, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера під час створення замовлення: {str(e)}")

    if order_id is None: # Якщо транзакція не вдалася і order_id не було присвоєно
        await _log_to_service_async("CRITICAL", f"Транзакція створення замовлення для {user_id} не повернула ID.", log_details_main)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не вдалося створити замовлення (ID не отримано).")

    # Подальші дії після успішної транзакції
    await clear_cart_in_service(user_id) # Очищення кошика (некритично, якщо не вдасться)
    
    await send_order_notification_async({ # Надсилання сповіщення (некритично)
        "type": "email", 
        "recipient": f"user_{user_id}@example.com", # Замініть на реальний email або отримання з профілю користувача
        "template_id": "order_confirmation",
        "context": {
            "order_id": order_id, 
            "user_name": user_id, # Замініть на ім'я користувача
            "total_amount": f"{calculated_total_amount:.2f} UAH",
            "shipping_address": order_request.shipping_address.model_dump_json() # Передаємо як JSON рядок або словник
        }
    })
    
    # Отримання повних даних створеного замовлення для відповіді
    try:
        final_order_data = await get_order_details_by_id(order_id) # Використовуємо нову функцію
        if not final_order_data: # Малоймовірно, якщо щойно створили
             await _log_to_service_async("ERROR", f"Не вдалося отримати деталі щойно створеного замовлення ID {order_id}.", {"order_id": order_id})
             raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка отримання даних створеного замовлення.")
        
        await _log_to_service_async("INFO", f"Замовлення ID {order_id} успішно створено для користувача {user_id}.", {**log_details_main, "order_id": order_id})
        return final_order_data
    except Exception as e:
        await _log_to_service_async("ERROR", f"Помилка при отриманні деталей створеного замовлення ID {order_id}: {str(e)}", {"order_id": order_id}, exc_info=True)
        # Повертаємо хоча б ID, якщо деталі не вдалося отримати
        raise HTTPException(status_code=status.HTTP_201_CREATED, detail=f"Замовлення створено (ID: {order_id}), але виникла помилка при отриманні повних деталей.")


async def get_order_details_by_id(order_id: int, db_conn: Optional[databases.Database] = None) -> Optional[OrderResponse]:
    """Допоміжна функція для отримання деталей замовлення за ID."""
    conn = db_conn or database # Використовуємо передане з'єднання або глобальне
    if not conn.is_connected: await conn.connect() # Переконуємося, що є з'єднання

    order_query = orders_table.select().where(orders_table.c.id == order_id)
    order_record = await conn.fetch_one(order_query)

    if not order_record:
        return None
    
    items_query = order_items_table.select().where(order_items_table.c.order_id == order_id)
    items_records = await conn.fetch_all(items_query)
    
    order_items_response = []
    for item_rec in items_records:
        item_total = item_rec["quantity"] * float(item_rec["price_at_purchase"])
        order_items_response.append(OrderItemResponse(
            id=item_rec["id"],
            order_id=item_rec["order_id"],
            product_sku=item_rec["product_sku"], # Змінено з product_id
            product_name=item_rec["product_name"],
            quantity=item_rec["quantity"],
            price_at_purchase=float(item_rec["price_at_purchase"]),
            item_total_price=round(item_total, 2)
        ))
        
    return OrderResponse(
        id=order_record["id"],
        user_id=order_record["user_id"],
        status=order_record["status"],
        total_amount=float(order_record["total_amount"]),
        currency=order_record["currency"],
        created_at=order_record["created_at"],
        updated_at=order_record["updated_at"],
        shipping_address=order_record["shipping_address"], # Вже є словником з JSON
        items=order_items_response,
        customer_notes=order_record["customer_notes"]
    )

@app.get(
    "/orders/{order_id}", 
    response_model=OrderResponse, 
    summary="Отримати деталі конкретного замовлення",
    responses={404: {"description": "Замовлення не знайдено"}}
)
async def get_order_details_endpoint(order_id: int = Path(..., description="ID замовлення")):
    log_details = {"order_id": order_id}
    await _log_to_service_async("DEBUG", f"Запит деталей замовлення ID {order_id}.", log_details)
    
    try:
        # @db_breaker
        order_response = await get_order_details_by_id(order_id)
        if not order_response:
            await _log_to_service_async("WARNING", f"Замовлення ID {order_id} не знайдено.", log_details)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Замовлення з ID {order_id} не знайдено.")
        
        await _log_to_service_async("INFO", f"Успішно отримано деталі замовлення ID {order_id}.", log_details)
        return order_response
    except SQLAlchemyError as e:
        await _log_to_service_async("ERROR", f"Помилка SQLAlchemy при отриманні замовлення {order_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Непередбачена помилка при отриманні замовлення {order_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.get(
    "/orders", 
    response_model=List[OrderResponse], 
    summary="Отримати список замовлень з фільтрацією та пагінацією"
)
async def get_orders_list_endpoint(
    user_id: Optional[str] = Query(None, description="Фільтрувати замовлення за ID користувача"),
    status: Optional[str] = Query(None, description="Фільтрувати замовлення за статусом"),
    page: int = Query(1, ge=1, description="Номер сторінки"),
    limit: int = Query(10, ge=1, le=100, description="Кількість замовлень на сторінці"),
    sort_by: str = Query("created_at_desc", description="Поле для сортування (created_at, total_amount) та напрямок (_asc, _desc)")
):
    log_details = {"user_id_filter": user_id, "status_filter": status, "page": page, "limit": limit, "sort_by": sort_by}
    await _log_to_service_async("DEBUG", "Запит списку замовлень.", log_details)

    base_query = orders_table.select()
    if user_id:
        base_query = base_query.where(orders_table.c.user_id == user_id)
    if status:
        base_query = base_query.where(orders_table.c.status == status)

    # Сортування
    sort_field_map = {"created_at": orders_table.c.created_at, "total_amount": orders_table.c.total_amount}
    sort_key_sqlalchemy = orders_table.c.created_at
    
    if sort_by:
        sort_parts = sort_by.lower().rsplit('_', 1)
        field_name = sort_parts[0]
        direction = sort_parts[1] if len(sort_parts) > 1 else "desc"
        
        if field_name in sort_field_map:
            sort_key_sqlalchemy = sort_field_map[field_name]
            if direction == "asc":
                base_query = base_query.order_by(sort_key_sqlalchemy.asc())
            else: # desc за замовчуванням
                base_query = base_query.order_by(sort_key_sqlalchemy.desc())
        else: # За замовчуванням, якщо поле невідоме
             base_query = base_query.order_by(orders_table.c.created_at.desc())
    else: # За замовчуванням
        base_query = base_query.order_by(orders_table.c.created_at.desc())
    
    # Пагінація
    offset = (page - 1) * limit
    paged_query = base_query.offset(offset).limit(limit)

    try:
        # @db_breaker
        order_records = await database.fetch_all(paged_query)
        
        result_orders: List[OrderResponse] = []
        if order_records:
            # Отримуємо ID всіх знайдених замовлень для пакетного запиту товарів
            order_ids = [record["id"] for record in order_records]
            items_query = order_items_table.select().where(order_items_table.c.order_id.in_(order_ids))
            all_items_records = await database.fetch_all(items_query)
            
            # Групуємо товари за order_id для ефективного доступу
            items_by_order_id: Dict[int, List[Dict]] = {}
            for item_rec in all_items_records:
                items_by_order_id.setdefault(item_rec["order_id"], []).append(dict(item_rec))

            for order_rec in order_records:
                order_id = order_rec["id"]
                order_items_list = []
                for item_data in items_by_order_id.get(order_id, []):
                    item_total = item_data["quantity"] * float(item_data["price_at_purchase"])
                    order_items_list.append(OrderItemResponse(
                        id=item_data["id"], order_id=order_id,
                        product_sku=item_data["product_sku"], product_name=item_data["product_name"],
                        quantity=item_data["quantity"], price_at_purchase=float(item_data["price_at_purchase"]),
                        item_total_price=round(item_total, 2)
                    ))
                
                result_orders.append(OrderResponse(
                    id=order_id, user_id=order_rec["user_id"], status=order_rec["status"],
                    total_amount=float(order_rec["total_amount"]), currency=order_rec["currency"],
                    created_at=order_rec["created_at"], updated_at=order_rec["updated_at"],
                    shipping_address=order_rec["shipping_address"], items=order_items_list,
                    customer_notes=order_rec["customer_notes"]
                ))
        
        await _log_to_service_async("INFO", f"Успішно отримано {len(result_orders)} замовлень.", {**log_details, "retrieved_count": len(result_orders)})
        return result_orders
    except SQLAlchemyError as e:
        await _log_to_service_async("ERROR", f"Помилка SQLAlchemy при отриманні списку замовлень: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Непередбачена помилка при отриманні списку замовлень: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.put(
    "/orders/{order_id}/status", 
    response_model=OrderResponse, 
    summary="Оновити статус замовлення (для адміна)",
    responses={404: {"description": "Замовлення не знайдено"}, 400: {"description": "Невалідний статус"}}
)
async def update_order_status_endpoint(
    order_id: int = Path(..., description="ID замовлення для оновлення статусу"), 
    status_update: OrderStatusUpdate = Body(...)
):
    new_status = status_update.status
    # Можна додати валідацію можливих статусів
    allowed_statuses = ["pending_payment", "processing", "shipped", "delivered", "cancelled", "refunded", "on_hold"]
    if new_status not in allowed_statuses:
        await _log_to_service_async("WARNING", f"Спроба встановити невалідний статус '{new_status}' для замовлення ID {order_id}.", {"order_id": order_id, "new_status": new_status})
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Невалідний статус замовлення: '{new_status}'. Дозволені: {', '.join(allowed_statuses)}")

    log_details = {"order_id": order_id, "new_status": new_status}
    await _log_to_service_async("INFO", f"Спроба оновити статус замовлення ID {order_id} на '{new_status}'.", log_details)

    try:
        async with database.transaction(): # Виконуємо в транзакції для атомарності
            # @db_breaker
            # Перевіряємо, чи існує замовлення
            select_query = orders_table.select().where(orders_table.c.id == order_id)
            existing_order = await database.fetch_one(select_query)
            if not existing_order:
                await _log_to_service_async("WARNING", f"Спроба оновити статус неіснуючого замовлення ID {order_id}.", log_details)
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Замовлення з ID {order_id} не знайдено.")

            # Оновлення статусу та часу оновлення
            update_values = {"status": new_status, "updated_at": datetime.datetime.utcnow()}
            update_query = orders_table.update().where(orders_table.c.id == order_id).values(**update_values)
            await database.execute(update_query)
        
        # Отримуємо оновлене замовлення для відповіді
        updated_order_response = await get_order_details_by_id(order_id)
        if not updated_order_response: # Малоймовірно, якщо оновлення пройшло
            await _log_to_service_async("ERROR", f"Не вдалося отримати замовлення {order_id} після оновлення статусу.", log_details)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка отримання оновленого замовлення.")

        # Надсилання сповіщення про зміну статусу (некритично, якщо не вдасться)
        await send_order_notification_async({
            "type": "email",
            "recipient": f"user_{updated_order_response.user_id}@example.com", # Замініть
            "template_id": "order_status_update",
            "context": {"order_id": order_id, "user_name": updated_order_response.user_id, "new_status": new_status}
        })
        
        await _log_to_service_async("INFO", f"Статус замовлення ID {order_id} успішно оновлено на '{new_status}'.", log_details)
        return updated_order_response
        
    except SQLAlchemyError as e:
        await _log_to_service_async("ERROR", f"Помилка SQLAlchemy при оновленні статусу замовлення {order_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service_async("CRITICAL", f"Непередбачена помилка при оновленні статусу замовлення {order_id}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.get("/health", summary="Перевірка стану сервісу замовлень")
async def health_check_endpoint():
    db_status = "disconnected"
    db_details = {}
    if database.is_connected: # Перевірка, чи database було успішно підключено
        try:
            # @db_breaker
            await database.execute(select(func.now())) # Простий запит для перевірки з'єднання
            db_status = "connected"
            # Можна додати перевірку кількості таблиць або іншу інформацію
            # tables = await database.fetch_all("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname != 'pg_catalog' AND schemaname != 'information_schema';")
            # db_details["tables_count"] = len(tables)
        except Exception as e:
            db_status = "error"
            db_details["error"] = str(e)
            logger.error(f"Health check: помилка PostgreSQL: {e}")
    
    dependencies_status = {}
    services_to_check = {
        "logging_service": LOGGING_SERVICE_URL,
        "cart_service": CART_SERVICE_URL,
        "notification_service": NOTIFICATION_SERVICE_URL
    }

    async def check_service_health(name: str, url: Optional[str]):
        if not url or not http_client: return "not_configured"
        try:
            response = await http_client.get(f"{url}/health", timeout=1.5)
            return "connected" if response.status_code == 200 else f"error_status_{response.status_code}"
        except (httpx.RequestError, httpx.TimeoutException):
            return "unreachable"
        except Exception:
            return "client_error"

    # Паралельна перевірка залежних сервісів
    results = await asyncio.gather(*(check_service_health(name, url) for name, url in services_to_check.items()))
    for i, name in enumerate(services_to_check.keys()):
        dependencies_status[name] = {"status": results[i], "url": services_to_check[name] or "N/A"}

    # Стани Circuit Breakers
    cb_states = {
        "logging_service_breaker": {"state": "CLOSED" if not log_service_breaker.opened else "OPEN", "failures": log_service_breaker.fail_counter},
        "cart_service_breaker": {"state": "CLOSED" if not cart_service_breaker.opened else "OPEN", "failures": cart_service_breaker.fail_counter},
        "notification_service_breaker": {"state": "CLOSED" if not notification_service_breaker.opened else "OPEN", "failures": notification_service_breaker.fail_counter},
        "database_breaker": {"state": "CLOSED" if not db_breaker.opened else "OPEN", "failures": db_breaker.fail_counter}
    }
    
    overall_status = "healthy"
    if db_status != "connected" or \
       any(dep["status"] != "connected" and dep["url"] != "N/A" for dep in dependencies_status.values()):
        overall_status = "degraded"

    return {
        "service_status": overall_status,
        "service_name": SERVICE_NAME,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "dependencies": {
            "postgresql_database": {"status": db_status, **db_details},
            **dependencies_status
        },
        "circuit_breakers": cb_states
    }
