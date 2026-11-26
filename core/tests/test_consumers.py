import pytest
from channels.testing import WebsocketCommunicator

from core.consumers import EchoConsumer


# Channels closes old DB connections around each consumer call, so allow DB access.
@pytest.mark.django_db(transaction=True)
async def test_echo_consumer():
    communicator = WebsocketCommunicator(EchoConsumer.as_asgi(), "/ws/echo/")
    connected, _ = await communicator.connect()
    assert connected
    await communicator.send_json_to({"text": "hi"})
    assert await communicator.receive_json_from() == {"echo": {"text": "hi"}}
    await communicator.disconnect()
