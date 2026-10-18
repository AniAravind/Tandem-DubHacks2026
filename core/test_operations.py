import base64
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, RequestFactory, override_settings
from core.checks import deployment_problems
from tandem import observability as ops


@override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver'])
class HealthTests(TestCase):
    def test_liveness_does_not_need_database_redis_or_worker(self):
        with patch('tandem.health.dependency_status',side_effect=AssertionError('No dependencies for liveness')):
            self.assertEqual(self.client.get('/api/live').status_code,200)

    def test_readiness_and_legacy_health_fail_closed_without_details(self):
        degraded={'db':'error','redis':'error','cache':'error','worker':'missing','beat':'missing','configuration':'ok'}
        with patch('tandem.health.dependency_status',return_value=degraded):
            for path in ('/api/ready','/api/health'):
                response=self.client.get(path)
                self.assertEqual(response.status_code,503)
                self.assertEqual(response['Cache-Control'],'no-store')
                self.assertNotIn('password',response.content.decode())
                self.assertIn('X-Request-ID',response)

    @override_settings(TANDEM_DEPLOYMENT=False,CELERY_TASK_ALWAYS_EAGER=True)
    def test_eager_development_readiness_skips_external_dependencies(self):
        response=self.client.get('/api/ready')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['redis'],'skipped')

    @override_settings(TANDEM_DEPLOYMENT=False,CELERY_TASK_ALWAYS_EAGER=False,TANDEM_REQUIRE_WORKER_HEARTBEAT=True)
    def test_missing_worker_or_beat_makes_readiness_fail(self):
        redis=Mock();redis.ping.return_value=True;redis.ttl.return_value=-2
        with patch('tandem.health.redis_client',return_value=redis):
            response=self.client.get('/api/ready')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['worker'],'missing')
        self.assertEqual(response.json()['beat'],'missing')
        redis.close.assert_called_once()


class DeploymentCheckTests(SimpleTestCase):
    def setUp(self):
        key=base64.urlsafe_b64encode(bytes(range(32))).decode()
        env={'AGENT_SIGNING_KEY_B64':key,'FIELD_ENCRYPTION_KEY':key,
            'SPECTRUM_PROJECT_ID':'','SPECTRUM_PROJECT_SECRET':'','PHOTON_BRIDGE_SECRET':'','PHOTON_TEST_PHONE':''}
        patcher=patch.dict(os.environ,env);patcher.start();self.addCleanup(patcher.stop)
        self.settings_override=override_settings(TANDEM_DEPLOYMENT=True,DEBUG=False,SECRET_KEY='9'*64,
            PAYMENTS_GATEWAY='simulated',CELERY_TASK_ALWAYS_EAGER=False,
            DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','HOST':'db.internal','OPTIONS':{'sslmode':'require'}}},
            REDIS_URL='rediss://redis.internal:6380/0',CACHES={'default':{'BACKEND':'django.core.cache.backends.redis.RedisCache','LOCATION':'redis://cache.invalid'}},
            PUBLIC_BASE_URL='https://tandem.test',ALLOWED_HOSTS=['tandem.test'],CSRF_TRUSTED_ORIGINS=['https://tandem.test'],
            MERCHANT_MODE='http',MERCHANT_BASE_URL='https://tandem.test/merchants',TANDEM_WEB_CONCURRENCY=3,DEMO_ADMIN_KEY='')
        self.settings_override.enable();self.addCleanup(self.settings_override.disable)

    def test_valid_config_requires_no_network_and_has_no_errors(self):
        self.assertEqual(deployment_problems(),[])

    def test_bad_configuration_has_stable_codes_without_secret_values(self):
        cases=[({'DEBUG':True},'tandem.E001'),({'CELERY_TASK_ALWAYS_EAGER':True},'tandem.E004'),
            ({'REDIS_URL':'redis://localhost:6379'},'tandem.E007'),({'PUBLIC_BASE_URL':'https://user:private-password@tandem.test'},'tandem.E009'),
            ({'ALLOWED_HOSTS':['*']},'tandem.E010'),({'TANDEM_WEB_CONCURRENCY':1},'tandem.E014'),
            ({'PAYMENTS_GATEWAY':'real'},'tandem.E003')]
        for values,code in cases:
            with self.subTest(code=code),override_settings(**values):
                errors=deployment_problems()
                self.assertIn(code,{e.id for e in errors})
                self.assertNotIn('private-password',str(errors))

    def test_missing_encryption_or_signing_key_is_detected(self):
        with patch.dict(os.environ,{'FIELD_ENCRYPTION_KEY':'','AGENT_SIGNING_KEY_B64':''}):
            self.assertTrue({'tandem.E015','tandem.E016'}<={e.id for e in deployment_problems()})


class ObservabilityTests(SimpleTestCase):
    def test_logs_redact_credentials_private_links_contact_and_card_details(self):
        with patch.dict(os.environ,{'SOME_API_KEY':'secret-fixture-should-not-appear'}):
            record=logging.LogRecord('test',logging.ERROR,'',0,
                'key=secret-fixture-should-not-appear https://host/c/capability?owner=secret +14257377163 buyer@example.com 4111 1111 1111 1111 /api/v1/consent/hidden-token',(),None)
            text=ops.SafeJSONFormatter().format(record)
        for secret in ('secret-fixture-should-not-appear','capability','14257377163','buyer@example.com','4111','hidden-token'):
            self.assertNotIn(secret,text)
        self.assertEqual(json.loads(text)['level'],'ERROR')

    def test_request_id_propagates_to_published_task_and_resets(self):
        headers={}
        def view(request):
            ops.publish_trace(headers=headers)
            return HttpResponse('ok')
        middleware=ops.RequestTracingMiddleware(view)
        response=middleware(RequestFactory().get('/c/do-not-log?owner=private',HTTP_X_REQUEST_ID='trace-123'))
        self.assertEqual(headers['x-tandem-request-id'],'trace-123')
        self.assertEqual(response['X-Request-ID'],'trace-123')
        self.assertEqual(ops.request_id.get(),'')
        self.assertNotEqual(ops.valid_id('bad\nid'),'bad\nid')

    def test_task_context_does_not_leak_to_next_task(self):
        task=SimpleNamespace(name='safe.task',request=SimpleNamespace(headers={'x-tandem-request-id':'trace-task'}))
        ops.start_task(task=task,task_id='task-123')
        self.assertEqual(ops.request_id.get(),'trace-task')
        self.assertEqual(ops.task_id.get(),'task-123')
        ops.finish_task(task=task,state='SUCCESS')
        self.assertEqual(ops.request_id.get(),'')
        self.assertEqual(ops.task_id.get(),'')

    def test_exception_messages_are_not_included_in_json_tracebacks(self):
        try:
            raise ValueError('private assertion payload')
        except ValueError:
            import sys
            record=logging.LogRecord('test',logging.ERROR,'',0,'operation failed',(),sys.exc_info())
        result=ops.SafeJSONFormatter().format(record)
        self.assertNotIn('private assertion payload',result)
        self.assertEqual(json.loads(result)['error_type'],'ValueError')


class SchedulerTests(SimpleTestCase):
    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    @patch('celery.beat.PersistentScheduler.setup_schedule')
    def test_second_scheduler_fails_before_reading_or_dispatching_schedule(self,setup):
        from tandem.beat import SingletonScheduler
        client=Mock();client.lock.return_value.acquire.return_value=False
        scheduler=object.__new__(SingletonScheduler)
        with patch('tandem.beat.redis_client',return_value=client),self.assertRaises(SystemExit):
            scheduler.setup_schedule()
        setup.assert_not_called()

    def test_lost_scheduler_lease_stops_dispatch(self):
        from tandem.beat import SingletonScheduler
        scheduler=object.__new__(SingletonScheduler);scheduler._lease=Mock()
        scheduler._lease.extend.side_effect=ConnectionError('redis credentials must not be logged')
        with patch('celery.beat.PersistentScheduler.tick') as tick,self.assertRaises(SystemExit):
            scheduler.tick()
        tick.assert_not_called()

    def test_scheduler_renews_before_dispatch_and_limits_sleep(self):
        from tandem.beat import SingletonScheduler
        scheduler=object.__new__(SingletonScheduler);scheduler._lease=Mock()
        with patch('celery.beat.PersistentScheduler.tick',return_value=60):
            self.assertEqual(scheduler.tick(),10)
        scheduler._lease.extend.assert_called_once_with(45,replace_ttl=True)
