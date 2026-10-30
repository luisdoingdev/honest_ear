FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN SECRET_KEY=collectstatic-only \
    DATABASE_URL=sqlite:///:memory: \
    REDIS_URL=redis://localhost:6379/0 \
    python manage.py collectstatic --noinput

RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

CMD ["sh", "/app/start.sh"]
