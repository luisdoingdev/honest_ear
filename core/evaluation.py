"""Scores an EvalClip against what its speaker meant to say, through the same pipeline as a live recording.

- Word score: Deepgram's transcript against the intended text, 100 × (1 − word error rate).
- Phoneme score: the phonemes wav2vec2 heard against the intended text's expected phonemes (espeak-ng,
  the same phonemizer this model's training labels came from), 100 × (1 − edit distance ÷ expected length).
"""

import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.utils import timezone

from . import scoring, transcription

logger = logging.getLogger(__name__)

# espeak-ng voices: Latin American Spanish (the audience) and US English (what the model was tuned on).
ESPEAK_VOICES = {"es": "es-419", "en": "en-us"}
CONTENT_TYPES = {
    "webm": "audio/webm",
    "ogg": "audio/ogg",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


def expected_phonemes(text, language):
    """The intended text as space-separated IPA phonemes, in the model's own format (no stress, no word breaks)."""
    from phonemizer import phonemize
    from phonemizer.separator import Separator

    phonemized = phonemize(
        text,
        language=ESPEAK_VOICES[language],
        backend="espeak",
        separator=Separator(phone=" ", word=" | ", syllable=""),
        strip=True,
        preserve_punctuation=False,
        with_stress=False,
    )
    return " ".join(token for token in phonemized.split() if token != "|")


def edit_distance(expected, heard):
    """Levenshtein distance between two token lists (substitutions, insertions and deletions all cost 1)."""
    previous = list(range(len(heard) + 1))
    for i, want in enumerate(expected, 1):
        current = [i]
        for j, got in enumerate(heard, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (want != got)))
        previous = current
    return previous[-1]


def similarity(expected, heard):
    """0–100; None when there is nothing to compare against."""
    if not expected:
        return None
    return round(max(0.0, 1 - edit_distance(expected, heard) / len(expected)) * 100, 1)


def words(text):
    # Case, punctuation and spacing don't count against the speaker; accents do (más ≠ mas).
    text = unicodedata.normalize("NFC", text).lower()
    return re.findall(r"[\w']+", text)


def word_score(intended_text, transcript):
    return similarity(words(intended_text), words(transcript))


def phoneme_score(expected, heard):
    return similarity(expected.split(), heard.split())


def evaluate(clip):
    """Runs one clip through Deepgram and the phoneme scorer (at the same time) and saves both scores on it.

    Each half that fails leaves its score empty and adds its reason to eval_error; the other half still saves.
    """
    with clip.audio.open("rb") as handle:
        audio = handle.read()
    content_type = CONTENT_TYPES.get(Path(clip.audio.name).suffix.lstrip(".").lower(), "application/octet-stream")
    errors = []

    with ThreadPoolExecutor(max_workers=1) as pool:
        heard = pool.submit(_phonemes, audio, content_type)
        transcript, transcript_error = _transcript(audio, content_type)
        phonemes, scorer_error = heard.result()
    errors += [e for e in (transcript_error, scorer_error) if e]

    try:
        # espeak runs here, not in the worker thread: it isn't thread-safe.
        clip.expected_phonemes = expected_phonemes(clip.intended_text, clip.language)
    except Exception as exc:
        clip.expected_phonemes = ""
        errors.append(f"Expected phonemes failed: {type(exc).__name__}: {exc}")

    clip.heard_transcript = transcript or ""
    clip.heard_phonemes = phonemes or ""
    clip.word_score = word_score(clip.intended_text, transcript) if transcript is not None else None
    clip.phoneme_score = (
        phoneme_score(clip.expected_phonemes, phonemes) if phonemes is not None and clip.expected_phonemes else None
    )
    clip.eval_error = "; ".join(errors)[:500]
    clip.evaluated_at = timezone.now()
    clip.save()
    logger.info(
        "Eval clip %s (%s): phoneme score %s, word score %s%s",
        clip.pk, clip.label, clip.phoneme_score, clip.word_score, " with errors" if errors else "",
    )
    return clip


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
        return scoring.phonemes_for(audio, content_type)["phonemes"], ""
    except scoring.ScorerError as exc:
        return None, str(exc)
