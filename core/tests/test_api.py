import pytest
from django.test import AsyncClient


@pytest.mark.django_db(transaction=True)
async def test_health_ok():
    response = await AsyncClient().get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"db": "ok", "redis": "ok"}


@pytest.mark.django_db
def test_index_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Honest Ear" in response.content
