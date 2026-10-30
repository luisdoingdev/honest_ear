#!/bin/sh
# Production entrypoint (Render). Local Docker Compose runs its own command for `web`.
set -e

python manage.py migrate --noinput
python manage.py ensure_superuser

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --access-logfile -
