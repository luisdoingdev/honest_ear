#!/usr/bin/env bash
# Start the Honest Ear dev stack at https://honest-ear.localhost
#   ./start.sh           start (or update) everything in the background
#   ./start.sh --watch   ...then stay attached and rebuild when requirements/Dockerfile change
#   ./start.sh --logs    ...then tail logs
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="honest-ear.localhost"
HOSTS="$DOMAIN flower.$DOMAIN mail.$DOMAIN"
CERT="certs/honest-ear.pem"
KEY="certs/honest-ear-key.pem"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

WATCH=false
LOGS=false
for arg in "$@"; do
  case "$arg" in
    --watch) WATCH=true ;;
    --logs) LOGS=true ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

docker info >/dev/null 2>&1 || die "Docker isn't running. Start Docker Desktop and try again."
command -v mkcert >/dev/null 2>&1 || die "mkcert is needed for local SSL. Install it with: brew install mkcert"

# 1. Locally trusted SSL certificate.
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  info "Creating a locally trusted SSL certificate (macOS may ask for your password once)"
  mkcert -install
  mkdir -p certs
  # shellcheck disable=SC2086
  mkcert -cert-file "$CERT" -key-file "$KEY" $HOSTS "*.$DOMAIN"
fi

# 2. Environment file.
if [ ! -f .env ]; then
  info "Creating .env from .env.example"
  secret="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 50)"
  sed "s|^SECRET_KEY=.*|SECRET_KEY=$secret|" .env.example > .env
fi

# 3. Hostnames. Browsers resolve *.localhost on their own; this helps curl/other tools.
missing=""
for h in $HOSTS; do
  grep -qE "^[^#]*[[:space:]]$h([[:space:]]|$)" /etc/hosts || missing="$missing $h"
done
if [ -n "$missing" ] && [ ! -f certs/.skip-hosts ] && [ -t 0 ]; then
  printf 'Add%s to /etc/hosts? Needs sudo; browsers work without it. [y/N] ' "$missing"
  read -r answer
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    echo "127.0.0.1$missing" | sudo tee -a /etc/hosts >/dev/null
  else
    touch certs/.skip-hosts
  fi
fi

# 4. Ports. Fail early with a clear message if something else holds them.
env_value() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
HTTP_PORT="$(env_value HTTP_PORT)"; HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="$(env_value HTTPS_PORT)"; HTTPS_PORT="${HTTPS_PORT:-443}"
if [ -z "$(docker compose ps -q --status running nginx 2>/dev/null)" ]; then
  for port in "$HTTP_PORT" "$HTTPS_PORT"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      die "Port $port is already in use. Free it (Rancher Desktop: disable Traefik under Preferences → Kubernetes) or set HTTP_PORT/HTTPS_PORT in .env (see .env.example)."
    fi
  done
fi
BASE="https://$DOMAIN"
SUFFIX=""
[ "$HTTPS_PORT" = "443" ] || SUFFIX=":$HTTPS_PORT"

# 5. Containers.
info "Building and starting containers"
if ! docker compose up -d --build --wait; then
  docker compose ps
  die "Some services didn't become healthy. Check: docker compose logs <service>"
fi

# 6. Dev superuser from .env (only created if missing).
docker compose exec -T web python manage.py shell --no-imports -c "
import os
from django.contrib.auth import get_user_model
User = get_user_model()
name = os.environ['DJANGO_SUPERUSER_USERNAME']
if not User.objects.filter(username=name).exists():
    User.objects.create_superuser(name, os.environ.get('DJANGO_SUPERUSER_EMAIL', ''), os.environ['DJANGO_SUPERUSER_PASSWORD'])
    print(f'Created superuser {name!r}')
"

cat <<EOF

  App        $BASE$SUFFIX
  API docs   $BASE$SUFFIX/api/docs
  Admin      $BASE$SUFFIX/admin/   (credentials in .env)
  Flower     https://flower.$DOMAIN$SUFFIX
  Mailpit    https://mail.$DOMAIN$SUFFIX

  Stop with ./stop.sh

EOF

if $WATCH; then
  info "Watching requirements*.txt and Dockerfile for rebuilds (Ctrl-C to stop watching)"
  exec docker compose watch --no-up
elif $LOGS; then
  exec docker compose logs -f
fi
