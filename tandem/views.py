import os
from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse


def health(request):
    out = {"status": "ok", "db": "ok", "redis": "skipped",
           "gateway": settings.PAYMENTS_GATEWAY, "simulated": True,
           "release": os.getenv("HEROKU_RELEASE_VERSION", "local")}
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
    except Exception as e:
        out.update(status="degraded", db=f"error: {e.__class__.__name__}")
    try:
        import redis
        kw = {"ssl_cert_reqs": None} if settings.REDIS_URL.startswith("rediss://") else {}
        redis.from_url(settings.REDIS_URL, socket_timeout=1, **kw).ping()
        out["redis"] = "ok"
    except Exception as e:
        out["redis"] = f"error: {e.__class__.__name__}"
        if not settings.CELERY_TASK_ALWAYS_EAGER:
            out["status"] = "degraded"
    return JsonResponse(out, status=200 if out["status"] == "ok" else 503)


def spa_index(request, *args, **kwargs):
    index = settings.FRONTEND_DIST / "index.html"
    if not index.exists():
        return HttpResponse("Frontend not built. Run: npm --prefix frontend run build", status=503)
    return HttpResponse(index.read_text(), content_type="text/html")
