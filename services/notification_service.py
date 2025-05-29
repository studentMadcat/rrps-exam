from fastapi import FastAPI, HTTPException, Body, status
from pydantic import BaseModel, EmailStr, Field, validator
from typing import Dict, Any, Optional, Literal, Tuple
import os
import logging
import httpx # Для відправки логів та, можливо, SMS
import datetime
from prometheus_fastapi_instrumentator import Instrumentator
from tenacity import retry, stop_after_attempt, wait_exponential, RetryError, retry_if_exception_type
import pybreaker
import smtplib # Для імітації відправки email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import asyncio

# --- Налаштування логування ---
# Логи цього сервісу будуть йти в stdout/stderr,
# а також він буде надсилати свої операційні логи до центрального Сервісу Логування.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__) # Логер для внутрішніх потреб цього сервісу

# --- Моделі Pydantic ---
class NotificationPayload(BaseModel):
    type: Literal['email', 'sms'] = Field(..., description="Тип сповіщення: 'email' або 'sms'")
    recipient: str = Field(..., description="Отримувач: email-адреса для 'email', номер телефону для 'sms'")
    template_id: Optional[str] = Field(None, description="Ідентифікатор шаблону для повідомлення (наприклад, 'order_confirmation', 'password_reset')", example="order_confirmation")
    context: Dict[str, Any] = Field(default_factory=dict, description="Дані для заповнення шаблону (ключ-значення)", example={"order_id": "123XYZ", "user_name": "Іван"})
    subject: Optional[str] = Field(None, description="Тема повідомлення (обов'язково для email, якщо не використовується шаблон з темою)", example="Ваше замовлення підтверджено!")
    body: Optional[str] = Field(None, description="Тіло повідомлення (використовується, якщо немає template_id або шаблон не знайдено)")

    @validator('recipient')
    def recipient_validator(cls, v, values):
        type_ = values.get('type')
        if type_ == 'email':
            # Pydantic автоматично валідує EmailStr, якщо поле recipient має тип EmailStr
            # Тут можна додати додаткову валідацію, якщо потрібно, але для EmailStr це вже є.
            # Для прикладу, перевіримо, чи це не порожній рядок після EmailStr валідації.
            if not v:
                raise ValueError("Email отримувача не може бути порожнім")
        elif type_ == 'sms':
            # Проста валідація номера телефону (приклад, потребує покращення для реальних умов)
            if not v.isdigit() or not (10 <= len(v) <= 15):
                raise ValueError("Номер телефону для SMS має складатися з цифр та мати довжину від 10 до 15 символів.")
        return v

class NotificationResponse(BaseModel):
    message_id: Optional[str] = Field(None, description="Ідентифікатор надісланого повідомлення (від зовнішнього сервісу)")
    status: str = Field(..., description="Статус операції відправки", example="accepted") # accepted, failed, simulated
    recipient: str
    type: str
    details: Optional[str] = None


# --- Глобальні змінні та конфігурація ---
app = FastAPI(
    title="Сервіс Нотифікацій",
    description="Відповідає за надсилання email та SMS сповіщень.",
    version="1.1.0"
)
Instrumentator(
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# Конфігурація
LOGGING_SERVICE_URL = os.getenv("LOGGING_SERVICE_URL")
SERVICE_NAME = "NotificationService"
HTTP_CLIENT_TIMEOUT = float(os.getenv("HTTP_CLIENT_TIMEOUT", "10.0")) # Більший таймаут для зовнішніх сервісів

# SMTP конфігурація (приклади, мають бути в змінних середовища)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.example.com") # Замініть на ваш SMTP хост
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER") # Ваш SMTP логін
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") # Ваш SMTP пароль
SMTP_SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL", f"noreply@{os.getenv('APP_DOMAIN', 'ecommerce.local')}")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

# SMS Gateway конфігурація (приклади)
SMS_GATEWAY_URL = os.getenv("SMS_GATEWAY_URL") # Наприклад, "https://api.smsgateway.com/send"
SMS_API_KEY = os.getenv("SMS_API_KEY")
SMS_SENDER_ID = os.getenv("SMS_SENDER_ID", "MyShop")

# HTTP клієнт
http_client: Optional[httpx.AsyncClient] = None

# Circuit Breakers
log_service_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=30, name=f"{SERVICE_NAME}_LoggingServiceBreaker")
smtp_breaker = pybreaker.CircuitBreaker(fail_max=2, reset_timeout=120, name=f"{SERVICE_NAME}_SMTPSenderBreaker") # CB для SMTP
sms_gateway_breaker = pybreaker.CircuitBreaker(fail_max=3, reset_timeout=90, name=f"{SERVICE_NAME}_SMSGatewayBreaker") # CB для SMS

# --- Допоміжні функції ---
@log_service_breaker
@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=1, min=0.5, max=3),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
async def _log_to_central_service(level: str, message: str, details: Optional[Dict[str, Any]] = None):
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
        logger.error(f"Не вдалося надіслати лог до центрального сервісу: {str(e)}. Оригінальний лог: {log_payload}")
        raise # Перевикидаємо для спрацювання CB/Retry на цю функцію

def _render_template(template_id: str, context: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Імітація рендерингу шаблону. Повертає (subject, body).
    У реальному додатку тут буде логіка роботи з шаблонізатором (напр., Jinja2).
    """
    subject, body = None, None
    user_name = context.get("user_name", "Клієнт")
    order_id = context.get("order_id")

    if template_id == "order_confirmation":
        subject = f"Підтвердження замовлення #{order_id}"
        body = (
            f"Шановний(а) {user_name},\n\n"
            f"Дякуємо за ваше замовлення #{order_id} в нашому магазині!\n"
            f"Загальна сума: {context.get('total_amount', 'N/A')}.\n"
            f"Деталі доставки: {context.get('shipping_address', 'N/A')}\n\n"
            f"З повагою,\nКоманда магазину."
        )
    elif template_id == "order_status_update":
        subject = f"Статус вашого замовлення #{order_id} оновлено"
        body = (
            f"Шановний(а) {user_name},\n\n"
            f"Статус вашого замовлення #{order_id} було змінено на: '{context.get('new_status', 'невідомий')}'.\n"
            f"Ви можете перевірити деталі замовлення в особистому кабінеті.\n\n"
            f"Дякуємо, що обрали нас!"
        )
    elif template_id == "password_reset_request":
        subject = "Запит на скидання паролю"
        reset_link = context.get("reset_link", "#")
        body = (
            f"Привіт, {user_name}!\n\n"
            f"Ми отримали запит на скидання паролю для вашого облікового запису.\n"
            f"Якщо це були ви, перейдіть за посиланням: {reset_link}\n"
            f"Якщо ви не робили цього запиту, просто проігноруйте цей лист.\n\n"
            f"З найкращими побажаннями."
        )
    else:
        logger.warning(f"Спроба використати невідомий шаблон: {template_id}")
        # Якщо шаблон не знайдено, можемо повернути None, щоб використати payload.body/subject
    return subject, body


@smtp_breaker
@retry(
    stop=stop_after_attempt(2), # Менше спроб для email, щоб не спамити
    wait=wait_exponential(multiplier=2, min=5, max=60), # Довші затримки
    retry=retry_if_exception_type(smtplib.SMTPException), # Повторювати при помилках SMTP
    reraise=True
)
async def execute_send_email(recipient: EmailStr, subject: str, html_body: str):
    """Реальна (або імітована) відправка email."""
    log_details = {"recipient": recipient, "subject": subject}
    await _log_to_central_service("DEBUG", "Спроба відправки email.", log_details)

    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SENDER_EMAIL]):
        logger.warning(f"SMTP не налаштовано повністю. Імітація відправки email до {recipient}.")
        print(f"--- ІМІТАЦІЯ EMAIL ---\nДо: {recipient}\nТема: {subject}\nТіло (HTML):\n{html_body[:200]}...\n--- КІНЕЦЬ ІМІТАЦІЇ ---")
        # Успішна імітація
        await _log_to_central_service("INFO", "Email успішно імітовано (SMTP не налаштовано).", log_details)
        return {"message_id": f"simulated_email_{datetime.datetime.utcnow().timestamp()}"}

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_SENDER_EMAIL
    msg['To'] = recipient
    
    # Додаємо HTML тіло
    part_html = MIMEText(html_body, 'html', 'utf-8')
    msg.attach(part_html)

    try:
        # Використовуємо asyncio.to_thread для запуску синхронного smtplib в окремому потоці,
        # щоб не блокувати event loop FastAPI.
        # Для повністю асинхронного рішення краще використовувати aiosmtplib.
        def _send_sync():
            if SMTP_USE_TLS:
                server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
                server.starttls()
            else: # Можливо SSL на іншому порті, або без шифрування (не рекомендовано)
                server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_SENDER_EMAIL, [recipient], msg.as_string())
            server.quit()
        
        await asyncio.to_thread(_send_sync) # Запуск синхронного коду в потоці
        
        await _log_to_central_service("INFO", f"Email успішно надіслано до {recipient}.", log_details)
        return {"message_id": f"smtp_sent_{datetime.datetime.utcnow().timestamp()}"} # Приклад ID
    except smtplib.SMTPException as e:
        await _log_to_central_service("ERROR", f"Помилка SMTP при надсиланні email до {recipient}: {str(e)}", log_details)
        raise # Перевикидаємо для retry/CB
    except Exception as e:
        await _log_to_central_service("ERROR", f"Неочікувана помилка при надсиланні email до {recipient}: {str(e)}", log_details, exc_info=True)
        raise smtplib.SMTPException(f"Неочікувана помилка: {str(e)}") # Перетворюємо на SMTPException для retry


@sms_gateway_breaker
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1.5, min=2, max=20),
    retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException)),
    reraise=True
)
async def execute_send_sms(recipient_phone: str, message_body: str) -> Dict[str, Any]:
    """Реальна (або імітована) відправка SMS через HTTP шлюз."""
    log_details = {"recipient_phone": recipient_phone, "message_length": len(message_body)}
    await _log_to_central_service("DEBUG", "Спроба відправки SMS.", log_details)

    if not SMS_GATEWAY_URL or not SMS_API_KEY or not http_client:
        logger.warning(f"SMS шлюз не налаштовано. Імітація відправки SMS до {recipient_phone}.")
        print(f"--- ІМІТАЦІЯ SMS ---\nДо: {recipient_phone}\nПовідомлення: {message_body}\n--- КІНЕЦЬ ІМІТАЦІЇ ---")
        await _log_to_central_service("INFO", "SMS успішно імітовано (шлюз не налаштовано).", log_details)
        return {"message_id": f"simulated_sms_{datetime.datetime.utcnow().timestamp()}", "status_detail": "Simulated"}

    payload = {
        "api_key": SMS_API_KEY,
        "to": recipient_phone,
        "from": SMS_SENDER_ID,
        "text": message_body
    }
    try:
        response = await http_client.post(SMS_GATEWAY_URL, json=payload) # Або data=payload, залежно від API шлюзу
        response.raise_for_status()
        # Припускаємо, що відповідь шлюзу містить message_id
        response_data = response.json()
        message_id = response_data.get("messageId", f"sms_sent_{datetime.datetime.utcnow().timestamp()}")
        await _log_to_central_service("INFO", f"SMS успішно надіслано до {recipient_phone}.", {**log_details, "gateway_response": response_data})
        return {"message_id": message_id, "status_detail": response_data.get("status", "Sent")}
    except httpx.HTTPStatusError as e:
        error_body = e.response.text
        await _log_to_central_service("ERROR", f"HTTP помилка ({e.response.status_code}) від SMS шлюзу: {error_body}", {**log_details, "error": error_body})
        raise
    except httpx.RequestError as e:
        await _log_to_central_service("ERROR", f"Мережева помилка при відправці SMS: {str(e)}", {**log_details, "error": str(e)})
        raise
    except Exception as e:
        await _log_to_central_service("ERROR", f"Неочікувана помилка при відправці SMS: {str(e)}", {**log_details, "error": str(e)}, exc_info=True)
        raise Exception(f"Неочікувана помилка SMS: {str(e)}") # Перетворюємо для retry


# --- Обробники подій FastAPI ---
@app.on_event("startup")
async def startup_event_async():
    global http_client
    logger.info(f"Запуск сервісу '{SERVICE_NAME}'...")
    http_client = httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT)
    await _log_to_central_service("INFO", f"Сервіс '{SERVICE_NAME}' запущено.")

@app.on_event("shutdown")
async def shutdown_event_async():
    if http_client:
        await http_client.aclose()
    logger.info(f"Сервіс '{SERVICE_NAME}' зупиняється...")
    try:
        await _log_to_central_service("INFO", f"Сервіс '{SERVICE_NAME}' зупинено.")
    except Exception as e:
        logger.warning(f"Не вдалося надіслати фінальний лог при зупинці: {str(e)}")

# --- Ендпоінти API ---
@app.post(
    "/notifications/send", 
    response_model=NotificationResponse, 
    status_code=status.HTTP_202_ACCEPTED, # 202 Accepted, оскільки відправка може бути асинхронною
    summary="Надіслати сповіщення (Email або SMS)"
)
async def handle_send_notification_endpoint(payload: NotificationPayload = Body(...)):
    """
    Приймає запит на надсилання сповіщення.
    Визначає тип (email/sms) та намагається надіслати.
    Використовує шаблони, якщо вказано `template_id`.
    """
    log_details = {"type": payload.type, "recipient": payload.recipient, "template_id": payload.template_id}
    await _log_to_central_service("INFO", "Отримано запит на надсилання сповіщення.", log_details)

    final_subject = payload.subject
    final_body = payload.body

    if payload.template_id:
        # Отримуємо текст та тему з шаблону
        template_subject, template_body = _render_template(payload.template_id, payload.context)
        if template_subject: final_subject = template_subject
        if template_body: final_body = template_body
        
        if not final_subject and payload.type == "email":
            await _log_to_central_service("WARNING", f"Тема для email не визначена (шаблон: {payload.template_id}, payload: відсутня).", log_details)
            # Можна встановити тему за замовчуванням або повернути помилку
            final_subject = "Важливе сповіщення" 
        if not final_body:
            await _log_to_central_service("WARNING", f"Тіло повідомлення не визначено (шаблон: {payload.template_id}, payload: відсутнє).", log_details)
            # Можна встановити тіло за замовчуванням або повернути помилку
            final_body = f"Сповіщення з контекстом: {payload.context}"


    if not final_body: # Якщо тіло все ще порожнє
        await _log_to_central_service("ERROR", "Не вдалося сформувати тіло повідомлення.", log_details)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неможливо надіслати порожнє повідомлення.")

    try:
        if payload.type == "email":
            if not final_subject: # Тема обов'язкова для email
                 await _log_to_central_service("ERROR", "Тема для email не вказана.", log_details)
                 raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Тема є обов'язковою для email сповіщень.")
            
            # Перевірка, чи recipient є валідним email (Pydantic це робить, якщо тип EmailStr)
            # Для прикладу, припустимо, що recipient вже валідований Pydantic
            # або ми можемо додати тут явну валідацію, якщо recipient є просто str
            try:
                validated_recipient = EmailStr.validate(payload.recipient)
            except ValueError:
                await _log_to_central_service("ERROR", f"Некоректний формат email отримувача: {payload.recipient}", log_details)
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некоректний формат email отримувача.")

            result = await execute_send_email(validated_recipient, final_subject, final_body)
            return NotificationResponse(
                message_id=result.get("message_id"), 
                status="email_sent_simulated" if "simulated" in result.get("message_id","") else "email_sent_attempted", 
                recipient=payload.recipient, type="email",
                details=result.get("status_detail")
            )

        elif payload.type == "sms":
            result = await execute_send_sms(payload.recipient, final_body)
            return NotificationResponse(
                message_id=result.get("message_id"), 
                status="sms_sent_simulated" if "simulated" in result.get("message_id","") else "sms_sent_attempted", 
                recipient=payload.recipient, type="sms",
                details=result.get("status_detail")
            )
        else: # Цей випадок не мав би статися через Literal type, але для повноти
            await _log_to_central_service("ERROR", f"Непідтримуваний тип сповіщення: {payload.type}", log_details)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Непідтримуваний тип сповіщення: {payload.type}")

    except (pybreaker.CircuitBreakerError, RetryError) as e_dependency:
        # Помилка відправки через CB або після всіх retry
        error_message = f"Не вдалося надіслати сповіщення типу '{payload.type}' до '{payload.recipient}' через проблеми із залежним сервісом: {str(e_dependency)}"
        await _log_to_central_service("ERROR", error_message, log_details)
        # Можна додати логіку постановки в чергу для повторної відправки пізніше
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Сервіс відправки {payload.type} тимчасово недоступний. Спробуйте пізніше.")
    except HTTPException: # Перевикидаємо HTTP помилки, які вже були створені
        raise
    except Exception as e:
        error_message = f"Неочікувана помилка при надсиланні сповіщення типу '{payload.type}' до '{payload.recipient}': {str(e)}"
        await _log_to_central_service("CRITICAL", error_message, log_details, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Внутрішня помилка сервера при відправці сповіщення.")


@app.get("/health", summary="Перевірка стану сервісу нотифікацій")
async def health_check_endpoint():
    """Перевіряє стан сервісу та його залежностей."""
    log_details = {}
    
    logging_service_status = "unknown"
    if LOGGING_SERVICE_URL and http_client:
        try:
            response = await http_client.get(f"{LOGGING_SERVICE_URL}/health", timeout=1.0)
            logging_service_status = "connected" if response.status_code == 200 else f"error_status_{response.status_code}"
        except Exception:
            logging_service_status = "unreachable"
    log_details["logging_service"] = {"status": logging_service_status, "url": LOGGING_SERVICE_URL or "N/A"}

    # Імітація перевірки доступності SMTP (можна спробувати з'єднатися без відправки)
    smtp_status = "not_configured"
    if SMTP_HOST and SMTP_USER: # Якщо основні налаштування є
        smtp_status = "simulated_ok" # Для прототипу
        # try:
        #     # Синхронна операція, краще в to_thread
        #     # server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=3)
        #     # if SMTP_USE_TLS: server.starttls()
        #     # server.login(SMTP_USER, SMTP_PASSWORD) # Можна не логінитися для простої перевірки з'єднання
        #     # server.noop() # Перевірка з'єднання
        #     # server.quit()
        #     # smtp_status = "connected"
        # except Exception as e_smtp:
        #     smtp_status = f"error: {str(e_smtp)[:50]}" # Обмежити довжину помилки
        #     logger.warning(f"Health check: помилка SMTP: {e_smtp}")
    log_details["smtp_gateway"] = {"status": smtp_status}

    # Імітація перевірки SMS шлюзу (ping ендпоінт, якщо є)
    sms_gateway_status = "not_configured"
    if SMS_GATEWAY_URL and SMS_API_KEY and http_client:
        sms_gateway_status = "simulated_ok" # Для прототипу
        # try:
        #     # response = await http_client.get(f"{SMS_GATEWAY_URL}/status", timeout=1.5) # Приклад
        #     # sms_gateway_status = "connected" if response.status_code == 200 else f"error_status_{response.status_code}"
        # except Exception:
        #     sms_gateway_status = "unreachable"
    log_details["sms_gateway"] = {"status": sms_gateway_status}
    
    cb_states = {
        "logging_service_breaker": {"state": "CLOSED" if not log_service_breaker.opened else "OPEN", "failures": log_service_breaker.fail_counter},
        "smtp_breaker": {"state": "CLOSED" if not smtp_breaker.opened else "OPEN", "failures": smtp_breaker.fail_counter},
        "sms_gateway_breaker": {"state": "CLOSED" if not sms_gateway_breaker.opened else "OPEN", "failures": sms_gateway_breaker.fail_counter}
    }
    
    overall_status = "healthy"
    # Приклад умови для "degraded"
    if (LOGGING_SERVICE_URL and logging_service_status != "connected") or \
       (SMTP_HOST and smtp_status != "simulated_ok" and smtp_status != "connected") or \
       (SMS_GATEWAY_URL and sms_gateway_status != "simulated_ok" and sms_gateway_status != "connected"):
        overall_status = "degraded"

    return {
        "service_status": overall_status,
        "service_name": SERVICE_NAME,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "dependencies": log_details,
        "circuit_breakers": cb_states
    }