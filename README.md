# Honest Ear

![Honest Ear home page](docs/home.png)

## Stack

- Django 5.2, served as ASGI by Daphne (Django Channels for the recording websocket)
- PostgreSQL 17
- Redis 7 (cache and Celery broker)
- Celery worker and Celery Beat
- Docker Compose
- Whisper (faster-whisper) and wav2vec2 in their own containers, for transcripts and phonemes

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
| `web`         | Django (Daphne `runserver`): pages, admin and `/ws/record/` | 8000 |
| `celery`      | Background task worker   | –    |
| `celery-beat` | Scheduled tasks          | –    |
| `db`          | PostgreSQL               | 5433 on the Mac (`POSTGRES_PORT`), `db:5432` inside Docker |
| `redis`       | Cache and message broker | 6380 on the Mac (`REDIS_PORT`), `redis:6379` inside Docker |
| `scorer`      | Local phoneme service (wav2vec2, CPU) | 8011 on the Mac (`SCORER_PORT`), `scorer:8000` inside Docker |
| `transcriber` | Local speech-to-text (Whisper, CPU) | 8012 on the Mac (`TRANSCRIBER_PORT`), `transcriber:8000` inside Docker |

`.env.example` points Django at the `transcriber` and `scorer` containers. No recording goes to an outside service.

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

1. **Tap the mic** (the first time, the browser asks for microphone access). It moves to the bottom of the screen and a glow rises while it listens, and the status counts up. Recording stops on its own at 60 seconds.
2. **Tap it again** to stop. The server saves the recording right away, so **Play** appears within a moment.
3. The results fill in as each is ready: the phonemes after a few seconds, then the transcript, then the words paired with their phonemes. Playback comes from the server. When it ends, the page calls `DELETE /api/recordings/<id>/`, and the results stay on screen. Tapping the mic again starts a new recording and deletes any unplayed one.

### The recording websocket

There's no upload. Each recording has its own websocket, `/ws/record/`, handled by `core/consumers.py` (Django Channels on Daphne):

```
page ──16 kHz PCM (80 ms chunks)──▶ /ws/record/ ──WAV──▶ scorer (wav2vec2)   ─┐
page ──{"type": "stop"}──────────▶              ──WAV──▶ transcriber (Whisper) ├─▶ Attempt
page ◀── recording · phonemes · transcript · pairs (each as soon as it's ready) ┘
```

1. `pcm-worklet.js` turns the microphone into 16 kHz 16-bit mono PCM and sends it in 80 ms chunks. Raw PCM streams the same way on every browser, including iPhones. The server keeps it in memory: at most 75 seconds of audio, after which it hangs up (close code `4413`).
2. On Stop, the page sends `{"type": "stop"}`. The server wraps the audio as a WAV, saves it as a **Recording** and pushes `{"type": "recording", "id", "audio_url"}`, so Play works before any result is in.
3. The scorer and the transcriber run **at the same time**, and each result is pushed as soon as it's ready:
   - `{"type": "phonemes", "phonemes"}` from wav2vec2. On CPU that's about 3 seconds for a short sentence.
   - `{"type": "transcript", "transcript"}` from Whisper, which detects Spanish or English itself. It takes about 13 seconds for the same sentence.
   - `{"type": "pairs", "attempt_id", "alignment"}`: the transcript's expected phonemes (espeak) aligned with the heard ones, word by word. Then the server closes the socket normally.
4. Everything is saved as an **Attempt** (see [Pronunciation scorer](#pronunciation-scorer-modal)).

The page shows each word over its phonemes: expected on top, heard below. Changed, missing (`–`) or extra phonemes are highlighted, with a count of the changes. This shows drift against what was *said* (the transcript), not a score against an intended sentence. Only Spanish and English have espeak voices here, so a clip Whisper detects as another language gets no pairs.

- **Failures never lose the other result.** If Whisper or the scorer fails or is off, its message carries `null` (the transcript also gets an `error`), the reason is stored on the Attempt and logged, and the rest still arrives. Without either service, recording and playback still work.
- **Refusals** come as a close code the page explains: `4503` when the `recording` flag is off, `4413` when the audio is too long. Only pages served from `ALLOWED_HOSTS` can open the socket.
- **Leaving before Stop** discards the audio. Leaving after Stop doesn't stop the analysis: the Attempt is still saved.
- **Privacy:** the logs record lengths and timings, never the words or the phonemes. The audio is deleted after playback unless the `keep_audio` flag (eval mode) is on.

As a safety net, Celery Beat runs `delete_stale_recordings` every minute and removes anything older than 10 minutes, such as recordings from a tab that was closed before playing.

Browsers only allow the microphone over HTTPS or on `localhost`. To try it on your phone, use the deployed HTTPS site; `http://<your-computer's-IP>:8000` won't work. On Render, the cleanup only runs once a Celery worker and Beat are deployed there.

## Pronunciation scorer (Modal)

A separate GPU service, `scorer/scorer.py`, turns a recording into phonemes (IPA) with [`facebook/wav2vec2-lv-60-espeak-cv-ft`](https://huggingface.co/facebook/wav2vec2-lv-60-espeak-cv-ft). It runs on [Modal](https://modal.com), not in the Django app or its Docker image. There's no scoring yet: it only reports what the model heard.

- **What it does:** one `POST` with the raw clip (WebM/Opus from Chrome, MP4 from iPhones). ffmpeg decodes it to 16 kHz mono float32, and the model runs on a T4 GPU. It returns:
  ```json
  {"phonemes": "o l a k o m o ...", "inference_ms": 41.5, "duration_s": 2.48}
  ```
- **The model loads once per container** (`@modal.enter()`), and the weights are cached on a Modal volume, `honest-ear-hf-cache`. The container shuts down 5 minutes after the last request.
- **Protected by a shared secret.** Requests must send it in the `X-Scorer-Secret` header; anything else gets 401.
- The tokenizer loads with `do_phonemize=False`. Its default needs the `phonemizer` library and espeak, which only matter for turning text into phonemes, not for decoding audio.

### Deploying the scorer

```bash
pip install modal
modal setup
```

Create the shared secret on Modal (keep the value; Django needs the same one):

```bash
modal secret create honest-ear-scorer SCORER_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

```bash
modal deploy scorer/scorer.py
```

The deploy prints the endpoint URL (`https://<workspace>--honest-ear-scorer-scorer-phonemes.modal.run`). Try it with a clip:

```bash
curl -X POST "$SCORER_URL" -H "X-Scorer-Secret: $SCORER_SECRET" -H "Content-Type: audio/webm" --data-binary @clip.webm
```

### Connecting Django

Set these in `.env` locally, then `docker compose up -d --force-recreate web`, and in Render's Environment:

| Variable | Value |
| --- | --- |
| `SCORER_URL` | The endpoint URL from `modal deploy`. Empty: phonemes are skipped. |
| `SCORER_SECRET` | The same value as the Modal secret `honest-ear-scorer`. |
| `SCORER_TIMEOUT_SECONDS` | Optional; defaults to 10. |

After every recording, the websocket sends the WAV to the scorer and to Whisper at once. Both results are saved together as an **Attempt**, visible in the admin under **Core → Attempts**: the transcript, the phonemes, the inference time, the audio duration, and the word-by-word pairs (as JSON and drawn per word). The audio is still deleted after playback, and the Attempt keeps only the text.

- **Cold starts:** a cold Modal container (starting up, loading the model) usually takes longer than the 10-second timeout, so the first request after a pause gets no phonemes. The recording still works, the transcript is still saved, and the reason is logged and stored on the Attempt (`Scorer timed out after 10s (cold start?)`). Later requests hit the warm container. The local container has no cold starts.
- **Cost:** the T4 is billed only while a container is up, which is any request plus 5 minutes.
- **Logs:** they record the phonemes' length, inference time and duration, never the phonemes or the words. The secret is never logged or sent anywhere but the scorer.

### Running the scorer locally (Docker, CPU)

The same model also runs in a local container, with no Modal, GPU or cold-start timeout. `scorer/local.py` answers exactly like the Modal endpoint: the same `POST`, the same `X-Scorer-Secret` header and the same JSON. Both use `scorer/engine.py`, so they decode and recognize identically, and Django can use either just by changing `SCORER_URL`.

It's part of the local stack, so `./dev.sh` builds and starts it (CI starts only `web` and doesn't build its 1.6 GB image).

- **The first start downloads the 1.2 GB of weights** into the `scorer_hf_cache` volume, then loads them. Later starts only load them. The container reports healthy once the model is ready.
- It reads `SCORER_SECRET` from `.env`, the same value Django sends.
- **Django uses it locally** through `SCORER_URL=http://scorer:8000/` and `SCORER_TIMEOUT_SECONDS=30` (both in `.env.example`). CPU inference is slower than the T4, hence the longer timeout. Modal's URL belongs only in Render.
- **Trying it from the Mac:** `curl -X POST localhost:8011/ -H "X-Scorer-Secret: $SCORER_SECRET" --data-binary @clip.webm`. If 8011 is taken, set `SCORER_PORT`. `GET localhost:8011/health` shows the model and device.
- **Memory:** the model needs about 2 GB, and the container is capped at 2.5 GB so it can't starve Postgres. If Docker's VM is small (Rancher Desktop → Preferences → Virtual Machine), either give it more memory or set `SCORER_QUANTIZE=1` in `.env`. That stores the weights as int8, about a quarter of the memory and faster, but the phonemes can differ slightly from Modal's.

## Eval bench (admin)

One question: **does the pronunciation score agree with human judgement, and how often?** Everything is in the Django admin under **Core**.

### 1. Golden set (Eval clips)

Each clip has its audio, the **intended text** (typed by a human, never copied from the transcript), its language, a **human label** (good, okay or off, by ear) and notes.

- **Upload** a clip on the Eval clips page.
- **Or promote a real recording:** turn on the `keep_audio` feature flag (off by default) and record on the site. While the flag is on, the audio isn't deleted after playback, and the page says "Kept for evaluation". Then, in **Attempts**, select attempts and choose **Promote to EvalClip**. That copies the audio and links the attempt, leaving the intended text and label for you to fill in. Turn `keep_audio` off again when you're done; visitors are promised their audio is deleted.

### 2. Scoring (`core/pronunciation.py`, configured in `core/eval_config/scoring.json`)

1. **Too short:** clips under `min_duration_s` (1.5 s) are not scored.
2. **Wrong sentence:** if fewer than `wrong_sentence_min_overlap` (50%) of the intended words appear in Whisper's transcript, the clip is marked `wrong_sentence` and not scored.
3. **Expected phonemes:** the intended text, word by word, through espeak-ng (`es-419` or `en-us`). espeak is the phonemizer this model's training labels came from, so both sides use the same IPA symbols.
4. **Alignment:** Levenshtein with backtrace, one op per phoneme (match, sub, ins or del), grouped by word. Among equally cheap alignments it picks the one with the most matches.
5. **PER** = (sub + ins + del) / expected phonemes, then a band: `good` up to `bands.good`, `okay` up to `bands.okay`, `off` above. The thresholds live only in the JSON config. Bump its `version` whenever you change a value: every run stores the config it used.

### 3. Runs and the report

- Select clips in **Eval clips** and choose **Run bench on the selected clips**. That creates an **Eval run**: each clip goes through the real pipeline, Whisper and the phoneme scorer at once, and gets an **Eval result** with status, PER, predicted band, alignment and the human label as it was at run time. If a service fails, that clip's status is `error` and the reason is saved.
- Each clip's page shows its **latest run**: expected phonemes above heard, per word, with mismatches in red.
- Each run has a **Report** (linked from the Eval runs list):
  - agreement % and Spearman correlation between PER and label (good < okay < off; near +1 means higher PER where you heard worse),
  - the 3×3 grid of human label vs predicted band,
  - the clips with the worst disagreement, linked to their pages,
  - the most common substitutions.
- `docker compose exec web python manage.py write_eval_summary` writes the latest run's summary into the **Evals** section at the end of this README.

#### The transcriber (Whisper)

The `transcriber` container gives the recordings and the bench's wrong-sentence gate their transcripts, with no API key, no cost and the same output every time. `./dev.sh` starts it, and `TRANSCRIBER_URL=http://transcriber:8000/` in `.env` points Django at it. With `TRANSCRIBER_URL` empty, recordings aren't transcribed.

- **What it runs:** `scorer/transcriber.py` runs Whisper `medium` with int8 weights on CPU (`TRANSCRIBER_MODEL` picks another size, such as `small` or `large-v3-turbo`). It uses the same `X-Scorer-Secret` as the phoneme scorer. The bench sends each clip's language; recordings send none and Whisper detects it.
- **First start:** it downloads the weights (about 1.5 GB) into the `transcriber_hf_cache` volume. The image is about 500 MB, since it has no PyTorch.
- **Runs say which transcriber they used** (`config.transcriber`, for example `local: faster-whisper-medium-int8`; runs 1 and 2 used Deepgram's `nova-3`). A different transcriber passes or gates different clips, so compare runs that used the same one.

The scorer is whatever `SCORER_URL` points at: locally the `scorer` container, which has no cold starts. Uploaded clips are stored in `media/eval_clips/` (gitignored) and played only with `DEBUG` on, so the bench is meant for local use.

## Deploying (Render + Supabase)

The web service on Render builds from the `Dockerfile`. On every start, `start.sh` runs migrations, creates the admin user from `DJANGO_SUPERUSER_*` if it doesn't exist yet, then starts Daphne (HTTP and the recording websocket) on Render's `PORT`. Render web services support websockets on every plan. If `DATABASE_URL` still points at the local Docker host, or the database can't be reached, the container exits with an error and Render marks the deploy as failed.

The service is described in [`render.yaml`](render.yaml), a Render Blueprint. It lists every environment variable by name, and it never holds a secret value:

| Variable | Where the value comes from |
| --- | --- |
| `SECRET_KEY` | Render generates it (`generateValue`). Nobody needs to see or copy it. |
| `DATABASE_URL` | You enter it in Render (`sync: false`). Use the Supabase **Session pooler** URL (port 5432) with `?sslmode=require` on the end. Render is IPv4-only, so the direct connection won't work. URL-encode special characters in the password. |
| `REDIS_URL` | Set automatically: `render.yaml` creates a free Key Value (Redis) instance, `honest-ear-redis`, reachable only from your Render services. |
| `DJANGO_SUPERUSER_EMAIL`, `DJANGO_SUPERUSER_PASSWORD` | You enter them in Render. They're used once, to create the admin; then delete the password from Render. |
| `SCORER_URL`, `SCORER_SECRET` | You enter them in Render. Optional: the Modal phoneme service (see [Pronunciation scorer](#pronunciation-scorer-modal)). |
| `TRANSCRIBER_URL` | You enter it in Render. Optional: a reachable Whisper container (`scorer/Dockerfile.transcriber`), which Render doesn't run for you. Without it, recordings aren't transcribed. |
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
3. Render deploys automatically. A healthy deploy log shows `Applying ... OK` (or `No migrations to apply`), then `Created superuser`, then Daphne's `Listening on TCP address 0.0.0.0:10000`. Later pushes deploy only after CI passes (`autoDeployTrigger: checksPass`).
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
| `recording` | The recorder on the home page and the recording websocket. When off, the page says recording is unavailable and the socket closes with `4503`. Recordings already made can still be played and deleted. |
| `transcription` | Sending recordings to Whisper. When off, the transcript comes back `null` and no text or pairs are shown. It also needs `TRANSCRIBER_URL` and `SCORER_SECRET`. |
| `keep_audio` | Eval mode: keeps the audio after playback so attempts can be promoted to eval clips. Off by default; turn it off again after collecting clips. |

`recording` and `transcription` are created switched on by migrations, `keep_audio` off. A flag that doesn't exist counts as off.

## Tests

Browser tests use [Playwright](https://playwright.dev) and live in `e2e/`. They run in Docker against the local stack on four devices: desktop Chrome, iPhone 15 and iPhone SE (WebKit, Safari's engine) and Pixel 7. They check that pages load with their styles, don't scroll sideways or overflow on phones, avoid `vh` units in our CSS, still match the approved screenshots, and that the admin login works.

Screenshot baselines are not committed (`e2e/tests/*-snapshots/` is gitignored). The first run on a machine writes them; later runs on that machine compare against them. CI starts fresh each time, so it checks layout but not visual changes.

Django unit tests cover the recording websocket (with Channels' `WebsocketCommunicator`, the services mocked), playback, cleanup, the clients and the eval bench:

```bash
docker compose exec web python manage.py test
```

On Chromium devices, Playwright also runs the full record → play → delete flow over the real websocket, using a fake microphone. It runs against `web-e2e`, a copy of `web` without Whisper and the scorer, so results come back at once (as nulls). One test passes the socket through to the server but swaps in realistic results (`page.routeWebSocket`), to check the 80 ms PCM chunks, Stop, and the word pairs on screen. Playwright's WebKit can't record, so the iPhone projects check layout only.

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
config/   Django settings, URLs, ASGI (HTTP + /ws/record/) and Celery app
core/     Recorder page and websocket (consumers.py, analysis.py), eval bench, health check, tasks
scorer/   Speech services: engine.py (shared), scorer.py (Modal GPU) and local.py + Dockerfile (wav2vec2 on CPU),
          transcriber.py + Dockerfile.transcriber (Whisper on CPU)
```

## Evals

<!-- evals:start (written by `manage.py write_eval_summary`; edits inside are replaced) -->
Latest run: **Run 2 (2026-10-31 15:18, config v1)**, 7 clips (5 scored, 1 wrong sentence, 1 too short, 0 error). Metrics use the 5 scored clips with a human label.

- **Agreement** (predicted band = human label): 40.0%
- **Spearman**, PER vs label (good < okay < off): 0.649

| Human ↓ / Predicted → | good | okay | off |
| --- | --- | --- | --- |
| good | 2 | 0 | 0 |
| okay | 0 | 0 | 2 |
| off | 0 | 1 | 0 |

Most common substitutions: `ɾ→r` ×8, `a→ɐ` ×6, `e→i` ×5, `s→z` ×4, `ɾ→ɹ` ×3.
<!-- evals:end -->
