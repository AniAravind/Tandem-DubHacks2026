"""Only one embedded beat may own the Redis lease; loss stops scheduling."""
import logging
from celery.beat import PersistentScheduler
from django.conf import settings
from tandem.health import BEAT_KEY, redis_client

log=logging.getLogger('tandem.ops')


class SingletonScheduler(PersistentScheduler):
    def setup_schedule(self):
        self._lease=None
        self._redis=None
        if not settings.CELERY_TASK_ALWAYS_EAGER:
            try:
                self._redis=redis_client()
                self._lease=self._redis.lock(BEAT_KEY,timeout=45,blocking=False,thread_local=False)
                if not self._lease.acquire(blocking=False):
                    raise RuntimeError('beat lease already held')
            except Exception as error:
                log.error('beat.lease_unavailable',extra={'error_type':type(error).__name__})
                self._release()
                raise SystemExit('Beat cannot obtain the shared scheduler lease.') from None
        try:
            super().setup_schedule()
        except BaseException:
            self._release()
            raise

    def tick(self,*args,**kwargs):
        if self._lease is not None:
            try:
                self._lease.extend(45,replace_ttl=True)
            except Exception as error:
                log.error('beat.lease_lost',extra={'error_type':type(error).__name__})
                raise SystemExit('Beat lost its scheduler lease; stopping.') from None
        return min(super().tick(*args,**kwargs),10.0)

    def _release(self):
        if self._lease is not None:
            try:
                self._lease.release()
            except Exception:
                pass  # a lease owned by another process must never be deleted
            self._lease=None
        if self._redis is not None:
            self._redis.close()
            self._redis=None

    def close(self):
        try:
            if getattr(self, "_store", None) is not None:
                super().close()
        finally:
            self._release()
