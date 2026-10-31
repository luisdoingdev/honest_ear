import asyncio
import json
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

import httpx
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from config.asgi import application
from . import scoring, transcription
from .analysis import pcm_to_wav
from .consumers import RecordingConsumer
from .models import Attempt, EvalClip, FeatureFlag, Recording
from .tasks import delete_stale_recordings


# Real URLs in .env must never make tests call the scorer or the transcriber.
@override_settings(SCORER_URL="", TRANSCRIBER_URL="")
class OfflineTestCase(TestCase):
    pass


# 0.1 s of 16 kHz 16-bit PCM, as pcm-worklet.js sends it.
PCM_CHUNK = b"\x01\x00" * 1600


def make_recording(content_type="audio/wav", audio=b"\x01" * 1024):
    return Recording.objects.create(audio=audio, content_type=content_type, size=len(audio))


def socket():
    """The page's side of /ws/record/, straight to the consumer (the origin check is tested on its own)."""
    return WebsocketCommunicator(RecordingConsumer.as_asgi(), "/ws/record/")


async def record(*chunks):
    """Streams the chunks, stops, and returns every message the server pushed and its close code."""
    ws = socket()
    connected, _ = await ws.connect()
    assert connected
    assert await ws.receive_json_from() == {"type": "ready"}
    for chunk in chunks:
        await ws.send_to(bytes_data=chunk)
    await ws.send_to(text_data=json.dumps({"type": "stop"}))
    messages = []
    while True:
        output = await ws.receive_output(timeout=10)
        if output["type"] == "websocket.close":
            return messages, output.get("code", 1000)
        messages.append(json.loads(output["text"]))


class RecordingPlaybackTests(OfflineTestCase):
    def test_fetch_then_delete(self):
        recording = make_recording()
        audio = self.client.get(f"/api/recordings/{recording.pk}/audio/")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content, b"\x01" * 1024)
        self.assertEqual(audio["Content-Type"], "audio/wav")
        self.assertEqual(audio["Cache-Control"], "no-store")

        self.assertEqual(self.client.delete(f"/api/recordings/{recording.pk}/").status_code, 204)
        self.assertFalse(Recording.objects.exists())

    def test_deleted_recording_is_gone_and_delete_is_repeatable(self):
        recording = make_recording()
        self.client.delete(f"/api/recordings/{recording.pk}/")
        self.assertEqual(self.client.get(f"/api/recordings/{recording.pk}/audio/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/recordings/{recording.pk}/").status_code, 204)

    def test_logs_playback_and_deletion(self):
        recording = make_recording()
        rid = recording.pk
        with self.assertLogs("core.views", level="INFO") as logs:
            self.client.get(f"/api/recordings/{rid}/audio/")
            self.client.delete(f"/api/recordings/{rid}/")
            self.client.delete(f"/api/recordings/{rid}/")
        self.assertEqual(
            logs.output,
            [
                f"INFO:core.views:Recording {rid} served (1024 bytes)",
                f"INFO:core.views:Recording {rid} deleted after playback",
                f"INFO:core.views:Recording {rid} delete requested but already gone",
            ],
        )

    def test_home_page_is_the_recorder(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="csrf-token"')
        self.assertContains(res, "Start session")
        self.assertNotContains(res, "data-live-captions")

    def test_old_record_url_redirects_home(self):
        self.assertRedirects(self.client.get("/record/"), "/", fetch_redirect_response=False)

    def test_the_upload_and_token_endpoints_are_gone(self):
        self.assertEqual(self.client.post("/api/recordings/").status_code, 404)
        self.assertEqual(self.client.post("/api/transcription/token/").status_code, 404)


SPANISH = {"phonemes": "m e ɣ u s t a ɾ i a r e s e ɾ β a", "inference_ms": 900.0, "duration_s": 2.1}


@override_settings(TRANSCRIBER_URL="http://transcriber:8000/", SCORER_URL="http://scorer:8000/", SCORER_SECRET="s")
class RecordingSocketTests(TransactionTestCase):
    """The page streams PCM up; the server saves a WAV and pushes wav2vec2's and Whisper's results back.

    TransactionTestCase: Channels closes the database connection around each sync call, which TestCase's
    wrapping transaction can't survive. serialized_rollback restores the seeded feature flags after each test.
    """

    serialized_rollback = True

    def setUp(self):
        patches = {
            "whisper": mock.patch("core.transcription.transcribe", return_value=("Me gustaría reservar", "es")),
            "scorer": mock.patch("core.scoring.phonemes_for", return_value=SPANISH),
        }
        self.mocks = {name: patcher.start() for name, patcher in patches.items()}
        for patcher in patches.values():
            self.addCleanup(patcher.stop)

    async def test_saves_a_wav_then_pushes_phonemes_transcript_and_pairs(self):
        messages, code = await record(PCM_CHUNK, PCM_CHUNK)

        self.assertEqual(code, 1000)
        self.assertEqual([m["type"] for m in messages][0], "recording", "Play works before any result is in")
        self.assertEqual({m["type"] for m in messages[1:3]}, {"phonemes", "transcript"})
        self.assertEqual(messages[-1]["type"], "pairs")
        by_type = {m["type"]: m for m in messages}

        recording = await Recording.objects.aget(pk=by_type["recording"]["id"])
        self.assertEqual(by_type["recording"]["audio_url"], f"/api/recordings/{recording.pk}/audio/")
        self.assertEqual(recording.content_type, "audio/wav")
        self.assertEqual(bytes(recording.audio), pcm_to_wav(PCM_CHUNK * 2))
        # Both services get the same WAV; Whisper detects the language.
        self.assertEqual(self.mocks["scorer"].call_args.args, (bytes(recording.audio), "audio/wav"))
        self.assertEqual(self.mocks["whisper"].call_args.args, (bytes(recording.audio), "audio/wav"))

        self.assertEqual(by_type["phonemes"], {"type": "phonemes", "phonemes": SPANISH["phonemes"]})
        self.assertEqual(by_type["transcript"], {"type": "transcript", "transcript": "Me gustaría reservar"})
        attempt = await Attempt.objects.aget(pk=by_type["pairs"]["attempt_id"])
        self.assertEqual(attempt.transcript_source, Attempt.TranscriptSource.LOCAL)
        self.assertEqual(attempt.phonemes, SPANISH["phonemes"])
        self.assertEqual(attempt.alignment, by_type["pairs"]["alignment"])
        self.assertEqual(attempt.alignment_language, "es")

    async def test_pairs_each_word_with_its_expected_and_heard_phonemes(self):
        messages, _ = await record(PCM_CHUNK)
        alignment = messages[-1]["alignment"]
        self.assertEqual([w["word"] for w in alignment], ["me", "gustaría", "reservar"])
        self.assertEqual([op["op"] for op in alignment[0]["ops"]], ["match", "match"])
        reservar = alignment[2]["ops"]
        # Heard-only phonemes stay with their word; drifts are marked, matches aren't.
        self.assertEqual("".join(op["heard"] or "" for op in reservar), "reseɾβa")
        self.assertTrue(any(op["op"] != "match" for op in reservar))

    async def test_phonemes_arrive_without_waiting_for_whisper(self):
        whisper_may_finish = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_whisper(*_):
            asyncio.run_coroutine_threadsafe(whisper_may_finish.wait(), loop).result(timeout=5)
            return "Me gustaría reservar", "es"

        self.mocks["whisper"].side_effect = slow_whisper
        ws = socket()
        await ws.connect()
        await ws.receive_json_from()
        await ws.send_to(bytes_data=PCM_CHUNK)
        await ws.send_to(text_data=json.dumps({"type": "stop"}))
        self.assertEqual((await ws.receive_json_from(timeout=5))["type"], "recording")
        self.assertEqual((await ws.receive_json_from(timeout=5))["type"], "phonemes", "while Whisper is still busy")
        whisper_may_finish.set()
        self.assertEqual((await ws.receive_json_from(timeout=5))["type"], "transcript")
        self.assertEqual((await ws.receive_json_from(timeout=5))["type"], "pairs")
        await ws.disconnect()

    async def test_a_service_failing_never_loses_the_other(self):
        self.mocks["whisper"].side_effect = transcription.TranscriptionError("Transcriber timed out after 60s")
        with self.assertLogs("core.analysis", level="WARNING"):
            messages, code = await record(PCM_CHUNK)
        by_type = {m["type"]: m for m in messages}
        self.assertEqual(code, 1000)
        self.assertEqual(by_type["transcript"], {"type": "transcript", "transcript": None, "error": "Couldn't transcribe this recording."})
        self.assertEqual(by_type["phonemes"]["phonemes"], SPANISH["phonemes"])
        self.assertEqual(by_type["pairs"]["alignment"], [])
        attempt = await Attempt.objects.aget()
        self.assertEqual(attempt.transcript_error, "Transcriber timed out after 60s")
        self.assertEqual(attempt.transcript_source, Attempt.TranscriptSource.NONE)
        self.assertEqual(attempt.phonemes, SPANISH["phonemes"])

    async def test_scorer_down_still_saves_the_transcript(self):
        self.mocks["scorer"].side_effect = scoring.ScorerError("Scorer timed out after 30s (cold start?)")
        with self.assertLogs("core.analysis", level="WARNING"):
            messages, _ = await record(PCM_CHUNK)
        by_type = {m["type"]: m for m in messages}
        self.assertIsNone(by_type["phonemes"]["phonemes"])
        self.assertEqual(by_type["transcript"]["transcript"], "Me gustaría reservar")
        attempt = await Attempt.objects.aget()
        self.assertEqual(attempt.scorer_error, "Scorer timed out after 30s (cold start?)")
        self.assertEqual(attempt.alignment_language, "")

    async def test_nothing_is_paired_for_a_language_without_an_espeak_voice(self):
        self.mocks["whisper"].return_value = ("Eu gostaria", "pt")
        messages, _ = await record(PCM_CHUNK)
        self.assertEqual(messages[-1]["alignment"], [])

    @override_settings(TRANSCRIBER_URL="", SCORER_URL="")
    async def test_without_the_services_the_recording_still_works(self):
        messages, code = await record(PCM_CHUNK)
        self.assertEqual(code, 1000)
        by_type = {m["type"]: m for m in messages}
        self.assertEqual(by_type["transcript"], {"type": "transcript", "transcript": None})
        self.assertIsNone(by_type["phonemes"]["phonemes"])
        self.mocks["whisper"].assert_not_called()
        self.mocks["scorer"].assert_not_called()

    async def test_transcription_flag_off_skips_whisper(self):
        await FeatureFlag.objects.filter(name="transcription").aupdate(enabled=False)
        messages, _ = await record(PCM_CHUNK)
        self.assertIsNone({m["type"]: m for m in messages}["transcript"]["transcript"])
        self.mocks["whisper"].assert_not_called()

    async def test_stop_without_audio_is_an_error_and_saves_nothing(self):
        messages, code = await record()
        self.assertEqual(messages, [{"type": "error", "error": "No audio was recorded."}])
        self.assertEqual(code, 1000)
        self.assertFalse(await Recording.objects.aexists())

    async def test_leaving_before_stop_saves_nothing(self):
        ws = socket()
        await ws.connect()
        await ws.receive_json_from()
        await ws.send_to(bytes_data=PCM_CHUNK)
        await ws.disconnect()
        self.assertFalse(await Recording.objects.aexists())
        self.mocks["scorer"].assert_not_called()

    async def test_hangs_up_after_too_much_audio(self):
        ws = socket()
        await ws.connect()
        await ws.receive_json_from()
        with mock.patch("core.consumers.MAX_BYTES", len(PCM_CHUNK) + 1), self.assertLogs("core.consumers", level="WARNING"):
            await ws.send_to(bytes_data=PCM_CHUNK)
            await ws.send_to(bytes_data=PCM_CHUNK)
            self.assertEqual(await ws.receive_output(timeout=2), {"type": "websocket.close", "code": 4413})
        self.assertFalse(await Recording.objects.aexists())

    async def test_ignores_unknown_and_malformed_messages(self):
        ws = socket()
        await ws.connect()
        await ws.receive_json_from()
        await ws.send_to(text_data="not json")
        await ws.send_to(text_data=json.dumps({"type": "Configure"}))
        self.assertTrue(await ws.receive_nothing(timeout=0.2))
        await ws.disconnect()

    async def test_phonemes_and_words_are_never_logged(self):
        with self.assertLogs("core", level="INFO") as logs:
            await record(PCM_CHUNK)
        joined = "\n".join(logs.output)
        self.assertIn("phonemes (33 characters, 900 ms inference, 2.10 s audio)", joined)
        self.assertIn("transcribed (es, 20 characters)", joined)
        self.assertNotIn("ɣ", joined)
        self.assertNotIn("gustaría", joined)

    async def test_the_socket_is_refused_when_recording_is_off(self):
        await FeatureFlag.objects.filter(name="recording").aupdate(enabled=False)
        with self.assertLogs("core.consumers", level="WARNING") as logs:
            ws = socket()
            await ws.connect()
            self.assertEqual(await ws.receive_output(timeout=2), {"type": "websocket.close", "code": 4503})
        self.assertIn("feature flag is off", logs.output[0])

    async def test_pages_from_other_sites_cant_open_a_socket(self):
        ws = WebsocketCommunicator(application, "/ws/record/", headers=[(b"origin", b"https://evil.example")])
        connected, _ = await ws.connect()
        self.assertFalse(connected)


class LanguageTests(OfflineTestCase):
    def test_spanish_browsers_get_spanish(self):
        # es-CO (Colombia: Bogotá, Medellín) is the main audience.
        for header in ["es-CO,es;q=0.9", "es-CO", "es-419", "es-MX", "es"]:
            with self.subTest(header=header):
                res = self.client.get("/", headers={"accept-language": header})
                self.assertContains(res, '<html lang="es">')
                self.assertContains(res, 'aria-label="Empezar"')
                self.assertEqual(res["Content-Language"], "es")

    def test_other_browsers_get_english(self):
        for header in ["en-US", "pt-BR,pt;q=0.9", "fr-FR"]:
            with self.subTest(header=header):
                res = self.client.get("/", headers={"accept-language": header})
                self.assertContains(res, '<html lang="en">')
                self.assertContains(res, 'aria-label="Start session"')

    def test_varies_by_language_so_caches_keep_them_apart(self):
        res = self.client.get("/", headers={"accept-language": "es-MX"})
        self.assertIn("Accept-Language", res["Vary"])


def scorer_reply(status=200, body=None):
    body = body if body is not None else {"phonemes": "o l a k o m o", "inference_ms": 41.5, "duration_s": 2.48}
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://scorer.example/phonemes"))


@override_settings(SCORER_URL="https://scorer.example/phonemes", SCORER_SECRET="scorer-secret", SCORER_TIMEOUT_SECONDS=7)
@mock.patch("core.scoring.httpx.post")
class ScorerClientTests(SimpleTestCase):
    def test_sends_the_raw_clip_with_the_shared_secret_and_parses_the_reply(self, post):
        post.return_value = scorer_reply()
        result = scoring.phonemes_for(b"webm-bytes", "audio/webm")

        self.assertEqual(result, {"phonemes": "o l a k o m o", "inference_ms": 41.5, "duration_s": 2.48})
        self.assertEqual(post.call_args.args, ("https://scorer.example/phonemes",))
        self.assertEqual(post.call_args.kwargs["content"], b"webm-bytes")
        self.assertEqual(
            post.call_args.kwargs["headers"], {"Content-Type": "audio/webm", "X-Scorer-Secret": "scorer-secret"}
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 7)

    def test_failures_become_scorer_errors_without_the_secret(self, post):
        cases = [
            (httpx.ReadTimeout("slow"), "timed out after 7s (cold start?)"),
            (httpx.ConnectError("refused"), "Scorer request failed: ConnectError"),
            (scorer_reply(status=401, body={"error": "invalid or missing secret"}), "rejected the secret (HTTP 401)"),
            (scorer_reply(status=500, body={}), "Scorer returned HTTP 500"),
            (scorer_reply(body={"error": "?"}), "Scorer response had no phonemes"),
        ]
        for outcome, message in cases:
            with self.subTest(message=message):
                post.reset_mock(side_effect=True, return_value=True)
                if isinstance(outcome, Exception):
                    post.side_effect = outcome
                else:
                    post.return_value = outcome
                with self.assertRaises(scoring.ScorerError) as raised:
                    scoring.phonemes_for(b"x", "audio/webm")
                self.assertIn(message, str(raised.exception))
                self.assertNotIn("scorer-secret", str(raised.exception))

    @override_settings(SCORER_URL="")
    def test_not_configured_without_a_url(self, post):
        self.assertFalse(scoring.is_configured())


class StaleRecordingCleanupTests(OfflineTestCase):
    def test_deletes_only_old_recordings(self):
        old = Recording.objects.create(audio=b"old", content_type="audio/webm", size=3)
        Recording.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        fresh = Recording.objects.create(audio=b"new", content_type="audio/webm", size=3)

        with self.assertLogs("core.tasks", level="WARNING") as logs:
            self.assertEqual(delete_stale_recordings(), 1)
        self.assertEqual(list(Recording.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertIn("Deleted 1 stale recording(s)", logs.output[0])


class RecordingFeatureFlagTests(OfflineTestCase):
    def turn_off(self):
        FeatureFlag.objects.filter(name="recording").update(enabled=False)

    def test_recording_flag_is_on_after_migrations(self):
        self.assertTrue(FeatureFlag.is_enabled("recording"))

    def test_home_page_hides_recorder_when_off(self):
        self.turn_off()
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Recording is unavailable right now")
        self.assertNotContains(res, "Start session")
        self.assertNotContains(res, "record.js")

    def test_existing_recording_can_still_be_played_and_deleted_when_off(self):
        recording = make_recording()
        self.turn_off()
        self.assertEqual(self.client.get(f"/api/recordings/{recording.pk}/audio/").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/recordings/{recording.pk}/").status_code, 204)

    def test_missing_flag_counts_as_off(self):
        FeatureFlag.objects.all().delete()
        self.assertFalse(FeatureFlag.is_enabled("recording"))

    def test_admin_can_turn_recording_off_and_it_is_logged(self):
        admin_user = get_user_model().objects.create_superuser("boss", "boss@example.com", "pw-123456")
        self.client.force_login(admin_user)
        flag = FeatureFlag.objects.get(name="recording")

        self.assertContains(self.client.get("/admin/core/featureflag/"), "recording")
        with self.assertLogs("core.admin", level="WARNING") as logs:
            res = self.client.post(
                f"/admin/core/featureflag/{flag.pk}/change/",
                {"name": "recording", "description": flag.description},
            )
        self.assertEqual(res.status_code, 302)
        self.assertFalse(FeatureFlag.is_enabled("recording"))
        self.assertEqual(logs.output, ["WARNING:core.admin:Feature flag recording turned off by boss"])
        self.assertNotContains(self.client.get("/"), "Start session")


class GoldenSetTests(TestCase):
    """Attempts are promoted into eval clips, with audio, only while eval mode (keep_audio) kept the audio."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media, SCORER_URL="", TRANSCRIBER_URL="")
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(get_user_model().objects.create_superuser("bench", "b@example.com", "pw-123456"))

    def keep_audio(self, on=True):
        FeatureFlag.objects.filter(name="keep_audio").update(enabled=on)

    def record(self, content_type="audio/wav"):
        recording = make_recording(content_type=content_type)
        attempt = Attempt.objects.create(recording=recording, transcript="hola")
        return {"id": str(recording.pk)}, attempt

    def promote(self, *attempts):
        return self.client.post(
            "/admin/core/attempt/",
            {"action": "promote_to_eval_clip", "_selected_action": [a.pk for a in attempts]},
            follow=True,
        )

    def test_keep_audio_is_off_by_default_and_playback_deletes_as_before(self):
        self.assertFalse(FeatureFlag.is_enabled("keep_audio"))
        data, _ = self.record()
        self.assertEqual(self.client.delete(f"/api/recordings/{data['id']}/").status_code, 204)
        self.assertFalse(Recording.objects.exists())

    def test_eval_mode_keeps_the_audio_and_tells_the_page(self):
        self.keep_audio()
        data, _ = self.record()
        res = self.client.delete(f"/api/recordings/{data['id']}/")
        self.assertEqual((res.status_code, res.json()), (200, {"kept": True}))
        self.assertTrue(Recording.objects.filter(pk=data["id"]).exists())

    def test_eval_mode_pauses_the_stale_cleanup(self):
        self.keep_audio()
        data, _ = self.record()
        Recording.objects.update(created_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(delete_stale_recordings(), 0)
        self.assertTrue(Recording.objects.exists())

    def test_promote_copies_the_audio_links_the_attempt_and_leaves_the_rest_blank(self):
        self.keep_audio()
        _, attempt = self.record(content_type="audio/mp4")

        res = self.promote(attempt)

        self.assertContains(res, "Promoted 1 attempt(s)")
        clip = EvalClip.objects.get()
        self.assertEqual(clip.source_attempt, attempt)
        self.assertEqual((clip.intended_text, clip.human_label), ("", ""), "left for a human, never from the transcript")
        self.assertTrue(clip.audio.name.endswith(".mp4"))
        with clip.audio.open("rb") as f:
            self.assertEqual(f.read(), b"\x01" * 1024)

    def test_attempts_whose_audio_is_gone_or_already_promoted_are_skipped(self):
        data, gone = self.record()
        self.client.delete(f"/api/recordings/{data['id']}/")
        self.keep_audio()
        _, kept = self.record()
        self.promote(kept)

        res = self.promote(gone, kept)

        self.assertContains(res, "Promoted 0 attempt(s)")
        self.assertContains(res, "1 attempt(s) skipped: their audio was already deleted")
        self.assertContains(res, "1 attempt(s) skipped: already promoted")
        self.assertEqual(EvalClip.objects.count(), 1)

    def test_a_promoted_clip_can_then_be_labeled_in_the_admin(self):
        self.keep_audio()
        _, attempt = self.record()
        self.promote(attempt)
        clip = EvalClip.objects.get()

        res = self.client.post(
            f"/admin/core/evalclip/{clip.pk}/change/",
            {"intended_text": "Hola amigo", "language": "es", "human_label": "good", "notes": "clear"},
        )

        self.assertEqual(res.status_code, 302, res.content[:400])
        clip.refresh_from_db()
        self.assertEqual((clip.intended_text, clip.human_label, clip.notes), ("Hola amigo", "good", "clear"))
        self.assertContains(self.client.get("/admin/core/evalclip/?human_label__exact=good"), "Hola amigo")
