"""Deployment-only checks; diagnostics name settings, never their values."""
import base64
import os
import re
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.checks import Error, register


def deployment_problems():
    if not settings.TANDEM_DEPLOYMENT:
        return []
    problems=[]
    def need(condition, code, message):
        if not condition:
            problems.append(Error(message, id='tandem.'+code))
    need(not settings.DEBUG, 'E001', 'DEBUG must be disabled in deployment.')
    from django.core.exceptions import ImproperlyConfigured
    try:
        secret=settings.SECRET_KEY
    except ImproperlyConfigured:
        secret=''
    need(len(secret)>=50 and not secret.startswith(('change-', 'test-', 'example')), 'E002', 'Set a strong DJANGO_SECRET_KEY (at least 50 characters).')
    need(settings.PAYMENTS_GATEWAY in ('simulated','visa_sandbox'), 'E003', 'PAYMENTS_GATEWAY must be simulated or visa_sandbox (never live).')
    if settings.PAYMENTS_GATEWAY == 'visa_sandbox':
        need(all(os.getenv(k) for k in ('VISA_MERCHANT_ID','VISA_KEY_ID','VISA_SHARED_SECRET')), 'E021', 'visa_sandbox needs VISA_MERCHANT_ID, VISA_KEY_ID and VISA_SHARED_SECRET.')
        need(os.getenv('VISA_HOST','apitest.cybersource.com') == 'apitest.cybersource.com', 'E022', 'VISA_HOST must be the sandbox host apitest.cybersource.com.')
    need(not settings.CELERY_TASK_ALWAYS_EAGER, 'E004', 'Deployment requires CELERY_EAGER=0 and a worker.')
    db=settings.DATABASES['default']
    need(db['ENGINE'].endswith('postgresql') and db.get('HOST') not in ('', 'localhost','127.0.0.1','::1'), 'E005', 'Deployment requires hosted PostgreSQL.')
    need(db.get('OPTIONS',{}).get('sslmode') in ('require','verify-ca','verify-full'), 'E006', 'Deployment PostgreSQL must require TLS.')
    try:
        redis=urlsplit(settings.REDIS_URL)
        redis_ok=redis.scheme in ('redis','rediss') and redis.hostname not in (None,'localhost','127.0.0.1','::1')
    except ValueError:
        redis_ok=False
    need(redis_ok, 'E007', 'REDIS_URL must point to hosted Redis, not localhost.')
    need(settings.CACHES['default']['BACKEND']=='django.core.cache.backends.redis.RedisCache', 'E008', 'Deployment requires the shared Redis cache.')
    def origin(value):
        try:
            url=urlsplit(value)
            return url if url.scheme=='https' and url.hostname and not url.username and not url.password and not url.query and not url.fragment else None
        except ValueError:
            return None
    public=origin(settings.PUBLIC_BASE_URL)
    need(public is not None and public.path in ('','/'), 'E009', 'PUBLIC_BASE_URL must be an HTTPS origin without credentials, paths, or query parameters.')
    if public:
        host=public.hostname
        allowed=any(entry==host or (entry.startswith('.') and (host==entry[1:] or host.endswith(entry))) for entry in settings.ALLOWED_HOSTS)
        need(allowed and '*' not in settings.ALLOWED_HOSTS, 'E010', 'ALLOWED_HOSTS must explicitly cover the public hostname; wildcard hosts are not deployment-safe.')
        need(f'https://{public.netloc}' in settings.CSRF_TRUSTED_ORIGINS, 'E011', 'CSRF_TRUSTED_ORIGINS must include the public HTTPS origin.')
    need(settings.MERCHANT_MODE in ('direct','http'), 'E012', 'MERCHANT_MODE must be direct or http.')
    merchant=origin(settings.MERCHANT_BASE_URL)
    if settings.MERCHANT_MODE=='http':
        need(merchant is not None, 'E013', 'HTTP merchant mode requires an HTTPS MERCHANT_BASE_URL.')
        if public and merchant and merchant.netloc==public.netloc:
            need(settings.TANDEM_WEB_CONCURRENCY>=2, 'E014', 'Self-HTTP merchant checkout requires at least two web workers.')
    for key, validator, code in [
        ('AGENT_SIGNING_KEY_B64', lambda value: len(base64.b64decode(value,validate=True))==32, 'E015'),
        ('FIELD_ENCRYPTION_KEY', lambda value: bool(Fernet(value.encode())), 'E016')]:
        try:
            valid=validator(os.getenv(key,''))
        except (ValueError,TypeError):
            valid=False
        need(valid,code,f'{key} must be configured with a valid key before deployment.')
    need(not settings.DEMO_ADMIN_KEY or len(settings.DEMO_ADMIN_KEY)>=24, 'E017', 'Set a strong DEMO_ADMIN_KEY or leave demo controls disabled.')
    photon=('SPECTRUM_PROJECT_ID','SPECTRUM_PROJECT_SECRET','PHOTON_BRIDGE_SECRET')
    if any(os.getenv(key) for key in photon) or os.getenv('PHOTON_PUBLIC_REGISTRATION') == '1':
        need(all(os.getenv(key) for key in photon), 'E018', 'Configure all Photon bridge settings together, or disable the Photon process.')
        need(len(os.getenv('PHOTON_BRIDGE_SECRET',''))>=32, 'E019', 'PHOTON_BRIDGE_SECRET must contain at least 32 characters.')
        if os.getenv('PHOTON_TEST_PHONE'):
            need(bool(re.fullmatch(r'\+[1-9]\d{7,14}',os.getenv('PHOTON_TEST_PHONE',''))), 'E020', 'PHOTON_TEST_PHONE must use E.164 format.')
    return problems


@register(deploy=True)
def check_deployment(app_configs, **kwargs):
    return deployment_problems()
