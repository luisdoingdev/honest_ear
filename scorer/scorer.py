"""Honest Ear phoneme service on Modal (T4 GPU): a recorded clip in, an IPA phoneme string out.

Separate from the Django app (it isn't in the Django image). Deploy with `modal deploy scorer/scorer.py`.
The model and request handling live in engine.py, shared with the local CPU container (local.py).
"""

from __future__ import annotations

import os

import modal

# Weights (~1.2 GB) live on a volume, so a cold container loads them from disk instead of the Hub.
CACHE_DIR = "/cache"

image = (
    modal.Image.debian_slim(python_version="3.12")
    # Plain apt_install pulls ffmpeg's ~270 desktop recommendations (GTK, Mesa, systemd); decoding needs none.
    .run_commands("apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*")
    .pip_install("torch", "transformers", "librosa", "soundfile", "fastapi[standard]", "numpy")
    .env({"HF_HOME": CACHE_DIR})
    .add_local_python_source("engine")
)
cache = modal.Volume.from_name("honest-ear-hf-cache", create_if_missing=True)
app = modal.App("honest-ear-scorer", image=image)

# Only installed in the container; deploying from a laptop without them still works.
with image.imports():
    import fastapi

    import engine


@app.cls(
    gpu="T4",
    scaledown_window=300,
    secrets=[modal.Secret.from_name("honest-ear-scorer")],
    volumes={CACHE_DIR: cache},
)
class Scorer:
    @modal.enter()
    def load(self):
        """Runs once per container, not per request."""
        self.recognizer = engine.PhonemeRecognizer(device="cuda")
        cache.commit()

    @modal.fastapi_endpoint(method="POST")
    async def phonemes(self, request: fastapi.Request):
        from fastapi.responses import JSONResponse

        status, body = engine.handle(
            await request.body(),
            request.headers.get(engine.SECRET_HEADER),
            os.environ.get("SCORER_SECRET"),
            self.recognizer,
        )
        return JSONResponse(body, status_code=status)
