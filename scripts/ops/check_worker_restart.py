"""Local/CI integration: real worker tracing, duplicate beat protection and restart.

Requires OPS_ISOLATED_TESTS=1 and loopback PostgreSQL/Redis. Never runs on a dyno.
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def main():
    if os.getenv('OPS_ISOLATED_TESTS')!='1' or os.getenv('DYNO'):
        raise SystemExit('Use only with OPS_ISOLATED_TESTS=1 on an isolated local/CI stack.')
    for name in ('DATABASE_URL','REDIS_URL'):
        if urlsplit(os.getenv(name,'')).hostname not in ('localhost','127.0.0.1','::1'):
            raise SystemExit('Operational restart tests require loopback data services.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE','tandem.settings')
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    import django
    django.setup()
    from django.conf import settings
    if settings.CELERY_TASK_ALWAYS_EAGER:
        raise SystemExit('This test requires CELERY_EAGER=0.')
    from tandem.celery import app
    from tandem.health import redis_client,BEAT_KEY,dependency_status
    from tandem.observability import request_id
    from core.tasks import ops_probe
    client=redis_client()
    if client.exists(BEAT_KEY):
        raise SystemExit('Another scheduler is active. Use a separate isolated Redis database.')
    subprocess.run([sys.executable,'manage.py','migrate','--noinput'],check=True,stdout=subprocess.DEVNULL)
    queue='ops-test-'+uuid.uuid4().hex
    processes=[]
    def wait_for(predicate,label):
        deadline=time.monotonic()+55
        while time.monotonic()<deadline:
            if predicate():
                return
            time.sleep(.25)
        raise RuntimeError('Timed out waiting for '+label)
    def stop(process):
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
    try:
        with tempfile.TemporaryDirectory(prefix='tandem-ops-') as directory:
            def start(index):
                log=Path(directory)/f'worker-{index}.log'
                output=log.open('w')
                process=subprocess.Popen([sys.executable,'-m','celery','-A','tandem','worker','--beat',
                    '--pool=solo','--concurrency=1','--loglevel=WARNING','--queues='+queue,
                    '--hostname=ops-'+str(index)+'@test','--schedule='+str(Path(directory)/f'beat-{index}')],
                    stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                output.close();processes.append(process)
                return process
            first=start(1)
            wait_for(lambda:client.ttl(BEAT_KEY)>0,'first scheduler lease')
            def probe():
                trace=uuid.uuid4().hex
                token=request_id.set(trace)
                try:
                    result=ops_probe.apply_async(queue=queue,expires=20)
                finally:
                    request_id.reset(token)
                payload=result.get(timeout=20)
                assert payload=={'request_id':trace,'task_id':result.id},'Task trace did not cross the real broker'
                result.forget()
            probe()
            owner=client.get(BEAT_KEY)
            second=start(2)
            time.sleep(3)
            assert client.get(BEAT_KEY)==owner,'Second scheduler replaced the first leader'
            # Both worker nodes may stay alive; only one beat may schedule.
            assert dependency_status()['worker']=='ok','Worker heartbeat missing'
            stop(first)
            wait_for(lambda:client.ttl(BEAT_KEY)<0,'released first scheduler lease')
            assert dependency_status()['beat']!='ok','Readiness missed a scheduler outage'
            stop(second)
            third=start(3)
            wait_for(lambda:client.ttl(BEAT_KEY)>0,'restarted scheduler lease')
            probe()
            assert dependency_status()['beat']=='ok'
            stop(third)
        print('WORKER_TRACE_SINGLETON_OUTAGE_AND_RESTART_OK')
    finally:
        for process in processes:
            stop(process)
        client.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
