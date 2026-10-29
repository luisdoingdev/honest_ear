from django.contrib import admin
from django.urls import path

from core import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("record/", views.record, name="record"),
    path("api/recordings/", views.create_recording, name="recording-create"),
    path("api/recordings/<uuid:recording_id>/", views.delete_recording, name="recording-delete"),
    path("api/recordings/<uuid:recording_id>/audio/", views.recording_audio, name="recording-audio"),
    path("admin/", admin.site.urls),
    path("health/", views.health),
]
