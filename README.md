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
./dev.sh
```

`dev.sh` is the local counterpart of `start.sh` (which Render runs). It builds the images, starts every service in the background, waits until they're healthy and returns you to the prompt. Follow logs with `docker compose logs -f`. `./stop.sh` stops and removes the containers but keeps your data; `./stop.sh --reset` also wipes the database and Redis, so `./stop.sh --reset && ./dev.sh` starts from scratch.

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
./stop.sh                                        # stop everything (data kept)
./stop.sh --reset && ./dev.sh                    # start from scratch: wipes the local database
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
4. In the same request, the server sends the audio to [Deepgram](https://deepgram.com) (`nova-3`, `language=multi`) and returns the text with the upload. The page shows it under the status line.
5. **Play** plays it back from the server. When playback ends the page calls `DELETE /api/recordings/<id>/`. The transcript stays on screen.

### Transcription

Set `DEEPGRAM_API_KEY` in `.env` (and in Render) to turn it on; without it, recording works and no text is shown. The admin's `transcription` feature flag switches it off without removing the key.

- The upload response includes `transcript`. It's text, `""` when no speech was heard, or `null` when transcription is off or failed. On failure there's also `transcript_error`, which the page shows. The recording is kept either way.
- Recordings are sent with Deepgram's `mip_opt_out=true`, so they're excluded from Deepgram's model-improvement program. Transcripts aren't stored, and the logs record only their length, never the words.
- `language=multi` transcribes Spanish, English, or both mixed in one sentence, whatever the browser's language. The browser language only picks the language of the page and its messages.
- This uses Deepgram's pre-recorded API: the whole file goes up after **Stop**. Live, word-by-word results while speaking (`interim_results`, `endpointing`) belong to the streaming API. Deepgram rejects them here with HTTP 400, and the log shows its reason. Live results would need a websocket, and therefore ASGI, which is a separate change.
- Deepgram's call has a 20-second timeout, which keeps it under gunicorn's 30-second request limit. `DEEPGRAM_MODEL` overrides the model.
- Tests never call Deepgram. The Django tests replace it with fake replies, and Playwright substitutes a transcript in the upload response.

As a safety net, Celery Beat runs `delete_stale_recordings` every minute and removes anything older than 10 minutes, such as recordings from a tab that was closed before playing.

Browsers only allow the microphone over HTTPS or on `localhost`. To try it on your phone, use the deployed HTTPS site; `http://<your-computer's-IP>:8000` won't work. On Render, the cleanup only runs once a Celery worker and Beat are deployed there.

## Deploying (Render + Supabase)

The web service on Render builds from the `Dockerfile`. On every start, `start.sh` runs migrations, creates the admin user from `DJANGO_SUPERUSER_*` if it doesn't exist yet, then starts gunicorn on Render's `PORT`. If `DATABASE_URL` still points at the local Docker host, or the database can't be reached, the container exits with an error and Render marks the deploy as failed.

The service is described in [`render.yaml`](render.yaml), a Render Blueprint. It lists every environment variable by name, and it never holds a secret value:

| Variable | Where the value comes from |
| --- | --- |
| `SECRET_KEY` | Render generates it (`generateValue`). Nobody needs to see or copy it. |
| `DATABASE_URL` | You enter it in Render (`sync: false`). Use the Supabase **Session pooler** URL (port 5432) with `?sslmode=require` on the end. Render is IPv4-only, so the direct connection won't work. URL-encode special characters in the password. |
| `REDIS_URL` | You enter it in Render. It's the internal URL of a Render Key Value instance, or Upstash. |
| `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` | You enter them in Render. They're used once, to create the admin; then delete the password from Render. |
| `DEEPGRAM_API_KEY` | You enter it in Render. Optional: without it, recordings aren't transcribed. |
| `DJANGO_SUPERUSER_USERNAME`, `DEBUG`, `LOG_LEVEL` | Fixed in `render.yaml` (`admin`, `False`, `INFO`). |
| `ALLOWED_HOSTS` | Not needed. Django adds Render's own `RENDER_EXTERNAL_HOSTNAME` automatically. Set it only for a custom domain. |

Don't copy the local `.env` into Render. Its hostnames (`db`, `redis`) only exist on the Docker Compose network.

### Step by step

1. **Supabase**: create a project and save the database password. A password with only letters and numbers is easiest, because it doesn't need URL-encoding. Click **Connect**, then **Session pooler**, and copy the URI:
   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   Don't use the Direct connection, which is IPv6-only, or the Transaction pooler on port 6543.
2. **Render → New → Blueprint**: pick this repository. Render reads `render.yaml`, generates `SECRET_KEY`, and asks for each `sync: false` value. Paste them there and nowhere else.
   If you already created the web service by hand, delete it first, or keep it and set the same variables in its **Environment**. Render's `sync: false` prompts only appear when a Blueprint creates a service.
3. Render deploys automatically. A healthy deploy log shows `Applying ... OK` (or `No migrations to apply`), then `Created superuser`, then `Listening at: http://0.0.0.0:10000`. Later pushes deploy only after CI passes (`autoDeployTrigger: checksPass`).
4. Open `https://<app>.onrender.com/` and `/admin/`. Then delete `DJANGO_SUPERUSER_PASSWORD` from Render's Environment: the admin already exists, and `ensure_superuser` skips users that do.
5. `/health/` reports Redis as `error` until `REDIS_URL` points at a real instance. That's why the Render health check uses `/` for now. Once Redis is set, change `healthCheckPath` to `/health/`.

### Keeping secrets out of the repo

- Real values live only in Render's Environment and in your local `.env`, which is gitignored. Don't paste them into chats, issues, screenshots or logs. `/health/` reports only `ok` or `error`; the details go to the server logs.
- CI runs [gitleaks](https://github.com/gitleaks/gitleaks) over the whole Git history on every push and pull request. The scan fails if it finds an API key or a database or Redis URL with a password. Custom rules are in `.gitleaks.toml`. To run it locally:
  ```bash
  docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.21.2 git --config /repo/.gitleaks.toml --redact /repo
  ```
- On GitHub, also turn on **Settings → Code security → Secret scanning** and **Push protection**. Both are free for public repos, and push protection blocks a push that contains a known token format.
- If a secret leaks, rotate it at the source (Supabase → Database → Reset password; Render regenerates `SECRET_KEY` from its Environment page). Deleting the commit isn't enough.

### Troubleshooting

| Deploy log says | Fix |
| --- | --- |
| `DATABASE_URL points at the local Docker host 'db'` or `failed to resolve host 'db'` | `DATABASE_URL` is still the local value. Use the Supabase Session pooler URL. |
| `Network is unreachable` (an IPv6 address) | You used Supabase's Direct connection. Use the Session pooler. |
| `password authentication failed` | The password is wrong or not URL-encoded, or the user isn't `postgres.<ref>`. |
| `Invalid HTTP_HOST header` / 400 responses | The request came in on a domain Django doesn't know, such as a custom domain. Add it to `ALLOWED_HOSTS` as a bare host, without `https://` or `/`. |
| No `Applying` lines at all | **Docker Command** in Settings isn't empty, so `start.sh` isn't running. |

## Languages

The site is in English and Spanish, aimed at Colombia (Bogotá and Medellín). Each visitor gets the language their browser asks for: `es-CO`, `es-419`, `es-MX` and any other `es-*` get Spanish, and everyone else gets English. There's no IP or geolocation lookup, because the browser language is what the person actually reads. The Django admin follows the same choice.

Translations live in `core/locale/es/LC_MESSAGES/`:

| File | Strings from |
| --- | --- |
| `django.po` | Templates (`{% translate %}`) and Python (`gettext`), including API error messages |
| `djangojs.po` | `record.js` (`gettext()`, served by `/jsi18n/`) |

The Spanish is neutral Latin American with the informal "tú", which works for Bogotá and Medellín. "Start session" is translated as "Empezar", because "Iniciar sesión" reads as "Log in".

After adding or changing text, update the `.po` files, fill in the new `msgstr` entries, then restart:

```bash
docker compose exec web sh -c "cd core && django-admin makemessages -l es && django-admin makemessages -d djangojs -l es"
docker compose restart web
```

The compiled `.mo` files aren't committed. Compose compiles them when `web` starts, and the Dockerfile compiles them at build time for Render. Remove any `#, fuzzy` marks after checking a translation, because fuzzy entries are skipped.

To add another language, add it to `LANGUAGES` in `config/settings.py`, run the commands above with `-l <code>`, and translate the new files.

## Feature flags

Flags live in the admin under **Core → Feature flags**. Tick or untick **Enabled** in the list and click **Save**. Changes take effect on the next request and are logged (`Feature flag recording turned off by admin`).

| Flag | Controls |
| --- | --- |
| `recording` | The recorder on the home page and new uploads. When off, the page says recording is unavailable and `POST /api/recordings/` returns 503. Recordings already in progress can still be played and deleted. |
| `transcription` | Sending recordings to Deepgram. When off, uploads return `transcript: null` and no text is shown. It also needs `DEEPGRAM_API_KEY`. |

Both flags are created switched on by migrations. A flag that doesn't exist counts as off.

## Tests

Browser tests use [Playwright](https://playwright.dev) and live in `e2e/`. They run in Docker against the local stack on four devices: desktop Chrome, iPhone 15 and iPhone SE (WebKit, Safari's engine) and Pixel 7. They check that pages load with their styles, don't scroll sideways or overflow on phones, avoid `vh` units in our CSS, still match the approved screenshots, and that the admin login works.

Screenshot baselines are not committed (`e2e/tests/*-snapshots/` is gitignored). The first run on a machine writes them; later runs on that machine compare against them. CI starts fresh each time, so it checks layout but not visual changes.

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

To watch the tests in a real browser, run Playwright on your Mac against the running stack (`./dev.sh` first). The one-time setup needs Node:

```bash
cd e2e && npm ci && npx playwright install chromium webkit
```

```bash
npm run ui         # Playwright's UI mode: pick tests, watch them, step through each action
npm run headed     # run everything in visible browser windows, one at a time
```

Screenshot baselines made on the Mac are saved as `*-darwin.png`, separate from Docker's `*-linux.png`.

GitHub Actions runs the same suite on every push and pull request, recording a video of every browser test. To watch them, open the run on GitHub, download the **playwright-report** artifact from the run's Summary page (it's kept for 7 days), unzip it, and run `npx playwright show-report playwright-report` from the unzipped folder. Each test has its video, and failures also have a screenshot and trace. Locally, videos are kept only for failing tests. In Render, set **Auto-Deploy** to **After CI Checks Pass** so a failing push is never deployed.

## Project layout

```
config/   Django settings, URLs, WSGI and Celery app
core/     Landing page, health check, tasks and management commands
```
