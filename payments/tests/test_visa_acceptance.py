"""Visa Acceptance sandbox gateway: signing, test-card substitution, and a full group flow through the rules engine.

A recording stand-in replaces Visa's HTTP server, so these run offline and never need credentials."""
import base64
import hashlib
import hmac
import itertools
import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings

from core.events import log_event_simulated
from core.models import EventLog
from payments.gateway import GatewayError, Money
from payments.models import Hold
from payments.visa_acceptance import TEST_CARD, VisaAcceptanceGateway, VisaClient
from purchases import rules
from purchases.payment_mode import payments_label, payments_mode
from purchases.tests.test_fallbacks import FallbackBase

SECRET = base64.b64encode(b"tandem-test-shared-secret-32byte").decode()


class FakeVisa:
    """Answers like Visa's sandbox: authorizations get a 22-digit id; captures/reversals/refunds succeed."""

    def __init__(self, decline=False):
        self.calls, self.decline, self.ids = [], decline, itertools.count(7000000000000000000001)

    def post(self, path, content, headers):
        body = json.loads(content)
        self.calls.append((path, body, headers))
        if path == "/pts/v2/payments":
            status = "DECLINED" if self.decline else "AUTHORIZED"
            data = {"id": str(next(self.ids)), "status": status}
            if self.decline:
                data["errorInformation"] = {"reason": "PROCESSOR_DECLINED"}
        elif path.endswith("/captures"):
            data = {"id": str(next(self.ids)), "status": "PENDING"}
        elif path.endswith("/reversals"):
            data = {"id": str(next(self.ids)), "status": "REVERSED"}
        else:
            data = {"id": str(next(self.ids)), "status": "PENDING"}
        return SimpleNamespace(status_code=201, json=lambda: data)


def gateway(fake=None):
    client = VisaClient("tandem_test", "key-123", SECRET, http=fake or FakeVisa())
    return VisaAcceptanceGateway(client=client, on_event=log_event_simulated, latency_ms=0)


class SigningTests(TestCase):
    def test_http_signature_matches_visa_format(self):
        client = VisaClient("tandem_test", "key-123", SECRET, http=FakeVisa())
        body = b'{"a":1}'
        h = client.headers("POST", "/pts/v2/payments", body, date="Sat, 18 Oct 2026 09:00:00 GMT")
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        signing = ("host: apitest.cybersource.com\ndate: Sat, 18 Oct 2026 09:00:00 GMT\n(request-target): post /pts/v2/payments\n"
                   f"digest: {digest}\nv-c-merchant-id: tandem_test")
        expected = base64.b64encode(hmac.new(base64.b64decode(SECRET), signing.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(h["digest"], digest)
        self.assertEqual(h["v-c-merchant-id"], "tandem_test")
        self.assertIn('keyid="key-123"', h["signature"])
        self.assertIn('headers="host date (request-target) digest v-c-merchant-id"', h["signature"])
        self.assertIn(f'signature="{expected}"', h["signature"])


class GatewayUnitTests(TestCase):
    _n = itertools.count()

    def mandate(self, g, last4="9999", cap=20000):
        n = next(self._n)
        tok = g.provision_agent_token("m1", f"tt_sim_{last4}_abc{n}", {"merchant_ids": ["pinestay"]}, f"k-tok{n}")
        return g.create_purchase_mandate(tok["agent_token_id"], Money(cap, "USD"), "pinestay", "pinecrest",
                                         "2099-01-01T00:00:00Z", "consent-1", f"k-mdt{n}")["mandate_id"]

    def test_any_typed_card_is_sent_as_visas_test_card(self):
        fake = FakeVisa()
        g = gateway(fake)
        hold = g.authorize_hold(self.mandate(g, last4="0002"), Money(17500, "USD"), "k-auth")
        self.assertEqual(hold["status"], "AUTHORIZED")                        # 0002 no longer forces a decline
        path, body, headers = fake.calls[0]
        self.assertEqual(path, "/pts/v2/payments")
        self.assertEqual(body["paymentInformation"]["card"]["number"], TEST_CARD["number"])
        self.assertEqual(body["processingInformation"]["capture"], False)
        self.assertEqual(body["orderInformation"]["amountDetails"], {"totalAmount": "175.00", "currency": "USD"})
        self.assertFalse(hold["simulated"])
        self.assertTrue(hold["sandbox"])

    def test_local_cap_is_enforced_before_calling_visa(self):
        fake = FakeVisa()
        g = gateway(fake)
        refused = g.authorize_hold(self.mandate(g, cap=10000), Money(17500, "USD"), "k-auth")
        self.assertEqual((refused["status"], refused["reason"]), ("REJECTED", "cap_exceeded"))
        self.assertEqual(fake.calls, [])

    def test_visa_decline_is_reported_as_decline(self):
        g = gateway(FakeVisa(decline=True))
        out = g.authorize_hold(self.mandate(g), Money(17500, "USD"), "k-auth")
        self.assertEqual((out["status"], out["decline_code"]), ("DECLINED", "processor_declined"))

    def test_capture_release_refund_hit_the_right_endpoints(self):
        fake = FakeVisa()
        g = gateway(fake)
        m = self.mandate(g)
        a = g.authorize_hold(m, Money(17500, "USD"), "k1")["hold_id"]
        cap = g.capture(a, Money(17500, "USD"), "k2")
        g.refund(cap["capture_id"], Money(17500, "USD"), "k3")
        m2 = self.mandate(g)
        b = g.authorize_hold(m2, Money(9000, "USD"), "k4")["hold_id"]
        g.release_hold(b, "left", "k5")
        paths = [c[0] for c in fake.calls]
        self.assertEqual(paths, ["/pts/v2/payments", f"/pts/v2/payments/{a}/captures", f"/pts/v2/captures/{cap['capture_id']}/refunds",
                                 "/pts/v2/payments", f"/pts/v2/payments/{b}/reversals"])

    def test_visa_outage_raises_gateway_error(self):
        class Down:
            def post(self, *a, **k):
                import httpx
                raise httpx.ConnectError("down")
        g = gateway(Down())
        with self.assertRaises(GatewayError) as ctx:
            g.authorize_hold(self.mandate(g), Money(17500, "USD"), "k")
        self.assertEqual(ctx.exception.code, "gateway_unavailable")

    def test_labels_follow_the_configured_rail(self):
        with override_settings(PAYMENTS_GATEWAY="visa_sandbox"):
            self.assertEqual((payments_mode(), payments_label()), ("visa_sandbox", "Visa sandbox · test mode"))
        with override_settings(PAYMENTS_GATEWAY="simulated"):
            self.assertEqual(payments_label(), "simulated")


class VisaGroupFlowTests(FallbackBase):
    """The Tahoe dropout story, with every hold/reversal going to (stand-in) Visa through the real rules engine."""

    def setUp(self):
        self.fake = FakeVisa()
        g = gateway(self.fake)
        patcher = mock.patch("purchases.rules.get_gateway", return_value=g)
        patcher.start()
        self.addCleanup(patcher.stop)
        from purchases.tests.test_rules_holds import build_world
        from core.models import Member
        self.gateway = g
        self.purchase, self.parts = build_world(g, all_cards_ok=True)
        self.group = self.purchase.group
        self.members = {m.display_name: m for m in Member.objects.all()}
        self.engine = rules.RulesEngine()

    def test_holds_are_visa_authorizations_and_reconfirm_reauthorizes(self):
        self.hold_everyone_but_jordan(cap=25000)
        visa_ids = set(Hold.objects.filter(status="authorized").values_list("gateway_hold_id", flat=True))
        self.assertEqual(len(visa_ids), 5)
        self.assertTrue(all(i.isdigit() and len(i) == 22 for i in visa_ids))          # Visa transaction ids
        self.assertFalse(EventLog.objects.filter(type="gateway.hold_authorized", simulated=True).exists())

        self.vote_all(self.propose()["vote_id"], "split_gap")                         # $175 -> $210 each
        maya = self.current_shares()["Maya"]
        old_id = Hold.objects.get(share__member=maya.member, status="authorized").gateway_hold_id
        result = rules.apply_share_change(maya.id, self.consent_for(maya, cap=25000).id)
        self.assertEqual((result["share_status"], result["amount_cents"]), ("held", 21000))
        hold = Hold.objects.get(share=maya, status="authorized")
        self.assertNotEqual(hold.gateway_hold_id, old_id)                             # Visa re-authorized
        self.assertEqual(hold.amount_cents, 21000)
        calls = [(c[0], c[1]) for c in self.fake.calls]
        self.assertIn(f"/pts/v2/payments/{old_id}/reversals", [p for p, _ in calls])
        self.assertEqual(calls[-1][1]["orderInformation"]["amountDetails"]["totalAmount"], "210.00")
