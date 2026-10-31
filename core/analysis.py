"""What one recording gets after Stop: wav2vec2's phonemes, Whisper's transcript, and the two paired by word.

Plain sync functions, called from the recording websocket (core.consumers) in worker threads. None of them
raise: each failure is logged and returned as a short reason, so one service being down never loses the other.
"""

import io
import logging
import wave

from . import pronunciation, scoring, transcription
from .models import Attempt, FeatureFlag

logger = logging.getLogger(__name__)

TRANSCRIPTION_FLAG = "transcription"
# The page streams 16 kHz mono 16-bit PCM (pcm-worklet.js).
SAMPLE_RATE = 16_000


def pcm_to_wav(pcm):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def transcription_enabled():
    return FeatureFlag.is_enabled(TRANSCRIPTION_FLAG) and transcription.is_configured()


def phonemes(recording_id, audio, content_type):
    """(result, error). result is None when the scorer is off or down."""
    if not scoring.is_configured():
        return None, ""
    try:
        result = scoring.phonemes_for(audio, content_type)
    except scoring.ScorerError as exc:
        logger.warning("Recording %s not scored: %s", recording_id, exc)
        return None, str(exc)
    logger.info(
        "Recording %s phonemes (%d characters, %.0f ms inference, %.2f s audio)",
        recording_id, len(result["phonemes"]), result["inference_ms"], result["duration_s"],
    )
    return result, ""


def transcript(recording_id, audio, content_type):
    """(text, error, language). Whisper detects the language; text is None when it failed."""
    try:
        text, language = transcription.transcribe(audio, content_type)
    except transcription.TranscriptionError as exc:
        logger.warning("Recording %s not transcribed: %s", recording_id, exc)
        return None, str(exc), None
    # Only the length is logged: the words themselves are the visitor's private speech.
    logger.info("Recording %s transcribed (%s, %d characters)", recording_id, language, len(text))
    return text, "", language


def pairs(recording_id, text, heard, language):
    """The transcript's expected phonemes paired with the heard ones, word by word; [] when it can't be done.

    This shows drift against what was said (the transcript), not a score against an intended sentence.
    """
    if not text or not heard or language not in pronunciation.ESPEAK_VOICES:
        return []
    try:
        return pronunciation.align(pronunciation.expected_phonemes(text, language), heard.split())
    except Exception:
        logger.exception("Recording %s phonemes not paired", recording_id)
        return []


def save_attempt(recording, *, text, transcript_error, language, result, scorer_error, alignment):
    return Attempt.objects.create(
        recording=recording,
        transcript=text,
        transcript_source=Attempt.TranscriptSource.LOCAL if text is not None else Attempt.TranscriptSource.NONE,
        transcript_error=transcript_error[:300],
        phonemes=result["phonemes"] if result else None,
        phoneme_inference_ms=result["inference_ms"] if result else None,
        audio_duration_s=result["duration_s"] if result else None,
        scorer_error=scorer_error[:300],
        alignment=alignment,
        alignment_language=language if alignment else "",
    )
