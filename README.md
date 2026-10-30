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

There's one mic button, following the "Honest Ear Voice Prototype" design:

1. **Tap the mic** (the first time, the browser asks for microphone access). It moves to the bottom of the screen and a glow rises while it listens. The status counts up, and **live captions** show the last few words as you speak. Recording stops on its own at 60 seconds.
2. **Tap it again** to stop. A spinner shows while the audio is uploaded to `POST /api/recordings/` and stored in Postgres (5 MB max; webm, mp4 or ogg).
3. The full transcript fades in, then **Play** appears. Playback comes from the server. When it ends, the page calls `DELETE /api/recordings/<id>/`, and the transcript stays on screen. Tapping the mic again starts a new recording and deletes any unplayed one.

### Transcription

Set `DEEPGRAM_API_KEY` in `.env` (and in Render) to turn it on; without it, recording works and no text is shown. The admin's `transcription` feature flag switches it off without removing the key.

- The upload response includes `transcript`. It's text, `""` when no speech was heard, or `null` when transcription is off or failed. On failure there's also `transcript_error`, which the page shows. The recording is kept either way.
- Recordings are sent with Deepgram's `mip_opt_out=true`, so they're excluded from Deepgram's model-improvement program. Transcripts aren't stored, and the logs record only their length, never the words.
- `language=multi` transcribes Spanish, English, or both mixed in one sentence, whatever the browser's language. The browser language only picks the language of the page and its messages.
- If live captions didn't run, the server sends the uploaded file to Deepgram's pre-recorded API after Stop and returns the text with the upload. Streaming-only options (`interim_results`, `endpointing`) don't work on that endpoint: Deepgram rejects them with HTTP 400, and the log shows its reason.

### Live captions

While you speak, the browser streams the microphone straight to **Deepgram Flux**, Deepgram's turn-based live model, and Django stays a plain WSGI app (no ASGI or websockets on the server):

1. On the first tap, the page calls `POST /api/transcription/token/`. Django uses the real API key to get a 30-second token from Deepgram's `/v1/auth/grant`, and returns it with a websocket URL whose options it picked: `wss://api.deepgram.com/v2/listen` (Flux only answers on v2), `model=flux-general-multi` with `language_hint=es` and `language_hint=en` (Spanish, English or a mix), and `mip_opt_out`. v1 options such as `interim_results`, `endpointing` or `smart_format` don't apply to Flux. The API key never reaches the browser, and the token only needs to be valid while the socket opens.
2. `pcm-worklet.js` turns the microphone into 16 kHz 16-bit PCM in 80 ms chunks, the size Deepgram recommends for Flux. Raw PCM streams the same way on every browser, while the MP4 that iPhones record can't be streamed. MediaRecorder still records the file for playback alongside it.
3. Flux reports **turns**. Each `TurnInfo` message (`StartOfTurn`, `Update`, `EndOfTurn`, …) carries the whole transcript of one turn so far and its `turn_index`, so the page replaces that turn's text in place. Captions show the last words across turns, and the transcript is all turns joined.
4. On Stop the page sends `ForceEndTurn`, then `CloseStream`. Closing alone would lose the turn still in progress, because Flux only sends a turn's final text when it ends. The page waits up to 2 seconds for that last `EndOfTurn`, then uploads with `live_transcript=1`, so the server doesn't pay Deepgram a second time.

If anything fails (the `live_captions` flag is off, no token, a socket error), recording works as before and the server transcribes after Stop.

- **The key needs Member permissions** in the Deepgram console; with the default role, `/v1/auth/grant` returns 403 and live captions stay off.
- Each IP can get 10 tokens a minute. The counter lives in Redis, and if Redis is down no tokens are issued. The IP is the last `X-Forwarded-For` entry, the one Render's proxy adds, because earlier entries can be forged.
- Logs record when a token is issued or refused, never the token itself.
- Deepgram's call has a 20-second timeout, which keeps it under gunicorn's 30-second request limit. `DEEPGRAM_MODEL` overrides the after-Stop model (`nova-3`) and `DEEPGRAM_STREAMING_MODEL` the live one (`flux-general-multi`).
- Tests never call Deepgram. The Django tests replace it with fake replies, and run without the `.env` key so a real key can't make them call it. Playwright fakes the token endpoint and Flux's websocket (`page.routeWebSocket`), checks the audio arrives as 80 ms PCM chunks and that Stop sends `ForceEndTurn` before `CloseStream`, and covers the fall-back path.

As a safety net, Celery Beat runs `delete_stale_recordings` every minute and removes anything older than 10 minutes, such as recordings from a tab that was closed before playing.

Browsers only allow the microphone over HTTPS or on `localhost`. To try it on your phone, use the deployed HTTPS site; `http://<your-computer's-IP>:8000` won't work. On Render, the cleanup only runs once a Celery worker and Beat are deployed there.

## Deploying (Render + Supabase)

The web service on Render builds from the `Dockerfile`. On every start, `start.sh` runs migrations, creates the admin user from `DJANGO_SUPERUSER_*` if it doesn't exist yet, then starts gunicorn on Render's `PORT`. If `DATABASE_URL` still points at the local Docker host, or the database can't be reached, the container exits with an error and Render marks the deploy as failed.

The service is described in [`render.yaml`](render.yaml), a Render Blueprint. It lists every environment variable by name, and it never holds a secret value:

| Variable | Where the value comes from |
| --- | --- |
| `SECRET_KEY` | Render generates it (`generateValue`). Nobody needs to see or copy it. |
| `DATABASE_URL` | You enter it in Render (`sync: false`). Use the Supabase **Session pooler** URL (port 5432) with `?sslmode=require` on the end. Render is IPv4-only, so the direct connection won't work. URL-encode special characters in the password. |
| `REDIS_URL` | Set automatically: `render.yaml` creates a free Key Value (Redis) instance, `honest-ear-redis`, reachable only from your Render services. |
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
5. Render's health check calls `/health/`, which checks both Postgres and Redis.

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
| `transcription` | Sending recordings to Deepgram. When off, uploads return `transcript: null`, no text is shown, and live captions are off too. It also needs `DEEPGRAM_API_KEY`. |
| `live_captions` | Captions while speaking (needs `transcription` and a Member-role key). When off, recordings are transcribed after Stop instead. |

All flags are created switched on by migrations. A flag that doesn't exist counts as off.

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
