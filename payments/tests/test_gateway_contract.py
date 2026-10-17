from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, TestCase
from django.utils import timezone as dj_tz
from io import StringIO

from django.core.management import call_command

from core.models import Member
from payments.gateway import Money, VisaAgentGateway
from payments.models import AgentToken, Hold, Mandate
from payments.simulated import SimulatedVisaAgentGateway
from purchases.models import Option, Purchase, Share

EXP = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z")


def run_contract(gw: VisaAgentGateway):
    tok = gw.provision_agent_token("mem_x", "tt_sim_1111_ab12", {"merchant_ids": ["pinestay"]}, "t-contract")
    assert set(tok) >= {"simulated", "agent_token_id", "status", "card"}
    assert tok["simulated"] is True
    mid = gw.create_purchase_mandate(
        tok["agent_token_id"], Money(20000), "pinestay", "pinecrest-a-frame", EXP, "cns_1", "m-contract")
    assert set(mid) >= {"simulated", "mandate_id", "status", "cap"}
    hold = gw.authorize_hold(mid["mandate_id"], Money(17500), "h-contract")
    assert hold["status"] == "AUTHORIZED"
    assert hold["simulated"] is True
    captured = gw.capture(hold["hold_id"], Money(17500), "c-contract")
    assert captured["status"] == "CAPTURED"
    refunded = gw.refund(captured["capture_id"], Money(17500), "r-contract")
    assert refunded["status"] == "REFUNDED"
    offers = gw.list_offers(tok["agent_token_id"], "summitpass")
    assert offers["simulated"] is True
    assert "offers" in offers


class GatewayContractTest(SimpleTestCase):
    def test_simulated_gateway_contract(self):
        run_contract(SimulatedVisaAgentGateway(on_event=lambda t, p: None, latency_ms=0, force_decline_members=set()))


class HydrateFromDbTest(TestCase):
    def test_hydrate_rebuilds_token_and_hold(self):
        call_command("seed_demo", stdout=StringIO())
        member = Member.objects.get(display_name="Maya")
        option = Option.objects.create(
            group=member.group, merchant_slug="pinestay", listing_ref="pinecrest-a-frame",
            category="rental", title="Pinecrest A-Frame", total_cents=105000, capacity=6)
        purchase = Purchase.objects.create(
            group=member.group, option=option, state=Purchase.State.COLLECTING, total_cents=105000,
            deadline_at=dj_tz.now() + timedelta(days=3))
        share = Share.objects.create(purchase=purchase, member=member, amount_cents=17500)
        from payments.models import Consent
        token = AgentToken.objects.create(
            member=member, gateway_token_id="atk_sim_dead", card_brand="visa", card_last4="1111",
            scope={}, expires_at=purchase.deadline_at, status="active")
        consent = Consent.objects.create(
            member=member, share=share, consent_text_version="v1", consent_text_sha256="0" * 64,
            amount_cents=17500, cap_cents=20000, method="otp", accepted_at=dj_tz.now())
        mandate = Mandate.objects.create(
            share=share, member=member, agent_token=token, consent=consent, merchant_slug="pinestay",
            listing_ref="pinecrest-a-frame", cap_cents=20000, expires_at=purchase.deadline_at,
            gateway_mandate_id="mdt_sim_dead", status=Mandate.Status.ACTIVE)
        Hold.objects.create(
            share=share, mandate=mandate, gateway_hold_id="auth_sim_dead", amount_cents=17500,
            status=Hold.Status.AUTHORIZED)
        gw = SimulatedVisaAgentGateway(on_event=lambda t, p: None, latency_ms=0)
        gw.hydrate_from_db()
        self.assertEqual(gw.tokens["atk_sim_dead"]["last4"], "1111")
        self.assertEqual(gw.mandates["mdt_sim_dead"]["cap"], 20000)
        self.assertEqual(gw.holds["auth_sim_dead"]["status"], "AUTHORIZED")
        released = gw.release_hold("auth_sim_dead", "restart", "rel-1")
        self.assertEqual(released["status"], "RELEASED")
