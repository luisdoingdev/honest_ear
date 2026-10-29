from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from .models import Recording
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

    def test_record_page_includes_csrf_token(self):
        res = self.client.get("/record/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'name="csrf-token"')


class StaleRecordingCleanupTests(TestCase):
    def test_deletes_only_old_recordings(self):
        old = Recording.objects.create(audio=b"old", content_type="audio/webm", size=3)
        Recording.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        fresh = Recording.objects.create(audio=b"new", content_type="audio/webm", size=3)

        self.assertEqual(delete_stale_recordings(), 1)
        self.assertEqual(list(Recording.objects.values_list("pk", flat=True)), [fresh.pk])
