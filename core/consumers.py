"""The recording websocket: the page streams the microphone in, the server pushes each result out as it's ready.

One socket per recording, at /ws/record/:

    page → server   binary   16 kHz mono 16-bit PCM chunks (pcm-worklet.js), while recording
    page → server   {"type": "stop"}
    server → page   {"type": "ready"}                                   on connect
    server → page   {"type": "recording", "id", "audio_url"}            the saved WAV, so Play works at once
    server → page   {"type": "phonemes", "phonemes"}                    wav2vec2 (null when the scorer is off or down)
    server → page   {"type": "transcript", "transcript", "error"?}      Whisper (null when off or failed)
    server → page   {"type": "pairs", "attempt_id", "alignment"}        both paired by word; then a normal close

The phonemes and the transcript run at the same time and are each pushed as soon as they finish, so the
page shows the fast one (phonemes, a few seconds on CPU) without waiting for the slow one (Whisper).
"""

import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.urls import reverse
from django.utils.translation import gettext as _

from . import analysis
from .models import FeatureFlag, Recording
from .views import RECORDING_FLAG

logger = logging.getLogger(__name__)

# The page stops at 60 s; a little slack, then the server hangs up on its own.
MAX_SECONDS = 75
# 16 kHz, 16-bit mono PCM is 32,000 bytes a second.
MAX_BYTES = MAX_SECONDS * analysis.SAMPLE_RATE * 2
WAV = "audio/wav"

# Close codes the page can tell apart (4000–4999 are free for applications).
CLOSE_UNAVAILABLE = 4503
CLOSE_TOO_LONG = 4413
CLOSE_FAILED = 4500


def in_thread(function):
    # The services take seconds; each call gets its own worker thread so they run side by side.
    return sync_to_async(function, thread_sensitive=False)


class RecordingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.pcm = bytearray()
        self.stopped = False
        self.finishing = None
        self.hang_up = None
        await self.accept()
        # Refusals are accept-then-close with a code, so the page can say why instead of a failed handshake.
        if not await database_sync_to_async(FeatureFlag.is_enabled)(RECORDING_FLAG):
            logger.warning("Recording socket refused: the %s feature flag is off", RECORDING_FLAG)
            await self.close(code=CLOSE_UNAVAILABLE)
            return
        self.hang_up = asyncio.get_running_loop().call_later(
            MAX_SECONDS, lambda: asyncio.ensure_future(self.close(code=CLOSE_TOO_LONG))
        )
        await self.send_json({"type": "ready"})

    async def receive(self, text_data=None, bytes_data=None):
        if self.stopped:
            return
        if bytes_data is not None:
            if len(self.pcm) + len(bytes_data) > MAX_BYTES:
                logger.warning("Recording socket closed: more than %d bytes of audio", MAX_BYTES)
                await self.close(code=CLOSE_TOO_LONG)
                return
            self.pcm.extend(bytes_data)
            return
        try:
            message = json.loads(text_data).get("type")
        except (ValueError, AttributeError):
            return
        if message == "stop":
            self.stopped = True
            if self.hang_up:
                self.hang_up.cancel()
            # Kept on the consumer so it isn't garbage-collected; it finishes even if the page leaves.
            self.finishing = asyncio.create_task(self.finish())

    async def finish(self):
        try:
            await self.analyze()
        except Exception:
            logger.exception("Recording analysis failed")
            await self.send_json({"type": "error", "error": _("Something went wrong with this recording.")})
            await self.close(code=CLOSE_FAILED)

    async def analyze(self):
        if not self.pcm:
            await self.send_json({"type": "error", "error": _("No audio was recorded.")})
            await self.close()
            return

        audio = analysis.pcm_to_wav(bytes(self.pcm))
        recording = await database_sync_to_async(Recording.objects.create)(
            audio=audio, content_type=WAV, size=len(audio)
        )
        logger.info("Recording %s created (%d bytes, %s)", recording.id, recording.size, WAV)
        await self.send_json(
            {"type": "recording", "id": str(recording.id), "audio_url": reverse("recording-audio", args=[recording.id])}
        )

        transcribe = await database_sync_to_async(analysis.transcription_enabled)()
        (result, scorer_error), (text, transcript_error, language) = await asyncio.gather(
            self.phonemes_step(recording.id, audio), self.transcript_step(recording.id, audio, transcribe)
        )

        heard = result["phonemes"] if result else None
        alignment = await in_thread(analysis.pairs)(recording.id, text, heard, language)
        attempt = await database_sync_to_async(analysis.save_attempt)(
            recording,
            text=text,
            transcript_error=transcript_error,
            language=language,
            result=result,
            scorer_error=scorer_error,
            alignment=alignment,
        )
        await self.send_json({"type": "pairs", "attempt_id": str(attempt.id), "alignment": alignment})
        await self.close()

    async def phonemes_step(self, recording_id, audio):
        result, error = await in_thread(analysis.phonemes)(recording_id, audio, WAV)
        await self.send_json({"type": "phonemes", "phonemes": result["phonemes"] if result else None})
        return result, error

    async def transcript_step(self, recording_id, audio, enabled):
        if not enabled:
            await self.send_json({"type": "transcript", "transcript": None})
            return None, "", None
        text, error, language = await in_thread(analysis.transcript)(recording_id, audio, WAV)
        message = {"type": "transcript", "transcript": text}
        if error:
            message["error"] = _("Couldn't transcribe this recording.")
        await self.send_json(message)
        return text, error, language

    async def send_json(self, data):
        await self.send(text_data=json.dumps(data))

    async def disconnect(self, code):
        if self.hang_up:
            self.hang_up.cancel()
        if not self.stopped and self.pcm:
            logger.info("Recording socket closed before Stop: %d bytes of audio discarded", len(self.pcm))
