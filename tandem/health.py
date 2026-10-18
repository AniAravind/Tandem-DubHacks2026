"""Dependency probes expose status only and use dedicated, bounded connections."""
import uuid
from django.conf import settings
from django.core.cache import cache
from django.db import connections
import redis

WORKER_KEY='ops:worker:alive'
BEAT_KEY='tandem:beat:leader'


def redis_client():
    options={'socket_connect_timeout':2,'socket_timeout':2,'retry_on_timeout':False}
    if settings.REDIS_URL.startswith('rediss://'):
        options['ssl_cert_reqs']=None  # same hosted Redis certificate policy as the broker
    return redis.Redis.from_url(settings.REDIS_URL,**options)


def database_status():
    probe=connections['default'].copy(alias='readiness')
    if probe.vendor=='postgresql':
        probe.settings_dict['OPTIONS']={**probe.settings_dict.get('OPTIONS',{}),
            'connect_timeout':2,'options':'-c statement_timeout=1500 -c lock_timeout=1000'}
    try:
        with probe.cursor() as cursor:
            cursor.execute('SELECT 1')
            return 'ok' if cursor.fetchone()[0]==1 else 'error'
    except Exception:
        return 'error'
    finally:
        probe.close()


def dependency_status():
    from core.checks import deployment_problems
    result={'db':database_status(),'redis':'skipped','cache':'skipped','worker':'skipped','beat':'skipped',
            'configuration':'error' if deployment_problems() else 'ok'}
    required=settings.TANDEM_DEPLOYMENT or not settings.CELERY_TASK_ALWAYS_EAGER
    if not required:
        return result
    client=None
    try:
        client=redis_client()
        result['redis']='ok' if client.ping() else 'error'
        if settings.TANDEM_REQUIRE_WORKER_HEARTBEAT:
            result['beat']='ok' if client.ttl(BEAT_KEY)>0 else 'missing'
    except Exception:
        result['redis']='error'
        if settings.TANDEM_REQUIRE_WORKER_HEARTBEAT:
            result['beat']='unknown'
    finally:
        if client is not None:
            client.close()
    key='ops:probe:'+uuid.uuid4().hex
    try:
        cache.set(key,'ready',timeout=5)
        result['cache']='ok' if cache.get(key)=='ready' else 'error'
        if settings.TANDEM_REQUIRE_WORKER_HEARTBEAT:
            result['worker']='ok' if cache.get(WORKER_KEY) else 'missing'
    except Exception:
        result['cache']='error'
        if settings.TANDEM_REQUIRE_WORKER_HEARTBEAT:
            result['worker']='unknown'
    finally:
        try:
            cache.delete(key)
        except Exception:
            pass
    return result
