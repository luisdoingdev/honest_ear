"""Honest Ear phoneme service in a local Docker container (CPU): same request and answer as the Modal one.

Run with `docker compose --profile scorer up -d scorer`; Django reaches it at http://scorer:8000/.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

import engine

logger = logging.getLogger("scorer")
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loaded once at startup, not per request; /health reports ready only after this.
    quantize = os.environ.get("SCORER_QUANTIZE", "") == "1"
    started = time.perf_counter()
    state["recognizer"] = await run_in_threadpool(engine.PhonemeRecognizer, device="cpu", quantize=quantize)
    logger.warning("Model %s loaded on CPU%s in %.1fs", engine.MODEL_ID, " (int8)" if quantize else "", time.perf_counter() - started)
    yield
    state.clear()


app = FastAPI(lifespan=lifespan)


@app.post("/")
async def phonemes(request: Request):
    # Inference is CPU-bound: run it off the event loop so /health still answers meanwhile.
    status, body = await run_in_threadpool(
        engine.handle,
        await request.body(),
        request.headers.get(engine.SECRET_HEADER),
        os.environ.get("SCORER_SECRET"),
        state["recognizer"],
    )
    return JSONResponse(body, status_code=status)


@app.get("/health")
async def health():
    recognizer = state.get("recognizer")
    return {"model": engine.MODEL_ID, "device": "cpu", "quantized": bool(recognizer and recognizer.quantized)}
