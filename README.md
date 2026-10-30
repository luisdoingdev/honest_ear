# Honest Ear

![Honest Ear home page](docs/home.png)

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

## Recorder

The home page, http://localhost:8000/, lets anyone record their voice, play it back, and have it deleted (`/record/` redirects there):

1. **Start session** asks for microphone access.
2. **Record** / **Stop**: recordings stop automatically at 60 seconds.
3. The audio is uploaded to `POST /api/recordings/` and stored in Postgres (5 MB max; webm, mp4 or ogg).
4. **Play** plays it back from the server. When playback ends the page calls `DELETE /api/recordings/<id>/`.

As a safety net, Celery Beat runs `delete_stale_recordings` every minute and removes anything older than 10 minutes, such as recordings from a tab that was closed before playing.

Browsers only allow the microphone over HTTPS or on `localhost`. To try it on your phone, use the deployed HTTPS site; `http://<your-computer's-IP>:8000` won't work. On Render, the cleanup only runs once a Celery worker and Beat are deployed there.

## Deploying (Render + Supabase)

The web service on Render builds from the `Dockerfile`. On every start, `start.sh` runs migrations, creates the admin user from `DJANGO_SUPERUSER_*` if it doesn't exist yet, then starts gunicorn on Render's `PORT`. If the database can't be reached, the container exits with an error and Render marks the deploy as failed.

Set these in the Render service's **Environment** (they are not read from `.env`):

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Supabase **Session pooler** URL (port 5432) with `?sslmode=require` on the end. Render is IPv4-only, so the direct connection won't work. URL-encode special characters in the password. |
| `REDIS_URL` | Internal URL of a Render Key Value instance (or Upstash). |
| `SECRET_KEY` | A new random value, not the local one. |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | Your `*.onrender.com` domain. |
| `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` | Admin account to create on first deploy. |
| `LOG_LEVEL` | Optional; defaults to `INFO`. |

## Feature flags

Flags live in the admin under **Core → Feature flags**. Tick or untick **Enabled** in the list and click **Save**. Changes take effect on the next request and are logged (`Feature flag recording turned off by admin`).

| Flag | Controls |
| --- | --- |
| `recording` | The recorder on the home page and new uploads. When off, the page says recording is unavailable and `POST /api/recordings/` returns 503. Recordings already in progress can still be played and deleted. |

The `recording` flag is created switched on by a migration. A flag that doesn't exist counts as off.

## Tests

Browser tests use [Playwright](https://playwright.dev) and live in `e2e/`. They run in Docker against the local stack on four devices: desktop Chrome, iPhone 15 and iPhone SE (WebKit, Safari's engine) and Pixel 7. They check that pages load with their styles, don't scroll sideways or overflow on phones, avoid `vh` units in our CSS, still match the approved screenshots, and that the admin login works.

Django unit tests cover the recordings API and cleanup:

```bash
docker compose exec web python manage.py test
```

On Chromium devices, Playwright also runs the full record → play → delete flow using a fake microphone. Playwright's WebKit can't record, so the iPhone projects check layout only.

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
