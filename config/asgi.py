import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Load Django before anything that imports models (the consumer does).
django_application = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.urls import path  # noqa: E402

from core.consumers import RecordingConsumer  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_application,
        # Only pages served from ALLOWED_HOSTS may open a recording socket.
        "websocket": AllowedHostsOriginValidator(
            URLRouter([path("ws/record/", RecordingConsumer.as_asgi())])
        ),
    }
)
