import os
from django.conf import settings
from django.http import HttpResponse, JsonResponse


def live(request):
    return JsonResponse({'status':'ok','release':os.getenv('HEROKU_RELEASE_VERSION','local')})


def health(request):
    from tandem.health import dependency_status
    checks=dependency_status()
    healthy=all(value in ('ok','skipped') for value in checks.values())
    out={'status':'ok' if healthy else 'degraded',**checks,'gateway':settings.PAYMENTS_GATEWAY,
         'simulated':settings.PAYMENTS_GATEWAY=='simulated','release':os.getenv('HEROKU_RELEASE_VERSION','local')}
    response=JsonResponse(out,status=200 if healthy else 503)
    response['Cache-Control']='no-store'
    return response


def spa_index(request, *args, **kwargs):
    index = settings.FRONTEND_DIST / "index.html"
    if not index.exists():
        return HttpResponse("Frontend not built. Run: npm --prefix frontend run build", status=503)
    return HttpResponse(index.read_text(), content_type="text/html")
