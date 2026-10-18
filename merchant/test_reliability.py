import base64
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection,connections,close_old_connections
from django.test import Client,TestCase,TransactionTestCase,override_settings
from merchant.models import MerchantBooking,SignedRequest
from merchant.signing import sign_request


class SignedRequests:
    def request(self,action,data,key='retry-key'):
        path='/merchants/pinestay/'+action
        body=json.dumps(data,sort_keys=True).encode()
        tag='agent-browser-auth' if action=='quotes' else 'agent-payer-auth'
        headers=sign_request('testserver',path,body,tag)
        return Client().post(path,body,content_type='application/json',headers={**headers,'Idempotency-Key':key})

    def setUp(self):
        patcher=patch.dict(os.environ,{'AGENT_SIGNING_KEY_B64':base64.b64encode(bytes(range(32))).decode()})
        patcher.start();self.addCleanup(patcher.stop)
        call_command('seed_demo',stdout=StringIO())
        cache.clear()  # Django tests use a private in-process cache.

    def quote(self):
        return self.request('quotes',{'listing_ref':'juniper-loft','guests':5}).json()


@override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver'])
class MerchantValidationTests(SignedRequests,TestCase):
    def test_fractional_or_boolean_guest_counts_are_rejected(self):
        for value in (True,1.9,'1.9',{},[]):
            response=self.request('quotes',{'listing_ref':'juniper-loft','guests':value})
            self.assertEqual(response.status_code,400)

    def test_success_and_business_rejections_record_the_actual_http_status(self):
        quote=self.quote()
        self.assertEqual(SignedRequest.objects.latest('created_at').response_code,200)
        response=self.request('agent/checkout',{'listing_ref':'juniper-loft','quote_id':quote['quote_id'],'amount_cents':1,'payment':{'mandate_ids':['sim_mandate']}})
        self.assertEqual(response.status_code,409)
        self.assertEqual(SignedRequest.objects.latest('created_at').response_code,409)

    def test_malformed_timestamps_cannot_overflow_the_audit_table(self):
        headers={'Signature-Input':'sig2=("@authority" "@path");created='+('9'*100)+';expires='+('9'*100)+';keyid="x";nonce="n";tag="agent-payer-auth"','Signature':'sig2=:AA==:'}
        response=self.client.post('/merchants/pinestay/agent/checkout',b'{}',content_type='application/json',headers=headers)
        self.assertEqual(response.status_code,401)
        self.assertFalse(MerchantBooking.objects.exists())

    def test_request_id_in_error_matches_tracing_header(self):
        response=self.client.post('/merchants/pinestay/agent/checkout',{},content_type='application/json',HTTP_X_REQUEST_ID='trace-merchant')
        self.assertEqual(response.json()['error']['request_id'],response['X-Request-ID'])


@skipUnless(connection.vendor=='postgresql','Cross-connection checkout tests require PostgreSQL')
@override_settings(SECURE_SSL_REDIRECT=False,ALLOWED_HOSTS=['testserver'])
class ConcurrentCheckoutTests(SignedRequests,TransactionTestCase):
    def race(self,quotes,keys):
        def checkout(pair):
            quote,key=pair
            close_old_connections()
            try:
                response=self.request('agent/checkout',{'listing_ref':'juniper-loft','quote_id':quote['quote_id'],
                    'amount_cents':92500,'payment':{'mandate_ids':['sim_mandate']}},key)
                return response.status_code,response.json()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(checkout,zip(quotes,keys)))

    def test_same_key_and_quote_return_the_same_single_booking(self):
        quote=self.quote();results=self.race([quote,quote],['same-key','same-key'])
        self.assertEqual([r[0] for r in results],[201,201])
        self.assertEqual(results[0][1],results[1][1])
        self.assertEqual(MerchantBooking.objects.count(),1)

    def test_same_key_across_distinct_quotes_is_a_conflict_not_a_server_error(self):
        results=self.race([self.quote(),self.quote()],['same-key','same-key'])
        self.assertEqual(sorted(r[0] for r in results),[201,409])
        self.assertEqual(MerchantBooking.objects.count(),1)

    def test_same_quote_with_different_keys_cannot_double_book(self):
        quote=self.quote();results=self.race([quote,quote],['key-one','key-two'])
        self.assertEqual(sorted(r[0] for r in results),[201,409])
        self.assertEqual(MerchantBooking.objects.count(),1)


@skipUnless(os.getenv('RUN_REDIS_INTEGRATION')=='1','Set RUN_REDIS_INTEGRATION=1 for actual Redis integration')
class RedisLeaseTests(TestCase):
    def setUp(self):
        self.prefix='ops-tests-'+uuid.uuid4().hex
        self.override=override_settings(CACHES={'default':{'BACKEND':'django.core.cache.backends.redis.RedisCache',
            'LOCATION':os.environ['REDIS_URL'],'KEY_PREFIX':self.prefix,
            'OPTIONS':{'socket_connect_timeout':2,'socket_timeout':2}}})
        self.override.enable();self.addCleanup(self.override.disable)

    def test_nonce_claim_is_atomic_across_connections(self):
        from merchant.nonce_store import claim
        nonce=uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=16) as pool:
            results=list(pool.map(lambda _:claim('ops-tests',nonce,5),range(16)))
        self.assertEqual(sum(results),1)

    def test_expired_or_released_leader_cannot_delete_successor_lease(self):
        from tandem.health import redis_client
        client=redis_client();self.addCleanup(client.close)
        key=self.prefix+':beat'
        first=client.lock(key,timeout=10,blocking=False,thread_local=False)
        second=client.lock(key,timeout=10,blocking=False,thread_local=False)
        self.assertTrue(first.acquire(blocking=False))
        self.assertFalse(second.acquire(blocking=False))
        first.release()
        self.assertTrue(second.acquire(blocking=False))
        self.assertFalse(first.owned())
        with self.assertRaises(Exception):
            first.release()
        self.assertTrue(second.owned())
        second.release()
