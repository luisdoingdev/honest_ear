from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from django.views.i18n import JavaScriptCatalog

from core import views

urlpatterns = [
    path("", views.record, name="record"),
    path("record/", RedirectView.as_view(pattern_name="record")),
    path("api/recordings/", views.create_recording, name="recording-create"),
    path("api/recordings/<uuid:recording_id>/", views.delete_recording, name="recording-delete"),
    path("api/recordings/<uuid:recording_id>/audio/", views.recording_audio, name="recording-audio"),
    path("jsi18n/", JavaScriptCatalog.as_view(packages=["core"]), name="javascript-catalog"),
    path("admin/", admin.site.urls),
    path("health/", views.health),
]
