# Один образ на три процесса: web (страницы), bot (long polling), worker (уведомления).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
# повторные попытки: канал до PyPI бывает нестабильным
RUN pip install --no-cache-dir --retries 10 --timeout 120 -r requirements.txt

COPY app ./app

RUN useradd --create-home aperio && chown -R aperio /app
USER aperio
EXPOSE 8080
# Команда по умолчанию — для хостинга, который поднимает один контейнер:
# app/run.py запускает web, а следом bot и worker. В docker compose каждый
# сервис задаёт свою команду и эту строку не использует.
CMD ["python", "-m", "app.run"]
