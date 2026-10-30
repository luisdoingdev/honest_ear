#!/bin/sh
# Production entrypoint (Render). Local Docker Compose runs its own command for `web`.
set -e

# `db` only exists on the local Compose network.
case "$DATABASE_URL" in
  *@db:*|*@db/*)
    echo "DATABASE_URL points at the local Docker host 'db'. Set it to the Supabase Session pooler URL in Render's Environment." >&2
    exit 1
    ;;
esac

python manage.py migrate --noinput
python manage.py ensure_superuser

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --access-logfile -
