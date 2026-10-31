"""Local speech-to-text (faster-whisper on CPU), so local development and the eval bench don't need Deepgram.

Same shape as the phoneme scorer: POST the raw clip with the X-Scorer-Secret header, optionally ?language=es|en.
Part of the local stack (./dev.sh); Django reaches it at http://transcriber:8000/. Production keeps Deepgram.
"""

from __future__ import annotations

import io
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from engine import SECRET_HEADER, secret_matches

MODEL = os.environ.get("TRANSCRIBER_MODEL", "medium")
LANGUAGES = {"es", "en"}
logger = logging.getLogger("transcriber")
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    # int8 weights: about a quarter of the memory of float32, and faster on CPU.
    state["model"] = await run_in_threadpool(WhisperModel, MODEL, device="cpu", compute_type="int8")
    logger.warning("Whisper %s loaded on CPU (int8) in %.1fs", MODEL, time.perf_counter() - started)
    yield
    state.clear()


app = FastAPI(lifespan=lifespan)


def transcribe(audio: bytes, language: str | None) -> dict:
    started = time.perf_counter()
    segments, info = state["model"].transcribe(
        io.BytesIO(audio),
        language=language,
        # Deterministic, and no carrying text across segments: what was said, not what fits the last sentence.
        temperature=0.0,
        condition_on_previous_text=False,
        # Skips silence, where Whisper tends to invent words.
        vad_filter=True,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {
        "transcript": text,
        "language": info.language,
        "duration_s": round(info.duration, 3),
        "inference_ms": round((time.perf_counter() - started) * 1000, 1),
        "model": f"faster-whisper-{MODEL}-int8",
    }


@app.post("/")
async def post(request: Request):
    if not secret_matches(request.headers.get(SECRET_HEADER), os.environ.get("SCORER_SECRET")):
        return JSONResponse({"error": "invalid or missing secret"}, status_code=401)
    # Uploads send no language and Whisper detects it (like Deepgram's multi); the eval bench sends the clip's.
    language = request.query_params.get("language") or None
    if language is not None and language not in LANGUAGES:
        return JSONResponse({"error": f"language must be one of {sorted(LANGUAGES)}"}, status_code=400)
    body = await request.body()
    if not body:
        return JSONResponse({"error": "empty body: send the recording's raw bytes"}, status_code=400)
    try:
        result = await run_in_threadpool(transcribe, body, language)
    except Exception as exc:  # PyAV raises its own types for undecodable input
        return JSONResponse({"error": f"could not transcribe audio: {type(exc).__name__}: {exc}"[:300]}, status_code=422)
    return result


@app.get("/health")
async def health():
    return {"model": f"faster-whisper-{MODEL}-int8", "device": "cpu"}
