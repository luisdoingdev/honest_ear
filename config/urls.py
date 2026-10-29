from django.contrib import admin
from django.urls import path

from core.views import health, landing

urlpatterns = [
    path("", landing, name="landing"),
    path("admin/", admin.site.urls),
    path("health/", health),
]
