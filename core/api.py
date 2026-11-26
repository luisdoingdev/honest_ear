from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from ninja import NinjaAPI, Schema, Status
from redis.asyncio import Redis

from .tasks import ping

api = NinjaAPI(title="Honest Ear API")


class HealthOut(Schema):
    db: str
    redis: str


class TaskOut(Schema):
    task_id: str


@sync_to_async
def _check_db() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


async def _check_redis() -> None:
    client = Redis.from_url(settings.REDIS_URL)
    try:
        await client.ping()
    finally:
        await client.aclose()


@api.get("/health", response={200: HealthOut, 503: HealthOut})
async def health(request):
    result = {}
    for name, check in (("db", _check_db), ("redis", _check_redis)):
        try:
            await check()
            result[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
            result[name] = f"error: {exc.__class__.__name__}"
    status = 200 if all(v == "ok" for v in result.values()) else 503
    return Status(status, result)


@api.post("/ping-task", response=TaskOut)
async def ping_task(request):
    result = await sync_to_async(ping.delay)()
    return {"task_id": result.id}
