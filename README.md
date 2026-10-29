# Honest Ear

![Honest Ear landing page](docs/landing.png)

## Stack

- Django 5.2
- PostgreSQL 17
- Redis 7 (cache and Celery broker)
- Celery worker and Celery Beat
- Docker Compose

## Getting started

Requires Docker.

```bash
cp .env.example .env
```

Edit `.env` and replace every `change-me` with your own values, then start everything:

```bash
docker compose up --build
```

The site is at http://localhost:8000. On startup the web container runs migrations and creates the admin user from the `DJANGO_SUPERUSER_*` values in `.env`. Sign in at http://localhost:8000/admin/.

## Services

| Service       | Purpose                  | Port |
| ------------- | ------------------------ | ---- |
| `web`         | Django development server | 8000 |
| `celery`      | Background task worker   | –    |
| `celery-beat` | Scheduled tasks          | –    |
| `db`          | PostgreSQL               | 5432 |
| `redis`       | Cache and message broker | 6379 |

## Useful commands

```bash
docker compose up -d && docker compose logs -f   # start in background and follow logs
docker compose down                              # stop everything
docker compose exec web bash                     # shell inside the web container
docker compose exec web python manage.py shell   # Django shell
docker compose exec web python manage.py changepassword admin
```

`GET /health/` returns the status of the database and Redis.

## Tests

Browser tests use [Playwright](https://playwright.dev) and live in `e2e/`. They run in Docker against the local stack on four devices: desktop Chrome, iPhone 15 and iPhone SE (WebKit, Safari's engine) and Pixel 7. They check that pages load with their styles, don't scroll sideways or overflow on phones, avoid `vh` units in our CSS, still match the approved screenshots, and that the admin login works.

```bash
docker compose run --rm e2e                       # run all tests
docker compose run --rm e2e --project=iphone-se   # one device only
docker compose run --rm e2e --update-snapshots    # approve intentional visual changes
```

After a run, open `e2e/playwright-report/index.html` to see failures with screenshots and traces.

GitHub Actions runs the same suite on every push and pull request. In Render, set **Auto-Deploy** to **After CI Checks Pass** so a failing push is never deployed.

## Project layout

```
config/   Django settings, URLs, WSGI and Celery app
core/     Landing page, health check, tasks and management commands
```
