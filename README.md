# Honest Ear

Django 6 (ASGI on Uvicorn) · Channels (WebSockets) · Django Ninja API · HTMX templates · Celery + beat · Postgres 17 · Redis 7 · Nginx (local HTTPS + load balancer) · Mailpit · Flower.

## Requirements
- Docker Desktop
- `mkcert` for trusted local SSL: `brew install mkcert`

## Start / stop
```sh
./start.sh            # build + start everything, print URLs
./start.sh --watch    # same, then rebuild automatically when requirements*.txt / Dockerfile change
./start.sh --logs     # same, then tail logs
./stop.sh             # stop (data kept)
./stop.sh --clean     # stop and delete Postgres/Redis volumes
```
On the first run `start.sh` installs the mkcert root CA (one-time password prompt), creates `.env` from `.env.example`, and can add the hostnames to `/etc/hosts`.

| URL | What |
|---|---|
| https://honest-ear.localhost | App |
| https://honest-ear.localhost/api/docs | API docs (Ninja / OpenAPI) |
| https://honest-ear.localhost/admin/ | Admin (user/pass from `.env`) |
| https://flower.honest-ear.localhost | Celery dashboard |
| https://mail.honest-ear.localhost | Caught outgoing email |

## Nginx load balancer
Nginx (`nginx/templates/default.conf.template`) terminates SSL and spreads requests across `WEB_REPLICAS` uvicorn containers (default 2) using `least_conn`. WebSockets work on any replica because Channels shares state through Redis. Sessions live in Postgres, so logins hold across replicas.
- Every response has an `X-Upstream` header showing which replica answered: `curl -skI https://honest-ear.localhost/ | grep -i x-upstream`.
- Scale without restarting nginx: `docker compose up -d --scale web=3` (or change `WEB_REPLICAS` and rerun `./start.sh`). Nginx re-reads Docker DNS every 5s.
- If a replica refuses a connection (e.g. while it reloads), nginx retries the request on another one. Killing a container outright can fail one in-flight request before nginx drops it.
- Migrations run once in the one-shot `migrate` service before the web replicas start.

## Auto reload
- **Python / templates / static:** the project folder is bind-mounted. Uvicorn restarts on changes and `django-browser-reload` refreshes the open page.
- **Celery worker & beat:** restarted by `watchfiles` whenever a `.py` file changes.
- **Dependencies:** run `./start.sh --watch` (or rerun `./start.sh`) to rebuild the image.

## Rancher Desktop notes
- **Ports 80/443:** Rancher's built-in Kubernetes runs Traefik on them. Either turn off Traefik (Preferences → Kubernetes) or set `HTTP_PORT=8080`, `HTTPS_PORT=8443` and `CSRF_TRUSTED_ORIGINS=https://honest-ear.localhost:8443` in `.env`. `start.sh` stops with a message if the ports are taken.
- **File watching:** Rancher's reverse-sshfs mount doesn't forward file-change events, so the reloaders poll (`WATCHFILES_FORCE_POLLING=true` in `.env`). Using the VZ + virtiofs mount (Preferences → Virtual Machine) gives you real events, and then you can set it to `false`.
- Right after you save a file, a request can very briefly see the old content while the mount syncs. Just refresh.

## Layout
```
nginx/    load balancer + SSL config (template rendered at container start)
config/   settings, asgi (HTTP + WebSocket routing), celery, urls
core/     api.py (Ninja), views.py + templates (HTMX), tasks.py (Celery), consumers.py (Channels)
```

## Common commands
```sh
docker compose exec web pytest                       # tests
docker compose exec web ruff check . && docker compose exec web ruff format .
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py shell
pre-commit install                                   # ruff + gitleaks on commit (needs pre-commit on host)
```

## Why Celery?
Async Django keeps request handling from blocking on I/O, but it doesn't run background jobs. Django's built-in `django.tasks` backends only run tasks immediately or discard them, so Celery (with Redis as the broker) does the background work, and beat runs scheduled jobs (edit schedules in the admin under *Periodic tasks*).
