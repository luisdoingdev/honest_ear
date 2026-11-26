FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG DEV=true
COPY requirements.txt requirements-dev.txt ./
RUN uv pip install --system --no-cache -r requirements.txt \
    && if [ "$DEV" = "true" ]; then uv pip install --system --no-cache -r requirements-dev.txt; fi

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
USER app

EXPOSE 8000
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
