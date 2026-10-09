# Se construye desde la raíz del repo (contexto ".."): la imagen necesita también web/panel.
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/alembic.ini .
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY web/panel ./web/panel

RUN useradd --create-home --uid 1000 omitel \
    && mkdir -p /app/app/static/assets /app/app/static/media \
    && chown -R omitel:omitel /app
USER omitel

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EMBED_WORKERS_IN_API=false

EXPOSE 8000

CMD ["gunicorn", "app.presentation.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "120"]
