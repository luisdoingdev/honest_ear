from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from django.views.i18n import JavaScriptCatalog

from core import views

urlpatterns = [
    path("", views.record, name="record"),
    path("record/", RedirectView.as_view(pattern_name="record")),
    path("api/recordings/", views.create_recording, name="recording-create"),
    path("api/transcription/token/", views.live_transcription_token, name="live-transcription-token"),
    path("api/recordings/<uuid:recording_id>/", views.delete_recording, name="recording-delete"),
    path("api/recordings/<uuid:recording_id>/audio/", views.recording_audio, name="recording-audio"),
    path("jsi18n/", JavaScriptCatalog.as_view(packages=["core"]), name="javascript-catalog"),
    path("admin/", admin.site.urls),
    path("health/", views.health),
]

# Lets the admin play uploaded eval clips locally; returns nothing when DEBUG is off.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
