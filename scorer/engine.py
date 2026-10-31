"""What both phoneme services share: decode a clip, run wav2vec2, answer the request.

`scorer.py` (Modal, GPU) and `local.py` (Docker, CPU) are thin wrappers around this, so the two answer
exactly alike. No scoring yet: this only returns what the model heard.
"""

from __future__ import annotations

import hmac
import subprocess
import time

MODEL_ID = "facebook/wav2vec2-lv-60-espeak-cv-ft"
SAMPLE_RATE = 16000
SECRET_HEADER = "x-scorer-secret"


class DecodeError(Exception):
    pass


def decode_audio(data: bytes):
    """Any container ffmpeg reads (WebM/Opus from Chrome, MP4/AAC from iPhones) → 16 kHz mono float32."""
    import numpy as np

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise DecodeError(result.stderr.decode(errors="replace").strip()[-300:] or "no audio decoded")
    return np.frombuffer(result.stdout, dtype=np.float32)


def secret_matches(sent: str | None, expected: str | None) -> bool:
    # Constant-time comparison; an unset secret on the server never matches.
    return bool(expected) and bool(sent) and hmac.compare_digest(sent.encode(), expected.encode())


class PhonemeRecognizer:
    """Loaded once per process (container), then reused for every request."""

    def __init__(self, device: str, quantize: bool = False):
        import torch
        from transformers import (
            Wav2Vec2FeatureExtractor,
            Wav2Vec2ForCTC,
            Wav2Vec2PhonemeCTCTokenizer,
            Wav2Vec2Processor,
        )

        # do_phonemize=False: phonemizing turns *text* into phonemes and needs espeak; decoding the model's
        # output (all this service does) doesn't.
        tokenizer = Wav2Vec2PhonemeCTCTokenizer.from_pretrained(MODEL_ID, do_phonemize=False)
        feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(MODEL_ID)
        self.processor = Wav2Vec2Processor(feature_extractor=feature_extractor, tokenizer=tokenizer)
        model = Wav2Vec2ForCTC.from_pretrained(MODEL_ID).eval()
        if quantize:
            # int8 weights for the linear layers: about a quarter of the memory and faster on CPU,
            # at the cost of slightly different phonemes than the full-precision model.
            model = torch.ao.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
        self.model = model.to(device)
        self.device = device
        self.quantized = quantize
        self.torch = torch

    def recognize(self, audio) -> dict:
        torch = self.torch
        inputs = self.processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt").input_values.to(self.device)
        sync = torch.cuda.synchronize if self.device == "cuda" else (lambda: None)
        sync()
        started = time.perf_counter()
        with torch.inference_mode():
            logits = self.model(inputs).logits
        ids = torch.argmax(logits, dim=-1)
        sync()
        inference_ms = (time.perf_counter() - started) * 1000
        return {
            "phonemes": self.processor.batch_decode(ids)[0],
            "inference_ms": round(inference_ms, 1),
            "duration_s": round(len(audio) / SAMPLE_RATE, 3),
        }


def handle(body: bytes, sent_secret: str | None, expected_secret: str | None, recognizer: PhonemeRecognizer):
    """One request, as (HTTP status, JSON body). Same contract for Modal and the local container."""
    if not secret_matches(sent_secret, expected_secret):
        return 401, {"error": "invalid or missing secret"}
    if not body:
        return 400, {"error": "empty body: send the recording's raw bytes"}
    try:
        audio = decode_audio(body)
    except DecodeError as exc:
        return 422, {"error": f"could not decode audio: {exc}"}
    return 200, recognizer.recognize(audio)
