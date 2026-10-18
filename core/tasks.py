"""Metadata-only operational probe; no app data, messages, or money are touched."""
from celery import shared_task
from tandem.observability import request_id,task_id


@shared_task(name='core.ops_probe',soft_time_limit=5,time_limit=8)
def ops_probe():
    return {'request_id':request_id.get(),'task_id':task_id.get()}
