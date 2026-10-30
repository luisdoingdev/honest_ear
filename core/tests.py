from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import FeatureFlag, Recording
from .tasks import delete_stale_recordings
from .transcription import DEEPGRAM_URL
from .views import MAX_RECORDING_BYTES


# A real DEEPGRAM_API_KEY in .env must never make tests call (and bill) Deepgram.
@override_settings(DEEPGRAM_API_KEY="")
class OfflineTestCase(TestCase):
    pass


def audio_file(size=1024, content_type="audio/webm"):
    return SimpleUploadedFile("recording", b"\x01" * size, content_type=content_type)


class RecordingApiTests(OfflineTestCase):
    def upload(self, **kwargs):
        return self.client.post("/api/recordings/", {"audio": audio_file(**kwargs)})

    def test_upload_fetch_then_delete(self):
        res = self.upload()
        self.assertEqual(res.status_code, 201)
        data = res.json()

        audio = self.client.get(data["audio_url"])
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.content, b"\x01" * 1024)
        self.assertEqual(audio["Content-Type"], "audio/webm")
        self.assertEqual(audio["Cache-Control"], "no-store")

        self.assertEqual(self.client.delete(f"/api/recordings/{data['id']}/").status_code, 204)
        self.assertFalse(Recording.objects.exists())

    def test_logs_each_step_of_a_recording(self):
        with self.assertLogs("core.views", level="INFO") as logs:
            data = self.upload().json()
            self.client.get(data["audio_url"])
            self.client.delete(f"/api/recordings/{data['id']}/")
            self.client.delete(f"/api/recordings/{data['id']}/")

        rid = data["id"]
        self.assertEqual(
            logs.output,
            [
                f"INFO:core.views:Recording {rid} created (1024 bytes, audio/webm)",
                f"INFO:core.views:Recording {rid} served (1024 bytes)",
                f"INFO:core.views:Recording {rid} deleted after playback",
                f"INFO:core.views:Recording {rid} delete requested but already gone",
            ],
        )

    def test_logs_rejections_as_warnings(self):
        with self.assertLogs("core.views", level="WARNING") as logs:
            self.upload(content_type="text/plain")
        self.assertEqual(
            logs.output, ["WARNING:core.views:Recording rejected: unsupported type text/plain (1024 bytes)"]
        )

    def test_deleted_recording_is_gone_and_delete_is_repeatable(self):
        data = self.upload().json()
        self.client.delete(f"/api/recordings/{data['id']}/")

        self.assertEqual(self.client.get(data["audio_url"]).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/recordings/{data['id']}/").status_code, 204)

    def test_accepts_safari_mp4(self):
        res = self.upload(content_type="audio/mp4")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Recording.objects.get().content_type, "audio/mp4")

    def test_rejects_missing_audio(self):
        self.assertEqual(self.client.post("/api/recordings/").status_code, 400)

    def test_rejects_wrong_type(self):
        self.assertEqual(self.upload(content_type="text/plain").status_code, 415)
        self.assertFalse(Recording.objects.exists())

    def test_rejects_oversized_upload(self):
        self.assertEqual(self.upload(size=MAX_RECORDING_BYTES + 1).status_code, 413)
        self.assertFalse(Recording.objects.exists())

    def test_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        res = client.post("/api/recordings/", {"audio": audio_file()})
        self.assertEqual(res.status_code, 403)

    def test_home_page_is_the_recorder(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="csrf-token"')
        self.assertContains(res, "Start session")

    def test_old_record_url_redirects_home(self):
        self.assertRedirects(self.client.get("/record/"), "/", fetch_redirect_response=False)


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

    def test_api_errors_are_translated(self):
        res = self.client.post(
            "/api/recordings/",
            {"audio": audio_file(content_type="text/plain")},
            headers={"accept-language": "es-MX"},
        )
        self.assertEqual(res.json()["error"], "Tipo de audio no compatible: text/plain")

    def test_javascript_messages_are_translated(self):
        res = self.client.get("/jsi18n/", headers={"accept-language": "es-MX"})
        self.assertContains(res, "Escuchando")


def deepgram_reply(transcript="", status=200, body=None):
    body = body or {"results": {"channels": [{"alternatives": [{"transcript": transcript}]}]}}
    return httpx.Response(status, json=body, request=httpx.Request("POST", DEEPGRAM_URL))


@override_settings(DEEPGRAM_API_KEY="test-key")
@mock.patch("core.transcription.httpx.post")
class TranscriptionTests(TestCase):
    def upload(self, language="es-CO"):
        return self.client.post(
            "/api/recordings/", {"audio": audio_file()}, headers={"accept-language": language}
        )

    def test_upload_returns_the_transcript(self, post):
        post.return_value = deepgram_reply("Hola, ¿cómo estás?")
        res = self.upload()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["transcript"], "Hola, ¿cómo estás?")

    def test_every_browser_is_transcribed_in_multi_and_opted_out_of_training(self, post):
        post.return_value = deepgram_reply("hi")
        self.upload(language="es-CO")
        self.upload(language="en-US")

        for call in post.call_args_list:
            self.assertEqual(call.args, (DEEPGRAM_URL,))
            self.assertEqual(
                call.kwargs["params"],
                {"model": "nova-3", "language": "multi", "smart_format": "true", "mip_opt_out": "true"},
            )
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Token test-key")
            self.assertEqual(call.kwargs["headers"]["Content-Type"], "audio/webm")
            self.assertEqual(call.kwargs["content"], b"\x01" * 1024)

    def test_deepgrams_reason_for_rejecting_a_request_is_logged(self, post):
        post.return_value = deepgram_reply(status=400, body={"err_msg": "Bad Request: bad option"})
        with self.assertLogs("core.views", level="WARNING") as logs:
            self.assertIsNone(self.upload().json()["transcript"])
        self.assertIn("Deepgram returned HTTP 400: Bad Request: bad option", logs.output[0])

    def test_silence_returns_an_empty_transcript(self, post):
        post.return_value = deepgram_reply("")
        self.assertEqual(self.upload().json()["transcript"], "")

    def test_transcript_text_is_never_logged(self, post):
        post.return_value = deepgram_reply("my private words")
        with self.assertLogs("core.views", level="INFO") as logs:
            rid = self.upload().json()["id"]
        self.assertIn(f"INFO:core.views:Recording {rid} transcribed (multi, 16 characters)", logs.output)
        self.assertNotIn("private", "\n".join(logs.output))

    def test_deepgram_down_still_keeps_the_recording(self, post):
        post.side_effect = httpx.ConnectError("boom")
        with self.assertLogs("core.views", level="WARNING") as logs:
            res = self.upload()
        data = res.json()
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(data["transcript"])
        self.assertEqual(data["transcript_error"], "No se pudo transcribir esta grabación.")
        self.assertEqual(self.client.get(data["audio_url"]).status_code, 200)
        self.assertIn("Deepgram request failed: ConnectError", logs.output[0])

    def test_deepgram_rejecting_the_key_is_logged_with_its_status(self, post):
        post.return_value = deepgram_reply(status=401)
        with self.assertLogs("core.views", level="WARNING") as logs:
            self.assertIsNone(self.upload().json()["transcript"])
        self.assertIn("Deepgram returned HTTP 401", logs.output[0])

    def test_flag_off_skips_deepgram(self, post):
        FeatureFlag.objects.filter(name="transcription").update(enabled=False)
        self.assertIsNone(self.upload().json()["transcript"])
        post.assert_not_called()

    @override_settings(DEEPGRAM_API_KEY="")
    def test_no_api_key_skips_deepgram(self, post):
        data = self.upload().json()
        self.assertIsNone(data["transcript"])
        self.assertNotIn("transcript_error", data)
        post.assert_not_called()


def grant_reply(status=200, body=None):
    body = body or {"access_token": "jwt-secret-value", "expires_in": 30}
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.deepgram.com/v1/auth/grant"))


@override_settings(
    DEEPGRAM_API_KEY="test-key",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
@mock.patch("core.transcription.httpx.post")
class LiveCaptionTests(TestCase):
    def setUp(self):
        cache.clear()

    def token(self, **headers):
        return self.client.post("/api/transcription/token/", headers=headers)

    def test_issues_a_short_lived_token_and_the_server_chosen_stream_url(self, post):
        post.return_value = grant_reply()
        data = self.token().json()

        self.assertEqual(data["protocols"], ["bearer", "jwt-secret-value"])
        url = urlparse(data["url"])
        # Flux (turn-based) on /v2: v1-only options such as interim_results would make it refuse the stream.
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "wss://api.deepgram.com/v2/listen")
        self.assertEqual(
            parse_qs(url.query),
            {
                "model": ["flux-general-multi"],
                "language_hint": ["es", "en"],
                "encoding": ["linear16"],
                "sample_rate": ["16000"],
                "mip_opt_out": ["true"],
            },
        )
        call = post.call_args
        self.assertEqual(call.args, ("https://api.deepgram.com/v1/auth/grant",))
        self.assertEqual(call.kwargs["headers"], {"Authorization": "Token test-key"})
        self.assertEqual(call.kwargs["json"], {"ttl_seconds": 30})

    def test_the_token_is_never_logged(self, post):
        post.return_value = grant_reply()
        with self.assertLogs("core.views", level="INFO") as logs:
            self.token()
        self.assertEqual(logs.output, ["INFO:core.views:Live captions token issued for 127.0.0.1"])

    def test_requires_a_csrf_token(self, post):
        res = Client(enforce_csrf_checks=True).post("/api/transcription/token/")
        self.assertEqual(res.status_code, 403)
        post.assert_not_called()

    def test_unavailable_when_either_flag_is_off(self, post):
        for flag in ["live_captions", "transcription"]:
            with self.subTest(flag=flag):
                FeatureFlag.objects.filter(name=flag).update(enabled=False)
                self.assertEqual(self.token().status_code, 503)
                self.assertContains(self.client.get("/"), 'data-live-captions="false"')
                FeatureFlag.objects.filter(name=flag).update(enabled=True)
        post.assert_not_called()

    @override_settings(DEEPGRAM_API_KEY="")
    def test_unavailable_without_an_api_key(self, post):
        self.assertEqual(self.token().status_code, 503)
        post.assert_not_called()

    def test_page_offers_live_captions_when_available(self, post):
        self.assertContains(self.client.get("/"), 'data-live-captions="true"')

    def test_deepgram_refusing_the_grant_is_logged_and_returns_503(self, post):
        post.return_value = grant_reply(status=403, body={"err_msg": "Insufficient permissions."})
        with self.assertLogs("core.views", level="WARNING") as logs:
            res = self.token()
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("protocols", res.json())
        self.assertIn("Deepgram returned HTTP 403: Insufficient permissions.", logs.output[0])

    def test_limits_each_ip_to_ten_tokens_a_minute(self, post):
        post.return_value = grant_reply()
        for _ in range(10):
            self.assertEqual(self.token().status_code, 200)
        self.assertEqual(self.token().status_code, 429)
        self.assertEqual(self.token(x_forwarded_for="198.51.100.7").status_code, 200, "other IPs unaffected")

    def test_rate_limit_uses_the_address_renders_proxy_appended(self, post):
        post.return_value = grant_reply()
        for _ in range(10):
            self.token(x_forwarded_for="6.6.6.6, 203.0.113.9")
        # Forging a different first entry doesn't escape the limit for 203.0.113.9.
        self.assertEqual(self.token(x_forwarded_for="1.2.3.4, 203.0.113.9").status_code, 429)

    def test_refuses_when_the_rate_limit_cache_is_down(self, post):
        with mock.patch("core.views.cache.incr", side_effect=ConnectionError("redis down")):
            with self.assertLogs("core.views", level="ERROR"):
                self.assertEqual(self.token().status_code, 503)
        post.assert_not_called()

    def test_upload_with_a_live_transcript_skips_deepgram(self, post):
        with self.assertLogs("core.views", level="INFO") as logs:
            res = self.client.post("/api/recordings/", {"audio": audio_file(), "live_transcript": "1"})
        self.assertEqual(res.status_code, 201)
        self.assertNotIn("transcript", res.json())
        self.assertIn("already transcribed live", logs.output[-1])
        post.assert_not_called()


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

    def test_upload_refused_when_off(self):
        self.turn_off()
        with self.assertLogs("core.views", level="WARNING") as logs:
            res = self.client.post("/api/recordings/", {"audio": audio_file()})
        self.assertEqual(res.status_code, 503)
        self.assertFalse(Recording.objects.exists())
        self.assertIn("feature flag is off", logs.output[0])

    def test_existing_recording_can_still_be_played_and_deleted_when_off(self):
        data = self.client.post("/api/recordings/", {"audio": audio_file()}).json()
        self.turn_off()
        self.assertEqual(self.client.get(data["audio_url"]).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/recordings/{data['id']}/").status_code, 204)

    def test_missing_flag_counts_as_off(self):
        FeatureFlag.objects.all().delete()
        self.assertFalse(FeatureFlag.is_enabled("recording"))
        self.assertEqual(self.client.post("/api/recordings/", {"audio": audio_file()}).status_code, 503)

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
