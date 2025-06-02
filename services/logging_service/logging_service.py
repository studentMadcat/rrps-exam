# logging_service/main.py
from fastapi import FastAPI, HTTPException, Body, Query
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
import json
import datetime
import os
import logging # Стандартне логування Python для самого сервісу логування
from prometheus_fastapi_instrumentator import Instrumentator # Додаємо Prometheus

# Налаштування логування для самого сервісу логування
# Ці логи будуть йти в stdout/stderr контейнера або куди налаштовано Docker/Kubernetes
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
service_logger = logging.getLogger(__name__)

app = FastAPI(
    title="Централізований Сервіс Логування",
    description="Приймає та зберігає (імітація) логи від інших мікросервісів.",
    version="1.0.0"
)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# Модель для вхідного запису логу
class LogEntryIn(BaseModel):
    service_name: str = Field(..., description="Назва сервісу, що генерує лог")
    level: str = Field(default="INFO", description="Рівень логування (INFO, ERROR, DEBUG, WARNING, CRITICAL)")
    message: str = Field(..., description="Повідомлення логу")
    timestamp: Optional[datetime.datetime] = Field(None, description="Часова мітка логу (якщо не надано, буде встановлено поточний час)")
    details: Optional[Dict[str, Any]] = Field(None, description="Додаткові деталі у форматі JSON")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "service_name": "OrderService",
                    "level": "INFO",
                    "message": "Замовлення #123 успішно створено.",
                    "timestamp": "2024-05-30T12:00:00Z",
                    "details": {"order_id": 123, "user_id": "user_abc"}
                }
            ]
        }
    }


app = FastAPI(
    title="Централізований Сервіс Логування",
    description="Приймає та зберігає (імітація) логи від інших мікросервісів.",
    version="1.0.0"
)

# Шлях до файлу логів (для прототипу)
LOG_FILE_PATH = os.getenv("APP_LOG_FILE", "application_events.log")
ENABLE_FILE_LOGGING = os.getenv("ENABLE_FILE_LOGGING", "true").lower() == "true"

@app.on_event("startup")
async def startup_event():
    service_logger.info(f"Сервіс логування запущено. Запис у файл '{LOG_FILE_PATH}' {'включено' if ENABLE_FILE_LOGGING else 'вимкнено'}.")

def _format_log_line(log_entry: LogEntryIn) -> str:
    """Форматує запис логу в рядок."""
    # Встановлюємо timestamp, якщо не надано
    ts = log_entry.timestamp or datetime.datetime.utcnow()
    # Форматуємо timestamp у стандартний вигляд ISO, але без мікросекунд для кращої читабельності в файлі
    formatted_ts = ts.strftime('%Y-%m-%dT%H:%M:%S') + f".{ts.microsecond // 1000:03d}Z"


    log_level_upper = log_entry.level.upper()
    
    # Базовий рядок логу
    log_parts = [
        f"[{formatted_ts}]",
        f"[{log_level_upper}]",
        f"[{log_entry.service_name}]",
        f"- {log_entry.message}"
    ]
    
    # Додаємо деталі, якщо вони є
    if log_entry.details:
        # Конвертуємо деталі в рядок JSON для компактності та структурованості
        try:
            details_str = json.dumps(log_entry.details, ensure_ascii=False, sort_keys=True)
            log_parts.append(f"| Details: {details_str}")
        except TypeError: # Якщо деталі не серіалізуються в JSON
            log_parts.append(f"| Details (raw): {log_entry.details}")
            
    return " ".join(log_parts)

async def store_log_entry_async(log_entry: LogEntryIn):
    """
    Асинхронно "зберігає" запис логу.
    """
    log_line = _format_log_line(log_entry)
    
    # Вивід в консоль самого сервісу логування (це буде видно в логах контейнера)
    # Рівень логування тут може залежати від рівня вхідного повідомлення
    if log_entry.level.upper() == "ERROR" or log_entry.level.upper() == "CRITICAL":
        service_logger.error(f"LOG_RECEIVED: {log_line}")
    elif log_entry.level.upper() == "WARNING":
        service_logger.warning(f"LOG_RECEIVED: {log_line}")
    else:
        service_logger.info(f"LOG_RECEIVED: {log_line}") # INFO, DEBUG etc.
    
    # Запис у файл (якщо включено)
    if ENABLE_FILE_LOGGING:
        try:
            with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")
        except Exception as e:
            service_logger.error(f"Не вдалося записати лог у файл '{LOG_FILE_PATH}': {e}")


@app.post("/logs", status_code=202, summary="Прийняти та зберегти запис логу")
async def receive_log_entry_endpoint(log_entry: LogEntryIn = Body(...)):
    try:
        # Встановлюємо timestamp, якщо він не був переданий
        if log_entry.timestamp is None:
            log_entry.timestamp = datetime.datetime.utcnow()
            
        await store_log_entry_async(log_entry)
        return {"status": "Лог успішно прийнято"}
    except Exception as e:
        # Логуємо помилку самого сервісу логування
        service_logger.critical(f"Критична помилка при обробці вхідного запису логу: {e}", exc_info=True)
        # Не кидаємо HTTPException, щоб не переривати сервіси, що логують,
        # але це означає, що відправник не дізнається про проблему тут.
        # Можна повернути 500, якщо це критично.
        # raise HTTPException(status_code=500, detail=f"Внутрішня помилка сервісу логування: {str(e)}")
        return {"status": "Помилка обробки логу на сервері логування", "error_detail": str(e)}


@app.get("/health", summary="Перевірка стану сервісу логування")
async def health_check_endpoint():
    # Тут можна додати перевірку доступності файлу логів або іншого сховища, якщо воно використовується.
    # Наприклад, перевірити, чи можна записати у файл.
    file_writable_status = "not_applicable"
    if ENABLE_FILE_LOGGING:
        try:
            with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(f"HEALTH_CHECK_PING at {datetime.datetime.utcnow().isoformat()}\n")
            file_writable_status = "writable"
        except Exception as e:
            file_writable_status = f"error_writing: {str(e)}"
            service_logger.error(f"Health check: не вдалося записати у файл логів {LOG_FILE_PATH}: {e}")

    service_logger.debug("Запит перевірки стану сервісу логування.")
    return {"status": "healthy", "file_logging_enabled": True, "log_file_status": "writable"}


@app.get("/logs", summary="Отримати останні N записів логів (для демонстрації)")
async def get_logs(last_n: int = Query(100, ge=1, le=1000)):
    if not ENABLE_FILE_LOGGING or not os.path.exists(LOG_FILE_PATH):
        raise HTTPException(status_code=404, detail="Файл логів не знайдено або запис у файл вимкнено.")
    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return {"logs": lines[-last_n:]}
    except Exception as e:
        service_logger.error(f"Помилка при читанні файлу логів {LOG_FILE_PATH}: {e}")
        raise HTTPException(status_code=500, detail="Не вдалося прочитати логи.")
