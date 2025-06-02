
# Використовуємо офіційний, легкий образ Python версії 3.9.
FROM python:3.9-slim AS builder

# Встановлення змінних середовища
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Встановлюємо робочу директорію всередині контейнера.
WORKDIR /app

COPY ./requirements.txt /app/

RUN pip install --upgrade pip && \
    pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt

COPY ./cart_service.py /app/

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
# USER appuser # Переключення на користувача без root-прав (можна розкоментувати, якщо потрібно)

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

