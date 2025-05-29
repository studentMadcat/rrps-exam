from fastapi import FastAPI, HTTPException, Query, Path, Body, status
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure, DuplicateKeyError
from pydantic import BaseModel, Field, HttpUrl
from typing import List, Optional, Dict, Any, Union
import os
import httpx
import datetime
import logging
from prometheus_fastapi_instrumentator import Instrumentator # Для Prometheus
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError, retry_if_exception_type # Для Retry
import pybreaker # Для Circuit Breaker

logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__) # Логер для цього модуля

# --- Моделі Pydantic ---
class Price(BaseModel):
    amount: float = Field(..., gt=0, description="Сума ціни", example=129.99)
    currency: str = Field(..., min_length=3, max_length=3, description="Код валюти (ISO 4217)", example="UAH")

class Attribute(BaseModel):
    key: str = Field(..., description="Назва атрибуту", example="Колір")
    value: Union[str, int, float, bool] = Field(..., description="Значення атрибуту", example="Чорний")

class ProductBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Назва товару", example="Смартфон SuperX 12 Pro")
    description_short: Optional[str] = Field(None, max_length=500, description="Короткий опис товару", example="Потужний смартфон з неймовірною камерою.")
    description_long: Optional[str] = Field(None, description="Повний опис товару з усіма деталями.")
    price: Price = Field(..., description="Ціна товару")
    categories: List[str] = Field(default_factory=list, description="Список ID або назв категорій, до яких належить товар", example=["електроніка", "смартфони"])
    brand: Optional[str] = Field(None, max_length=100, description="Бренд товару", example="TechCorp")
    stock_quantity: int = Field(0, ge=0, description="Кількість товару на складі", example=150)
    images: List[HttpUrl] = Field(default_factory=list, description="Список URL зображень товару", example=["https://example.com/image1.jpg"])
    attributes: List[Attribute] = Field(default_factory=list, description="Список атрибутів товару")
    is_active: bool = Field(True, description="Чи активний товар (доступний для продажу)")
    tags: List[str] = Field(default_factory=list, description="Теги для пошуку та фільтрації", example=["новинка", "топ продажів"])

class ProductCreate(ProductBase):
    product_sku: str = Field(..., min_length=1, max_length=100, description="Унікальний артикул (SKU) товару", example="SKU-SUPERX-12PRO-BLK")

class ProductUpdate(BaseModel): # Модель для часткового оновлення
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Назва товару")
    description_short: Optional[str] = Field(None, max_length=500, description="Короткий опис товару")
    description_long: Optional[str] = Field(None, description="Повний опис товару")
    price: Optional[Price] = Field(None, description="Ціна товару")
    categories: Optional[List[str]] = Field(None, description="Список ID або назв категорій")
    brand: Optional[str] = Field(None, max_length=100, description="Бренд товару")
    stock_quantity: Optional[int] = Field(None, ge=0, description="Кількість товару на складі")
    images: Optional[List[HttpUrl]] = Field(None, description="Список URL зображень товару")
    attributes: Optional[List[Attribute]] = Field(None, description="Список атрибутів товару")
    is_active: Optional[bool] = Field(None, description="Чи активний товар")
    tags: Optional[List[str]] = Field(None, description="Теги для пошуку та фільтрації")

class ProductResponse(ProductBase):
    product_sku: str = Field(..., description="Унікальний артикул (SKU) товару")
    created_at: datetime.datetime = Field(..., description="Час створення запису")
    updated_at: datetime.datetime = Field(..., description="Час останнього оновлення запису")

    model_config = { # Раніше Config
        "json_schema_extra": { # Раніше schema_extra
            "example": {
                "product_sku": "SKU-SUPERX-12PRO-BLK",
                "name": "Смартфон SuperX 12 Pro",
                "description_short": "Потужний смартфон з неймовірною камерою.",
                "description_long": "Детальний опис...",
                "price": {"amount": 34999.99, "currency": "UAH"},
                "categories": ["електроніка", "смартфони"],
                "brand": "TechCorp",
                "stock_quantity": 135,
                "images": ["https://example.com/image1.jpg", "https://example.com/image2.jpg"],
                "attributes": [
                    {"key": "Колір", "value": "Космічний Чорний"},
                    {"key": "Пам'ять", "value": "256GB"}
                ],
                "is_active": True,
                "tags": ["флагман", "камерофон"],
                "created_at": "2024-05-30T10:00:00Z",
                "updated_at": "2024-05-30T12:30:00Z"
            }
        }
    }

# --- Глобальні змінні та конфігурація ---
app = FastAPI(
    title="Сервіс Каталогу Товарів",
    description="API для управління каталогом товарів інтернет-магазину.",
    version="1.1.0"
)

# Prometheus Instrumentator: автоматично додає метрики для FastAPI ендпоінтів
Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


# Конфігурація з змінних середовища
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://mongodb:27017/")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "product_db")
LOGGING_SERVICE_URL = os.getenv("LOGGING_SERVICE_URL") # Може бути None, якщо не налаштовано
SERVICE_NAME = "ProductCatalogService"
HTTP_CLIENT_TIMEOUT = float(os.getenv("HTTP_CLIENT_TIMEOUT", "5.0"))

# MongoDB клієнт (буде ініціалізований в startup_event)
mongo_client: Optional[MongoClient] = None
db: Optional[Any] = None # MongoClient.database returns Any
products_collection: Optional[Any] = None # Collection returns Any

# HTTP клієнт для відправки логів
# Ініціалізується в startup, закривається в shutdown
http_client_logger: Optional[httpx.AsyncClient] = None

# Circuit Breaker для критичних залежностей
# Для сервісу логування: 3 послідовні помилки відкривають CB на 30 секунд
log_service_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name=f"{SERVICE_NAME}_LoggingServiceBreaker")
# Для MongoDB операцій (приклад, можна налаштувати для конкретних операцій)
# mongodb_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60, name=f"{SERVICE_NAME}_MongoDBBreaker")


# --- Допоміжні функції ---

# Функція для логування з Retry та Circuit Breaker
@log_service_breaker
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5), # Затримка 1s, 2s, 4s (якщо max=5)
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)), # Повторювати при мережевих помилках/таймаутах
    reraise=True # Перевикидати помилку, якщо всі спроби невдалі
)
async def _log_to_service(level: str, message: str, details: Optional[Dict[str, Any]] = None):
    """Відправляє структурований лог до центрального сервісу логування."""
    if not LOGGING_SERVICE_URL or not http_client_logger:
        # Резервне логування в stdout, якщо сервіс логування не налаштований або клієнт не ініціалізований
        logger.warning(
            f"Резервне логування (сервіс логування недоступний/не налаштований): "
            f"Level: {level}, Service: {SERVICE_NAME}, Message: {message}, Details: {details or {}}"
        )
        return

    log_payload = {
        "service_name": SERVICE_NAME,
        "level": level.upper(),
        "message": message,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z", # Додаємо Z для UTC
        "details": details or {}
    }
    try:
        response = await http_client_logger.post(f"{LOGGING_SERVICE_URL}/logs", json=log_payload)
        response.raise_for_status()  # Викине помилку для 4xx/5xx статусів
        logger.debug(f"Лог успішно надіслано до сервісу: {message[:50]}...")
    except httpx.HTTPStatusError as e: # Обробка HTTP помилок від сервісу логування
        logger.error(
            f"HTTP помилка ({e.response.status_code}) при відправці логу до сервісу: {e.response.text}. "
            f"Оригінальний лог: Level: {level}, Message: {message}"
        )
        raise # Перевикидаємо для retry/circuit breaker
    except httpx.RequestError as e: # Обробка мережевих помилок httpx
        logger.error(
            f"Мережева помилка при відправці логу до сервісу: {str(e)}. "
            f"Оригінальний лог: Level: {level}, Message: {message}"
        )
        raise
    except Exception as e: # Інші неочікувані помилки
        logger.error(
            f"Неочікувана помилка при відправці логу до сервісу: {str(e)}. "
            f"Оригінальний лог: Level: {level}, Message: {message}"
        )
        raise


# --- Обробники подій FastAPI (startup, shutdown) ---
@app.on_event("startup")
async def startup_event():
    """Ініціалізація ресурсів при старті додатку."""
    global mongo_client, db, products_collection, http_client_logger
    
    http_client_logger = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)
    logger.info(f"HTTP клієнт для логування ініціалізовано з таймаутом {HTTP_CLIENT_TIMEOUT}s.")

    try:
        # Спроба підключення до MongoDB з retry логікою
        @retry(
            stop=stop_after_attempt(5), # 5 спроб
            wait=wait_exponential(multiplier=1, min=2, max=30), # Затримка 2s, 4s, 8s, 16s, 30s
            retry=retry_if_exception_type(ConnectionFailure),
            reraise=True
        )
        async def _connect_to_mongodb():
            logger.info(f"Спроба підключення до MongoDB: {MONGODB_URL}")
            mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000) # Таймаут вибору сервера
            mongo_client.admin.command('ping') # Перевірка з'єднання
            db = mongo_client[MONGODB_DATABASE]
            products_collection = db["products"]
            # Створення індексів (приклад, product_sku має бути унікальним)
            # `create_index` є ідемпотентним
            products_collection.create_index([("product_sku", ASCENDING)], unique=True, name="sku_unique_idx")
            products_collection.create_index([("name", "text"), ("description_short", "text")], name="name_desc_text_idx") # Для текстового пошуку
            products_collection.create_index([("categories", ASCENDING)], name="categories_idx")
            products_collection.create_index([("brand", ASCENDING)], name="brand_idx")
            products_collection.create_index([("price.amount", ASCENDING)], name="price_idx")
            products_collection.create_index([("is_active", ASCENDING)], name="active_idx")
            logger.info(f"Успішно підключено до MongoDB ({MONGODB_URL}) та бази даних '{MONGODB_DATABASE}'. Індекси перевірено/створено.")
        
        await _connect_to_mongodb()
        await _log_to_service("INFO", f"{SERVICE_NAME} успішно підключено до MongoDB.")
    except RetryError as e: # Якщо всі спроби підключення до MongoDB невдалі
        logger.critical(f"Критична помилка: не вдалося підключитися до MongoDB після кількох спроб: {e.last_attempt.exception()}. Сервіс може бути непрацездатним.")
        await _log_to_service("CRITICAL", f"Не вдалося підключитися до MongoDB при старті: {str(e.last_attempt.exception())}")
        # У цьому випадку сервіс, ймовірно, не зможе працювати. Можна або завершити роботу,
        # або дозволити запуститися, але ендпоінти будуть повертати помилки.
        # Для прототипу, дозволимо запуститися, але health check покаже проблему.
    except Exception as e: # Інші помилки при старті
        logger.critical(f"Неочікувана помилка при старті сервісу: {str(e)}", exc_info=True)
        await _log_to_service("CRITICAL", f"Неочікувана помилка при старті: {str(e)}")


@app.on_event("shutdown")
async def shutdown_event():
    """Закриття ресурсів при зупинці додатку."""
    if mongo_client:
        mongo_client.close()
        logger.info("З'єднання з MongoDB закрито.")
    if http_client_logger:
        await http_client_logger.aclose()
        logger.info("HTTP клієнт для логування закрито.")
    
    try:
        # Спроба надіслати фінальний лог
        await _log_to_service("INFO", f"{SERVICE_NAME} зупинено.")
    except (pybreaker.CircuitBreakerError, RetryError) as e:
        logger.warning(f"Не вдалося надіслати фінальний лог до сервісу логування при зупинці: {str(e)}")
    except Exception as e:
        logger.error(f"Неочікувана помилка при відправці фінального логу: {str(e)}")


# --- Ендпоінти API ---
@app.post(
    "/products", 
    response_model=ProductResponse, 
    status_code=status.HTTP_201_CREATED, 
    summary="Створити новий товар",
    description="Створює новий товар в каталозі. Поле `product_sku` має бути унікальним."
)
async def create_product_endpoint(product_data: ProductCreate = Body(..., description="Дані нового товару")):
    """
    Створює новий товар.
    - `product_sku` має бути унікальним.
    - Автоматично додає `created_at` та `updated_at`.
    """
    if not products_collection:
        await _log_to_service("ERROR", "Спроба створити товар, але колекція MongoDB не ініціалізована.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс бази даних тимчасово недоступний.")

    current_time = datetime.datetime.utcnow()
    product_to_insert = product_data.model_dump()
    product_to_insert["created_at"] = current_time
    product_to_insert["updated_at"] = current_time

    try:
        # @mongodb_breaker # Можна обгорнути окремі операції з БД
        result = products_collection.insert_one(product_to_insert)
        # Отримуємо вставлений документ для відповіді
        created_doc = products_collection.find_one({"_id": result.inserted_id})
        if created_doc:
            await _log_to_service("INFO", f"Товар '{product_data.name}' (SKU: {product_data.product_sku}) успішно створено.", {"product_sku": product_data.product_sku, "inserted_id": str(result.inserted_id)})
            return ProductResponse(**created_doc)
        else: # Малоймовірно, якщо insert_one не кинув помилку
            await _log_to_service("ERROR", "Не вдалося знайти товар після вставки в БД.", {"product_sku": product_data.product_sku})
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка створення товару: не вдалося отримати створений запис.")
    except DuplicateKeyError: # Обробка порушення унікальності SKU
        await _log_to_service("WARNING", f"Спроба створити товар з існуючим SKU: {product_data.product_sku}", {"product_sku": product_data.product_sku})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Товар з SKU '{product_data.product_sku}' вже існує.")
    except OperationFailure as e: # Інші помилки операцій MongoDB
        await _log_to_service("ERROR", f"Помилка операції MongoDB при створенні товару {product_data.product_sku}: {str(e)}", {"product_sku": product_data.product_sku, "mongo_error_code": e.code})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Помилка бази даних: {str(e)}")
    except (pybreaker.CircuitBreakerError, RetryError) as log_e: # Помилки логування
        logger.error(f"Проблема з сервісом логування при створенні товару: {str(log_e)}. Операція з товаром продовжена.")
        # Тут ми вирішили продовжити, навіть якщо логування не вдалося, але можна змінити логіку
        # Повторно отримуємо документ, оскільки логування могло перервати потік до повернення
        created_doc_after_log_fail = products_collection.find_one({"product_sku": product_data.product_sku})
        if created_doc_after_log_fail:
            return ProductResponse(**created_doc_after_log_fail)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка створення товару (проблема з логуванням).") # Якщо товар не знайдено
    except Exception as e: # Інші непередбачені помилки
        await _log_to_service("CRITICAL", f"Непередбачена помилка при створенні товару {product_data.product_sku}: {str(e)}", {"product_sku": product_data.product_sku}, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.get(
    "/products", 
    response_model=List[ProductResponse], 
    summary="Отримати список товарів з фільтрацією та пагінацією"
)
async def get_products_list_endpoint(
    search: Optional[str] = Query(None, description="Текст для повнотекстового пошуку (по назві та опису). Потрібен текстовий індекс в MongoDB."),
    category: Optional[str] = Query(None, description="Фільтрація за назвою категорії (точне співпадіння)."),
    brand: Optional[str] = Query(None, description="Фільтрація за брендом (точне співпадіння)."),
    min_price: Optional[float] = Query(None, ge=0, description="Мінімальна ціна (сума)."),
    max_price: Optional[float] = Query(None, description="Максимальна ціна (сума)."),
    is_active: Optional[bool] = Query(None, description="Фільтрація за статусом активності товару."),
    tags: Optional[List[str]] = Query(None, description="Фільтрація за тегами (товар має містити всі вказані теги)."),
    sort_by: Optional[str] = Query("created_at_desc", description="Поле для сортування та напрямок. Формат: 'поле_asc' або 'поле_desc'. Доступні поля: name, price, created_at, stock_quantity.", example="price_asc"),
    page: int = Query(1, ge=1, description="Номер сторінки для пагінації."),
    limit: int = Query(10, ge=1, le=100, description="Кількість товарів на сторінці.")
):
    if not products_collection:
        await _log_to_service("ERROR", "Спроба отримати список товарів, але колекція MongoDB не ініціалізована.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс бази даних тимчасово недоступний.")

    query_filter: Dict[str, Any] = {}
    if search:
        query_filter["$text"] = {"$search": search} # Використовуємо текстовий індекс
    if category:
        query_filter["categories"] = category # Простий пошук за наявністю категорії в масиві
    if brand:
        query_filter["brand"] = brand
    
    price_query = {}
    if min_price is not None:
        price_query["$gte"] = min_price
    if max_price is not None:
        price_query["$lte"] = max_price
    if price_query:
        query_filter["price.amount"] = price_query

    if is_active is not None:
        query_filter["is_active"] = is_active
    
    if tags:
        query_filter["tags"] = {"$all": tags} # Товар має містити всі теги зі списку

    # Сортування
    sort_field_map = {
        "name": "name",
        "price": "price.amount",
        "created_at": "created_at",
        "stock_quantity": "stock_quantity"
    }
    sort_order = DESCENDING
    sort_key = "created_at" # За замовчуванням

    if sort_by:
        parts = sort_by.lower().rsplit('_', 1)
        field_name = parts[0]
        if field_name in sort_field_map:
            sort_key = sort_field_map[field_name]
            if len(parts) == 2 and parts[1] == "asc":
                sort_order = ASCENDING
    
    sort_criteria = [(sort_key, sort_order)]
    if sort_key != "created_at": # Додаємо сортування за _id для стабільності, якщо не сортуємо за унікальним полем
        sort_criteria.append(("_id", DESCENDING if sort_order == DESCENDING else ASCENDING))


    skip_amount = (page - 1) * limit
    
    log_details_query = {
        "filter": query_filter, "sort": sort_criteria, "skip": skip_amount, "limit": limit, "page": page
    }
    await _log_to_service("DEBUG", "Запит списку товарів.", log_details_query)

    try:
        # @mongodb_breaker
        cursor = products_collection.find(query_filter).sort(sort_criteria).skip(skip_amount).limit(limit)
        # Оцінка загальної кількості документів (може бути повільною на великих колекціях без правильних індексів)
        # total_documents = products_collection.count_documents(query_filter) # Розкоментувати, якщо потрібна загальна кількість
        
        result_list = [ProductResponse(**product) for product in cursor]
        await _log_to_service("INFO", f"Успішно отримано {len(result_list)} товарів.", {"retrieved_count": len(result_list), **log_details_query})
        # Можна додати заголовок X-Total-Count, якщо total_documents розраховується
        return result_list
    except OperationFailure as e:
        await _log_to_service("ERROR", f"Помилка операції MongoDB при отриманні списку товарів: {str(e)}", {"mongo_error_code": e.code, **log_details_query})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service("CRITICAL", f"Непередбачена помилка при отриманні списку товарів: {str(e)}", log_details_query, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.get(
    "/products/{product_sku}", 
    response_model=ProductResponse, 
    summary="Отримати товар за його SKU",
    responses={404: {"description": "Товар не знайдено"}}
)
async def get_product_by_sku_endpoint(product_sku: str = Path(..., description="SKU товару для пошуку", example="SKU-SUPERX-12PRO-BLK")):
    if not products_collection:
        await _log_to_service("ERROR", f"Спроба отримати товар {product_sku}, але колекція MongoDB не ініціалізована.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс бази даних тимчасово недоступний.")
    
    await _log_to_service("DEBUG", f"Запит товару за SKU: {product_sku}.", {"product_sku": product_sku})
    try:
        # @mongodb_breaker
        product = products_collection.find_one({"product_sku": product_sku})
        if product:
            await _log_to_service("INFO", f"Товар з SKU '{product_sku}' успішно знайдено.", {"product_sku": product_sku})
            return ProductResponse(**product)
        else:
            await _log_to_service("WARNING", f"Товар з SKU '{product_sku}' не знайдено.", {"product_sku": product_sku})
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар з SKU '{product_sku}' не знайдено.")
    except OperationFailure as e:
        await _log_to_service("ERROR", f"Помилка операції MongoDB при отриманні товару {product_sku}: {str(e)}", {"product_sku": product_sku, "mongo_error_code": e.code})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service("CRITICAL", f"Непередбачена помилка при отриманні товару {product_sku}: {str(e)}", {"product_sku": product_sku}, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.put(
    "/products/{product_sku}", 
    response_model=ProductResponse, 
    summary="Оновити існуючий товар (часткове оновлення)",
    responses={404: {"description": "Товар не знайдено"}}
)
async def update_product_endpoint(
    product_sku: str = Path(..., description="SKU товару для оновлення"), 
    product_update_data: ProductUpdate = Body(..., description="Дані для оновлення товару (тільки поля, що змінюються)")
):
    if not products_collection:
        await _log_to_service("ERROR", f"Спроба оновити товар {product_sku}, але колекція MongoDB не ініціалізована.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс бази даних тимчасово недоступний.")

    # model_dump(exclude_unset=True) гарантує, що тільки передані поля будуть використані для оновлення
    update_data = product_update_data.model_dump(exclude_unset=True)
    if not update_data: # Якщо тіло запиту порожнє або всі поля None
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Не надано даних для оновлення.")

    update_data["updated_at"] = datetime.datetime.utcnow()
    
    log_details = {"product_sku": product_sku, "updated_fields": list(update_data.keys())}
    await _log_to_service("DEBUG", f"Спроба оновити товар {product_sku}.", log_details)

    try:
        # @mongodb_breaker
        result = products_collection.update_one(
            {"product_sku": product_sku}, 
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            await _log_to_service("WARNING", f"Спроба оновити неіснуючий товар: {product_sku}.", log_details)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар з SKU '{product_sku}' не знайдено для оновлення.")
        
        if result.modified_count == 0 and result.matched_count == 1:
            # Товар знайдено, але дані не змінилися (можливо, передані ті ж самі значення)
            await _log_to_service("INFO", f"Товар {product_sku} знайдено, але дані для оновлення не змінили запис.", log_details)
            # Повертаємо поточний стан товару
            current_product_doc = products_collection.find_one({"product_sku": product_sku})
            if current_product_doc:
                 return ProductResponse(**current_product_doc)
            else: # Малоймовірно, якщо matched_count == 1
                 raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар з SKU '{product_sku}' не знайдено після спроби оновлення без змін.")


        updated_product_doc = products_collection.find_one({"product_sku": product_sku})
        if updated_product_doc:
            await _log_to_service("INFO", f"Товар {product_sku} успішно оновлено.", log_details)
            return ProductResponse(**updated_product_doc)
        else: # Малоймовірно, якщо оновлення пройшло
            await _log_to_service("ERROR", f"Не вдалося отримати оновлений товар {product_sku} після запису в БД.", log_details)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Помилка отримання оновленого товару.")
            
    except OperationFailure as e:
        await _log_to_service("ERROR", f"Помилка операції MongoDB при оновленні товару {product_sku}: {str(e)}", {**log_details, "mongo_error_code": e.code})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service("CRITICAL", f"Непередбачена помилка при оновленні товару {product_sku}: {str(e)}", log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.delete(
    "/products/{product_sku}", 
    status_code=status.HTTP_204_NO_CONTENT, 
    summary="Видалити товар за SKU",
    responses={404: {"description": "Товар не знайдено"}}
)
async def delete_product_endpoint(product_sku: str = Path(..., description="SKU товару для видалення")):
    if not products_collection:
        await _log_to_service("ERROR", f"Спроба видалити товар {product_sku}, але колекція MongoDB не ініціалізована.")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Сервіс бази даних тимчасово недоступний.")

    await _log_to_service("INFO", f"Спроба видалити товар з SKU: {product_sku}.", {"product_sku": product_sku})
    try:
        # @mongodb_breaker
        result = products_collection.delete_one({"product_sku": product_sku})
        if result.deleted_count == 0:
            await _log_to_service("WARNING", f"Спроба видалити неіснуючий товар: {product_sku}.", {"product_sku": product_sku})
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Товар з SKU '{product_sku}' не знайдено.")
        
        await _log_to_service("INFO", f"Товар з SKU '{product_sku}' успішно видалено.", {"product_sku": product_sku, "deleted_count": result.deleted_count})
        # Для статусу 204 відповідь не повинна мати тіла
        return 
    except OperationFailure as e:
        await _log_to_service("ERROR", f"Помилка операції MongoDB при видаленні товару {product_sku}: {str(e)}", {"product_sku": product_sku, "mongo_error_code": e.code})
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Помилка бази даних: {str(e)}")
    except Exception as e:
        await _log_to_service("CRITICAL", f"Непередбачена помилка при видаленні товару {product_sku}: {str(e)}", {"product_sku": product_sku}, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Внутрішня помилка сервера: {str(e)}")


@app.get("/health", summary="Перевірка стану сервісу каталогу товарів")
async def health_check_endpoint():
    """
    Перевіряє стан сервісу та його залежностей (MongoDB, Сервіс Логування).
    """
    db_status = "disconnected"
    db_details = {}
    if mongo_client:
        try:
            # Більш надійна перевірка - спробувати виконати просту операцію
            db_server_info = mongo_client.server_info() # Отримує інформацію про сервер
            db_details["mongo_version"] = db_server_info.get("version")
            db_details["collections_count"] = len(db.list_collection_names()) if db else "N/A"
            db_status = "connected"
        except ConnectionFailure as e:
            db_status = "connection_failure"
            db_details["error"] = str(e)
            logger.error(f"Health check: помилка підключення до MongoDB: {e}")
        except Exception as e:
            db_status = "error"
            db_details["error"] = str(e)
            logger.error(f"Health check: неочікувана помилка MongoDB: {e}")

    logging_service_status = "unknown"
    if LOGGING_SERVICE_URL and http_client_logger:
        try:
            response = await http_client_logger.get(f"{LOGGING_SERVICE_URL}/health", timeout=1.5)
            if response.status_code == status.HTTP_200_OK:
                logging_service_status = "connected"
            else:
                logging_service_status = f"error_status_{response.status_code}"
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logging_service_status = "unreachable"
            logger.warning(f"Health check: сервіс логування недоступний: {str(e)}")
        except Exception as e: # Обробка інших можливих помилок httpx
            logging_service_status = "client_error"
            logger.warning(f"Health check: помилка клієнта при запиті до сервісу логування: {str(e)}")


    log_breaker_state = "CLOSED" if not log_service_breaker.opened else "OPEN"
    
    overall_status = "healthy"
    if db_status != "connected" or (LOGGING_SERVICE_URL and logging_service_status != "connected"):
        overall_status = "degraded" # Або "unhealthy", якщо залежності критичні

    return {
        "service_status": overall_status,
        "service_name": SERVICE_NAME,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "dependencies": {
            "mongodb": {"status": db_status, **db_details},
            "logging_service": {"status": logging_service_status, "url": LOGGING_SERVICE_URL or "N/A"}
        },
        "circuit_breakers": {
            "logging_service_breaker": {"state": log_breaker_state, "failures": log_service_breaker.fail_counter, "max_failures": log_service_breaker.fail_max}
        }
    }