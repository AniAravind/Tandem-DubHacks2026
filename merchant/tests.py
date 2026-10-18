import json
import os
import base64
from unittest import mock
from io import StringIO
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.core.cache import cache
from merchant.signing import sign_request
from merchant.models import MerchantBooking

@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class MerchantHTTPTests(TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"AGENT_SIGNING_KEY_B64": base64.b64encode(bytes(range(32))).decode()})
        patcher.start()
        self.addCleanup(patcher.stop)
        call_command("seed_demo", stdout=StringIO())
        cache.clear()

    def signed(self, action, data, tamper=False, ttl=300, key="booking-test"):
        path = "/merchants/pinestay/" + action
        body = json.dumps(data, sort_keys=True).encode()
        tag = "agent-browser-auth" if action == "quotes" else "agent-payer-auth"
        headers = sign_request("testserver", path, body, tag, ttl)
        self.last_request = (path, body, headers)
        if tamper:
            body += b" "
        return self.client.post(path, body, content_type="application/json", headers={**headers, "Idempotency-Key": key})

    def quote(self):
        return self.signed("quotes", {"listing_ref": "juniper-loft", "guests": 5}).json()

    def payload(self):
        q = self.quote()
        return {"quote_id": q["quote_id"], "listing_ref": "juniper-loft", "amount_cents": 92500,
                "payment": {"mandate_ids": ["sim_mandate"]}}

    def test_quote_checkout_idempotency_and_cancel(self):
        data = self.payload()
        a = self.signed("agent/checkout", data)
        self.assertEqual(a.status_code, 201, a.content)
        b = self.signed("agent/checkout", data)
        self.assertEqual(a.json(), b.json())
        self.assertEqual(MerchantBooking.objects.count(), 1)
        cancel = self.signed("agent/cancel", {"booking_ref": a.json()["booking_ref"]})
        self.assertEqual(cancel.json()["refund_amount_cents"], 92500)

    def test_attacks_never_book(self):
        data = self.payload()
        tampered = self.signed("agent/checkout", data, tamper=True)
        self.assertEqual(tampered.json()["error"]["code"], "digest_mismatch")
        expired = self.signed("agent/checkout", data, ttl=-1)
        self.assertEqual(expired.json()["error"]["code"], "expired")
        unsigned = self.client.post("/merchants/pinestay/agent/checkout", data, content_type="application/json")
        self.assertEqual(unsigned.json()["error"]["code"], "signature_missing")
        self.assertFalse(MerchantBooking.objects.exists())

    def test_replay_and_mismatch(self):
        data = self.payload()
        data["amount_cents"] = 1
        mismatch = self.signed("agent/checkout", data)
        self.assertEqual(mismatch.status_code, 409)
        path, body, headers = self.last_request
        replay = self.client.post(path, body, content_type="application/json", headers=headers)
        self.assertEqual(replay.json()["error"]["code"], "nonce_replayed")
        self.assertFalse(MerchantBooking.objects.exists())

    def test_digest_coverage_required(self):
        path = "/merchants/pinestay/agent/checkout"
        headers = sign_request("testserver", path, None, "agent-payer-auth")
        result = self.client.post(path, b"{}", content_type="application/json", headers=headers)
        self.assertEqual(result.json()["error"]["code"], "digest_mismatch")

    def test_wrong_tag_unknown_agent_and_invalid_signature(self):
        from merchant import signing
        path = "/merchants/pinestay/agent/checkout"
        body = b"{}"
        wrong_tag = sign_request("testserver", path, body, "agent-browser-auth")
        response = self.client.post(path, body, content_type="application/json", headers=wrong_tag)
        self.assertEqual(response.json()["error"]["code"], "wrong_tag")
        with mock.patch.object(signing, "KEY_ID", "unknown-key"):
            unknown = sign_request("testserver", path, body, "agent-payer-auth")
        response = self.client.post(path, body, content_type="application/json", headers=unknown)
        self.assertEqual(response.json()["error"]["code"], "unknown_agent")
        forged = sign_request("testserver", path, body, "agent-payer-auth")
        forged["Signature"] = "sig2=:" + base64.b64encode(bytes(64)).decode() + ":"
        response = self.client.post(path, body, content_type="application/json", headers=forged)
        self.assertEqual(response.json()["error"]["code"], "signature_invalid")
        self.assertFalse(MerchantBooking.objects.exists())

    def test_nonce_backend_failure_is_closed(self):
        path = "/merchants/pinestay/agent/checkout"
        headers = sign_request("testserver", path, b"{}", "agent-payer-auth")
        with mock.patch("merchant.nonce_store.cache.add", side_effect=ConnectionError):
            response = self.client.post(path, b"{}", content_type="application/json", headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(MerchantBooking.objects.exists())
