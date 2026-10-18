from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.events import EventType
from core.models import EventLog, Member
from payments.models import AgentToken, Hold
from payments.services import make_consent_token
from payments.simulated import SimulatedVisaAgentGateway
from purchases.models import Option, Purchase, Share

SHARE_CENTS = 17_500
TOTAL_CENTS = SHARE_CENTS * 6
REQUIRED_CONSENT_KEYS = {
    "member", "invited_by", "group_name", "purchase", "share_cents", "default_cap_cents",
    "deadline_at", "can", "can_never", "consent_text_version", "consent_text_sha256", "simulated", "offers",
}
OTP = {"method": "otp", "code": "000000"}


def build_purchase(member_name="Jordan"):
    call_command("seed_demo", stdout=StringIO())
    members = list(Member.objects.filter(group__name="Tahoe weekend"))
    first = members[0]
    option = Option.objects.create(
        group=first.group, merchant_slug="pinestay", listing_ref="pinecrest-a-frame", category="rental",
        title="Pinecrest A-Frame", total_cents=TOTAL_CENTS, capacity=6,
        meta={"dates": "Nov 14 to 16", "photo_url": "/static/demo/pinecrest.jpg", "demo_data": True})
    purchase = Purchase.objects.create(
        group=first.group, option=option, state=Purchase.State.COLLECTING, total_cents=TOTAL_CENTS,
        deadline_at=timezone.now() + timedelta(days=3))
    shares = {}
    for member in members:
        shares[member.display_name] = Share.objects.create(
            purchase=purchase, member=member, amount_cents=SHARE_CENTS, status=Share.Status.INVITED,
            purchase_version=purchase.version)
    target = Member.objects.get(display_name=member_name)
    return shares[member_name], make_consent_token(shares[member_name]), target


class ConsentApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.gw = SimulatedVisaAgentGateway(on_event=lambda t, p: None, latency_ms=0, force_decline_members=set())
        self.addCleanup(mock.patch("payments.services.get_gateway", return_value=self.gw).start())
        self.addCleanup(mock.patch("purchases.rules.get_gateway", return_value=self.gw).start())

    def _approve(self, token, last4="1111", cap=20000, sha=None, assertion=None):
        payload = self.client.get(f"/api/v1/consent/{token}?cap_cents={cap}").json()
        tok = self.client.post(f"/api/v1/consent/{token}/tokenize",
                               {"transient_card_token": f"tt_sim_{last4}_ab12"}, format="json").json()
        return self.client.post(f"/api/v1/consent/{token}/approve", {
            "agent_token_id": tok["agent_token_id"], "cap_cents": cap,
            "consent_text_sha256": sha or payload.get("consent_text_sha256", "invalid"),
            "assertion": assertion or OTP,
        }, format="json")

    def test_get_payload_shape(self):
        share, token, _ = build_purchase()
        resp = self.client.get(f"/api/v1/consent/{token}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(REQUIRED_CONSENT_KEYS.issubset(body))
        self.assertEqual(body["consent_text_version"], "v1")
        self.assertTrue(body["simulated"])
        self.assertEqual(body["share_cents"], SHARE_CENTS)
        share.refresh_from_db()
        self.assertEqual(share.status, "viewing")

    def test_pan_rejected(self):
        _, token, _ = build_purchase()
        resp = self.client.post(f"/api/v1/consent/{token}/tokenize",
                                {"transient_card_token": "4111111111111111"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "pan_not_allowed")

    def test_wrong_member_token_403(self):
        _, token, maya = build_purchase("Maya")
        jordan = Member.objects.get(display_name="Jordan")
        resp = self.client.get(f"/api/v1/consent/{token}", HTTP_X_TANDEM_MEMBER=str(jordan.id))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "auth_denied")
        self.assertTrue(EventLog.objects.filter(type=EventType.AUTH_DENIED).exists())
        self.assertEqual(maya.display_name, "Maya")

    def test_decline_hides_reason_from_group(self):
        share, token, member = build_purchase()
        resp = self.client.post(f"/api/v1/consent/{token}/decline", {"reason": "too_expensive"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["share_status"], "declined")
        group = self.client.get(f"/api/v1/purchases/{share.purchase_id}",
                                HTTP_X_TANDEM_MEMBER=str(member.id)).json()
        blob = str(group)
        self.assertNotIn("too_expensive", blob)
        event = EventLog.objects.filter(type=EventType.CONSENT_DECLINED).latest("seq")
        self.assertEqual(event.payload["reason"], "too_expensive")

    @override_settings(DEMO_MODE=True)
    def test_card_1111_held(self):
        share, token, _ = build_purchase("Alex")
        resp = self._approve(token, last4="1111", cap=21000)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["share_status"], "held")
        self.assertEqual(body["hold"]["status"], "AUTHORIZED")
        self.assertTrue(body["simulated"])
        share.refresh_from_db()
        self.assertEqual(share.status, "held")
        self.assertEqual(Hold.objects.filter(share=share, status="authorized").count(), 1)

    @override_settings(DEMO_MODE=True)
    def test_card_0002_declined(self):
        share, token, _ = build_purchase("Jordan")
        resp = self._approve(token, last4="0002", cap=20000)
        self.assertEqual(resp.status_code, 402, resp.content)
        self.assertEqual(resp.json()["error"]["code"], "card_declined")
        self.assertIn("Nothing was charged", resp.json()["error"]["message"])
        share.refresh_from_db()
        self.assertEqual(share.status, "card_failed")
        self.assertFalse(Hold.objects.filter(share=share, status="authorized").exists())

    @override_settings(DEMO_MODE=True)
    def test_double_approve_one_hold(self):
        share, token, _ = build_purchase("Maya")
        a = self._approve(token, last4="1111", cap=25000)
        b = self._approve(token, last4="1111", cap=25000)
        self.assertEqual(a.status_code, 200, a.content)
        self.assertEqual(b.status_code, 200, b.content)
        self.assertEqual(a.json()["hold"]["hold_id"], b.json()["hold"]["hold_id"])
        self.assertEqual(Hold.objects.filter(share=share, status="authorized").count(), 1)

    @override_settings(DEMO_MODE=True)
    def test_cap_raise_rejected(self):
        _, token, _ = build_purchase("Jordan")
        resp = self._approve(token, last4="1111", cap=25000)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "cap_out_of_range")

    @override_settings(DEMO_MODE=True)
    def test_hash_mismatch_rejected(self):
        _, token, _ = build_purchase("Alex")
        resp = self._approve(token, last4="1111", cap=21000, sha="deadbeef")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "consent_text_changed")

    @override_settings(DEMO_MODE=True)
    def test_wrong_otp_rejected(self):
        _, token, _ = build_purchase("Alex")
        resp = self._approve(token, last4="1111", cap=21000, assertion={"method": "otp", "code": "111111"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "otp_invalid")

    def test_expired_token_410(self):
        share, token, _ = build_purchase()
        share.purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        share.purchase.save()
        resp = self.client.get(f"/api/v1/consent/{token}")
        self.assertEqual(resp.status_code, 410)
        self.assertEqual(resp.json()["error"]["code"], "link_expired")

    @override_settings(DEMO_MODE=True)
    def test_reconfirm_without_an_earlier_hold_needs_the_consent_page(self):
        """The rules exist now (M6). A share that replaced nothing has no limit to re-approve, so use the link."""
        share, _, member = build_purchase("Alex")
        resp = self.client.post(
            f"/api/v1/shares/{share.id}/reconfirm",
            {"assertion": OTP}, format="json", HTTP_X_TANDEM_MEMBER=str(member.id))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "consent_required")

    def test_tokenize_persists_last4_only(self):
        _, token, member = build_purchase("Alex")
        self.client.get(f"/api/v1/consent/{token}")
        resp = self.client.post(f"/api/v1/consent/{token}/tokenize",
                                {"transient_card_token": "tt_sim_1111_9b2c"}, format="json")
        self.assertEqual(resp.status_code, 200)
        row = AgentToken.objects.get(member=member)
        self.assertEqual(row.card_last4, "1111")
        self.assertNotIn("4111", str(row.scope))


    @override_settings(DEMO_MODE=True)
    def test_declined_card_can_be_replaced(self):
        share, token, _ = build_purchase("Jordan")
        self.assertEqual(self._approve(token, last4="0002").status_code, 402)
        response = self._approve(token, last4="1111")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Hold.objects.filter(share=share, status="authorized").count(), 1)

    def test_lowered_cap_changes_copy_and_hash(self):
        _, token, _ = build_purchase("Alex")
        original = self.client.get(f"/api/v1/consent/{token}").json()
        lowered = self.client.get(f"/api/v1/consent/{token}?cap_cents=18000").json()
        self.assertNotEqual(original["consent_text_sha256"], lowered["consent_text_sha256"])
        self.assertIn("$180.00", lowered["can_never"][0])

    def test_tokenization_retry_and_new_attempt(self):
        _, token, _ = build_purchase("Alex")
        url = f"/api/v1/consent/{token}/tokenize"
        a = self.client.post(url, {"transient_card_token": "tt_sim_1111_attemptA"}, format="json")
        b = self.client.post(url, {"transient_card_token": "tt_sim_1111_attemptA"}, format="json")
        c = self.client.post(url, {"transient_card_token": "tt_sim_0002_attemptB"}, format="json")
        self.assertEqual(a.json(), b.json())
        self.assertEqual(c.status_code, 200)
        self.assertNotEqual(a.json()["agent_token_id"], c.json()["agent_token_id"])

    @override_settings(DEMO_MODE=True)
    def test_reconfirm_records_fresh_consent_when_backend_available(self):
        from payments.models import Consent
        share, _, member = build_purchase("Alex")
        Consent.objects.create(member=member, share=share, consent_text_version="v1",
            consent_text_sha256="0" * 64, amount_cents=17000, cap_cents=21000,
            method="otp", accepted_at=timezone.now())
        with mock.patch("purchases.rules.apply_share_change", create=True, return_value={"share_status": "held"}) as apply:
            response = self.client.post(f"/api/v1/shares/{share.id}/reconfirm", {"assertion": OTP},
                format="json", HTTP_X_TANDEM_MEMBER=str(member.id))
        self.assertEqual(response.status_code, 200, response.content)
        fresh = Consent.objects.get(pk=apply.call_args.args[1])
        self.assertEqual(fresh.amount_cents, share.amount_cents)
        self.assertEqual(fresh.cap_cents, 21000)

    def test_spaced_pan_in_unexpected_field_rejected(self):
        _, token, _ = build_purchase("Alex")
        response = self.client.post(f"/api/v1/consent/{token}/tokenize",
            {"transient_card_token": "tt_sim_1111_attempt", "unexpected": "4111 1111 1111 1111"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "pan_not_allowed")
