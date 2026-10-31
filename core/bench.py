"""Runs the eval bench: each clip through the real pipeline, scored by core.pronunciation, saved as an EvalRun.

The question it answers: does the pronunciation score agree with human judgement, and how often?
"""

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import pronunciation, scoring, transcription
from .models import EvalResult, EvalRun

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}
# Ordinal for agreement and correlation: a higher PER should go with a worse label.
LABEL_RANK = {"good": 0, "okay": 1, "off": 2}


def run_bench(clips, config=None):
    """Evaluates every clip and saves the run with its config snapshot and summary. Never raises per clip."""
    config = config or pronunciation.load_config()
    run = EvalRun.objects.create(config_version=str(config["version"]), config=config)
    results = [evaluate_clip(run, clip, config) for clip in clips]
    run.summary = summarize(results)
    run.save(update_fields=["summary"])
    logger.info(
        "Eval run %s: %d clips, agreement %s, Spearman %s",
        run.pk, len(results), run.summary["agreement"], run.summary["spearman"],
    )
    return run


def evaluate_clip(run, clip, config):
    result = EvalResult(run=run, clip=clip, human_label=clip.human_label)
    if not clip.intended_text.strip():
        return _save(result, status=EvalResult.Status.ERROR, error="No intended text: type what was meant first")

    with clip.audio.open("rb") as handle:
        audio = handle.read()
    content_type = CONTENT_TYPES.get(Path(clip.audio.name).suffix.lstrip(".").lower(), "application/octet-stream")

    # Both services at once, as for a live recording.
    with ThreadPoolExecutor(max_workers=1) as pool:
        heard = pool.submit(_phonemes, audio, content_type)
        transcript, transcript_error = _transcript(audio, content_type)
        phonemes, scorer_error = heard.result()
    result.transcript = transcript or ""
    if phonemes:
        result.heard_phonemes = phonemes["phonemes"]
        result.duration_s = phonemes["duration_s"]
    errors = [e for e in (scorer_error, transcript_error) if e]
    if errors:
        # Without the duration and phonemes, or the transcript for the wrong-sentence gate, there's no verdict.
        return _save(result, status=EvalResult.Status.ERROR, error="; ".join(errors))

    try:
        # espeak runs here, on this thread: it isn't thread-safe.
        verdict = pronunciation.score(
            intended_text=clip.intended_text,
            language=clip.language,
            transcript=transcript,
            heard_phonemes=result.heard_phonemes,
            duration_s=result.duration_s,
            config=config,
        )
    except Exception as exc:
        logger.exception("Eval clip %s could not be scored", clip.pk)
        return _save(result, status=EvalResult.Status.ERROR, error=f"Scoring failed: {type(exc).__name__}: {exc}")

    return _save(
        result,
        status=verdict["status"],
        per=verdict["per"],
        predicted_band=verdict["band"] or "",
        alignment=verdict["alignment"],
        word_overlap=verdict["overlap"],
    )


def _save(result, **fields):
    for name, value in fields.items():
        setattr(result, name, value)
    result.error = result.error[:500]
    result.save()
    return result


def _transcript(audio, content_type):
    if not transcription.is_configured():
        return None, "Deepgram not configured (DEEPGRAM_API_KEY)"
    try:
        return transcription.transcribe(audio, content_type), ""
    except transcription.TranscriptionError as exc:
        return None, str(exc)


def _phonemes(audio, content_type):
    if not scoring.is_configured():
        return None, "Phoneme scorer not configured (SCORER_URL, SCORER_SECRET)"
    try:
        return scoring.phonemes_for(audio, content_type), ""
    except scoring.ScorerError as exc:
        return None, str(exc)


def spearman(xs, ys):
    """Spearman's rank correlation (ties get average ranks); None when it isn't defined."""
    if len(xs) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mean_x, mean_y = sum(rx) / len(rx), sum(ry) / len(ry)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    spread = (sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry)) ** 0.5
    return round(covariance / spread, 3) if spread else None


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def summarize(results):
    """Run-level metrics over the clips that were scored and have a human label."""
    statuses = Counter(r.status for r in results)
    judged = [r for r in results if r.status == EvalResult.Status.SCORED and r.human_label]
    confusion = {human: {band: 0 for band in LABEL_RANK} for human in LABEL_RANK}
    for r in judged:
        confusion[r.human_label][r.predicted_band] += 1
    agreed = sum(r.predicted_band == r.human_label for r in judged)
    substitutions = Counter(
        f"{op['expected']}→{op['heard']}"
        for r in results
        if r.status == EvalResult.Status.SCORED
        for word in r.alignment
        for op in word["ops"]
        if op["op"] == "sub"
    )
    return {
        "clips": len(results),
        "statuses": {status: statuses.get(status, 0) for status in EvalResult.Status.values},
        "judged": len(judged),
        "agreement": round(agreed / len(judged) * 100, 1) if judged else None,
        "spearman": spearman([r.per for r in judged], [LABEL_RANK[r.human_label] for r in judged]),
        "confusion": confusion,
        "top_substitutions": substitutions.most_common(10),
    }
