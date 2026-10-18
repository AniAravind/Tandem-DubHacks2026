"""Safe HTTP/task tracing. Bodies, cookies, headers and task arguments are never logged."""
import contextvars
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

request_id=contextvars.ContextVar('request_id',default='')
task_id=contextvars.ContextVar('task_id',default='')
log=logging.getLogger('tandem.ops')


def valid_id(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value) else uuid.uuid4().hex


def redact(value):
    value=str(value)
    for key, secret in os.environ.items():
        if secret and len(secret)>=8 and (re.search(r'KEY|SECRET|TOKEN|PASSWORD|PHONE',key,re.I) or key in ('DATABASE_URL','REDIS_URL')):
            value=value.replace(secret,'[redacted]')
    value=re.sub(r'https?://[^\s<>]+','[url redacted]',value)
    value=re.sub(r'/(?:api/v1/consent|c)/[^\s?]+','/[private link]',value)
    value=re.sub(r'(?i)(owner|token|password|secret|assertion|otp|cap_cents|budget_cap_cents)([\"\']?\s*[:=]\s*[\"\']?)[^\s,}\"\']+',r'\1\2[redacted]',value)
    value=re.sub(r'(?i)Bearer\s+[^\s]+','Bearer [redacted]',value)
    value=re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b','[email]',value)
    value=re.sub(r'\+\d[\d ()-]{7,}\d','[phone]',value)
    return re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)','[card-like digits]',value)


class SafeJSONFormatter(logging.Formatter):
    def format(self,record):
        message = 'celery.runtime_event' if record.name in ('celery.app.trace','celery.worker.request') else redact(record.getMessage())
        entry={'at':datetime.now(timezone.utc).isoformat(),'level':record.levelname,
               'logger':record.name,'message':message,
               'request_id':request_id.get() or getattr(getattr(record,'request',None),'request_id',''),'task_id':task_id.get()}
        for key in ('event','method','route','status','duration_ms','task_name','state','error_type'):
            if hasattr(record,key):
                value=getattr(record,key)
                entry[key]=redact(value) if isinstance(value,str) else value
        if record.exc_info:
            entry['error_type']=record.exc_info[0].__name__
        return json.dumps(entry,ensure_ascii=False,default=str)


class RequestTracingMiddleware:
    def __init__(self,get_response):
        self.get_response=get_response

    def __call__(self,request):
        identity=valid_id(request.headers.get('X-Request-ID'))
        request.request_id=identity
        token=request_id.set(identity)
        start=time.monotonic()
        try:
            response=self.get_response(request)
            response['X-Request-ID']=identity
            match=getattr(request,'resolver_match',None)
            log.info('http.request',extra={'event':'http.request','method':request.method,
                'route':getattr(match,'route',None) or 'unmatched','status':response.status_code,
                'duration_ms':round((time.monotonic()-start)*1000)})
            return response
        finally:
            request_id.reset(token)


def publish_trace(sender=None,headers=None,**kwargs):
    if headers is not None:
        headers['x-tandem-request-id']=valid_id(request_id.get() or headers.get('x-tandem-request-id'))


def start_task(sender=None,task_id=None,task=None,**kwargs):
    headers=getattr(task.request,'headers',None) or {}
    task.request.ops_tokens=(request_id.set(valid_id(headers.get('x-tandem-request-id'))),
                             globals()['task_id'].set(valid_id(task_id)))
    log.info('task.started',extra={'event':'task.started','task_name':task.name})


def finish_task(sender=None,task=None,state=None,**kwargs):
    try:
        log.info('task.finished',extra={'event':'task.finished','task_name':task.name,'state':state})
    finally:
        tokens=getattr(task.request,'ops_tokens',None)
        if tokens:
            request_id.reset(tokens[0]);task_id.reset(tokens[1])


def task_failed(sender=None,exception=None,**kwargs):
    log.error('task.failed',extra={'event':'task.failed','task_name':getattr(sender,'name','unknown'),
                                 'error_type':type(exception).__name__})


def heartbeat(**kwargs):
    from django.conf import settings
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return
    from django.core.cache import cache
    from tandem.health import WORKER_KEY
    try:
        cache.set(WORKER_KEY,True,timeout=30)
    except Exception as error:
        log.warning('worker.heartbeat_unavailable',extra={'error_type':type(error).__name__})


def format_worker_logs(logger=None,**kwargs):
    for handler in (logger or logging.getLogger()).handlers:
        handler.setFormatter(SafeJSONFormatter())


def register_signals():
    from celery import signals
    for signal,handler in [(signals.before_task_publish,publish_trace),(signals.task_prerun,start_task),
        (signals.task_postrun,finish_task),(signals.task_failure,task_failed),
        (signals.worker_ready,heartbeat),(signals.heartbeat_sent,heartbeat),
        (signals.after_setup_logger,format_worker_logs),(signals.after_setup_task_logger,format_worker_logs)]:
        signal.connect(handler,weak=False,dispatch_uid='tandem.ops.'+handler.__name__)
