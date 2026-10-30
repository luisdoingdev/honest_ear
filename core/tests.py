from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from .models import FeatureFlag, Recording
from .tasks import delete_stale_recordings
from .views import MAX_RECORDING_BYTES


def audio_file(size=1024, content_type="audio/webm"):
    return SimpleUploadedFile("recording", b"\x01" * size, content_type=content_type)


class RecordingApiTests(TestCase):
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


class LanguageTests(TestCase):
    def test_spanish_browsers_get_spanish(self):
        # es-CO (Colombia: Bogotá, Medellín) is the main audience.
        for header in ["es-CO,es;q=0.9", "es-CO", "es-419", "es-MX", "es"]:
            with self.subTest(header=header):
                res = self.client.get("/", headers={"accept-language": header})
                self.assertContains(res, '<html lang="es">')
                self.assertContains(res, ">Empezar</button>")
                self.assertEqual(res["Content-Language"], "es")

    def test_other_browsers_get_english(self):
        for header in ["en-US", "pt-BR,pt;q=0.9", "fr-FR"]:
            with self.subTest(header=header):
                res = self.client.get("/", headers={"accept-language": header})
                self.assertContains(res, '<html lang="en">')
                self.assertContains(res, ">Start session</button>")

    def test_switcher_names_each_language_in_itself(self):
        for header in ["es-MX", "en-US"]:
            with self.subTest(header=header):
                res = self.client.get("/", headers={"accept-language": header})
                self.assertContains(res, ">English</button>")
                self.assertContains(res, ">Español</button>")

    def test_varies_by_language_so_caches_keep_them_apart(self):
        res = self.client.get("/", headers={"accept-language": "es-MX"})
        self.assertIn("Accept-Language", res["Vary"])

    def test_switcher_choice_overrides_the_browser(self):
        res = self.client.post("/i18n/setlang/", {"language": "es", "next": "/"})
        self.assertRedirects(res, "/", fetch_redirect_response=False)
        res = self.client.get("/", headers={"accept-language": "en-US"})
        self.assertContains(res, ">Empezar</button>")

    def test_api_errors_are_translated(self):
        res = self.client.post(
            "/api/recordings/",
            {"audio": audio_file(content_type="text/plain")},
            headers={"accept-language": "es-MX"},
        )
        self.assertEqual(res.json()["error"], "Tipo de audio no compatible: text/plain")

    def test_javascript_messages_are_translated(self):
        res = self.client.get("/jsi18n/", headers={"accept-language": "es-MX"})
        self.assertContains(res, "Grabando")


class StaleRecordingCleanupTests(TestCase):
    def test_deletes_only_old_recordings(self):
        old = Recording.objects.create(audio=b"old", content_type="audio/webm", size=3)
        Recording.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        fresh = Recording.objects.create(audio=b"new", content_type="audio/webm", size=3)

        with self.assertLogs("core.tasks", level="WARNING") as logs:
            self.assertEqual(delete_stale_recordings(), 1)
        self.assertEqual(list(Recording.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertIn("Deleted 1 stale recording(s)", logs.output[0])


class RecordingFeatureFlagTests(TestCase):
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
