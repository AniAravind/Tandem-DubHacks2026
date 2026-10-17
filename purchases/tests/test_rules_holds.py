"""
Tests for the money core (purchases/rules.py).

Each test starts from the same small world, built by `build_world()`:
  - the 6-person Tahoe demo group (from seed_demo)
  - one purchase in the "collecting" state, total $1,050.00, split into 6 shares of $175.00
  - every share is "viewing" and every member already has a payment token and a signed consent
The payment bank is the simulated gateway, created fresh for each test.
"""
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import close_old_connections, OperationalError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from core.models import EventLog, Member
from payments.gateway import Money
from payments.models import AgentToken, Consent, Hold, Mandate
from payments.simulated import SimulatedVisaAgentGateway
from purchases import rules
from purchases.models import Option, Purchase, Share

SHARE_CENTS = 17_500          # $175.00 x 6 = $1,050.00
TOTAL_CENTS = SHARE_CENTS * 6
CAP_CENTS = 19_000            # what each member approved on the consent page


def build_world(gateway, all_cards_ok=False):
    """Create the group, purchase, shares, tokens and consents. Returns (purchase, {name: (share, consent, token)})."""
    call_command("seed_demo", stdout=StringIO())
    members = {m.display_name: m for m in Member.objects.all()}
    first = next(iter(members.values()))
    option = Option.objects.create(
        group=first.group, merchant_slug="pinestay", listing_ref="pinecrest-a-frame", category="rental",
        title="Pinecrest A-Frame", total_cents=TOTAL_CENTS, capacity=6)
    purchase = Purchase.objects.create(
        group=first.group, option=option, state=Purchase.State.COLLECTING, total_cents=TOTAL_CENTS,
        deadline_at=timezone.now() + timedelta(days=3))
    parts = {}
    for name, member in members.items():
        share = Share.objects.create(purchase=purchase, member=member, amount_cents=SHARE_CENTS,
                                     status=Share.Status.VIEWING)
        last4 = "1111" if all_cards_ok else member.prefs["demo_card_last4"]
        scope = {"purchase_id": str(purchase.id)}
        resp = gateway.provision_agent_token(str(member.id), f"tt_sim_{last4}_abc", scope, f"tok-{member.id}")
        token = AgentToken.objects.create(
            member=member, gateway_token_id=resp["agent_token_id"], card_brand="visa", card_last4=last4,
            scope=scope, expires_at=timezone.now() + timedelta(days=7))
        consent = Consent.objects.create(
            member=member, share=share, consent_text_version="v1", consent_text_sha256="0" * 64,
            amount_cents=SHARE_CENTS, cap_cents=CAP_CENTS, method="passkey", accepted_at=timezone.now())
        parts[name] = (share, consent, token)
    return purchase, parts


def approve(name, parts, cap=CAP_CENTS):
    share, consent, token = parts[name]
    return rules.place_hold(share.id, consent.id, token.id, cap)


class MoneyCoreBase(TestCase):
    def setUp(self):
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0)
        patcher = mock.patch("purchases.rules.get_gateway", return_value=self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)


class PlaceHoldTests(MoneyCoreBase):
    def test_happy_path_hold(self):
        purchase, parts = build_world(self.gateway)
        result = approve("Maya", parts)
        self.assertEqual(result["share_status"], "held")
        self.assertEqual(result["hold"]["status"], "authorized")
        self.assertEqual(result["hold"]["amount_cents"], SHARE_CENTS)
        self.assertEqual(result["group_counts"], {"held": 1, "active": 6})
        self.assertTrue(result["hold"]["simulated"])
        self.assertEqual(Mandate.objects.get(share=parts["Maya"][0]).cap_cents, CAP_CENTS)
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, "collecting")            # 5 members have not held yet

    def test_cap_below_share_is_rejected(self):
        _, parts = build_world(self.gateway)
        result = approve("Maya", parts, cap=SHARE_CENTS - 1)
        self.assertEqual(result["error"]["code"], "cap_exceeded")
        self.assertEqual(Hold.objects.count(), 0)

    def test_cap_over_hard_limit_is_rejected(self):
        _, parts = build_world(self.gateway)
        result = approve("Maya", parts, cap=rules.MAX_SHARE_CENTS + 1)
        self.assertEqual(result["error"]["code"], "over_limit")

    def test_decline_with_card_0002(self):
        _, parts = build_world(self.gateway)                     # Jordan's demo card ends in 0002
        result = approve("Jordan", parts)
        self.assertEqual(result["share_status"], "card_failed")
        self.assertEqual(result["hold"]["status"], "declined")
        self.assertEqual(result["hold"]["decline_code"], "do_not_honor")
        share = parts["Jordan"][0]
        share.refresh_from_db()
        self.assertEqual(share.card_attempts, 1)
        self.assertEqual(share.status, "card_failed")

    def test_three_declines_blocks_a_fourth_try(self):
        _, parts = build_world(self.gateway)
        for _ in range(3):
            self.assertEqual(approve("Jordan", parts)["share_status"], "card_failed")
        self.assertEqual(approve("Jordan", parts)["error"]["code"], "too_many_declines")

    def test_deadline_passed(self):
        purchase, parts = build_world(self.gateway)
        purchase.deadline_at = timezone.now() - timedelta(minutes=1)
        purchase.save()
        self.assertEqual(approve("Maya", parts)["error"]["code"], "deadline_passed")

    def test_consent_for_wrong_amount_is_rejected(self):
        _, parts = build_world(self.gateway)
        share, consent, _ = parts["Maya"]
        consent.amount_cents = SHARE_CENTS - 100
        consent.save()
        self.assertEqual(approve("Maya", parts)["error"]["code"], "consent_mismatch")

    def test_someone_elses_consent_is_rejected(self):
        _, parts = build_world(self.gateway)
        share, _, token = parts["Maya"]
        other_consent = parts["Priya"][1]
        result = rules.place_hold(share.id, other_consent.id, token.id, CAP_CENTS)
        self.assertEqual(result["error"]["code"], "consent_required")

    def test_double_call_is_idempotent(self):
        _, parts = build_world(self.gateway)
        first = approve("Maya", parts)
        second = approve("Maya", parts)
        self.assertEqual(first["hold"]["hold_id"], second["hold"]["hold_id"])
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 1)
        self.assertEqual(len(self.gateway.holds), 1)


class FundingTests(MoneyCoreBase):
    def test_six_consents_reach_funded(self):
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        for name in parts:
            approve(name, parts)
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, "funded")
        self.assertIsNotNone(purchase.funded_at)

    def test_five_of_six_is_not_funded(self):
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        for name in list(parts)[:5]:
            approve(name, parts)
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, "collecting")
        self.assertEqual(rules.check_funded(purchase.id)["funded"], False)

    def test_nothing_is_captured_by_the_money_core(self):
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        for name in parts:
            approve(name, parts)
        self.assertEqual(self.gateway.captures, {})                # M1 never captures; that is M4


class ReleaseTests(MoneyCoreBase):
    def test_release_all_frees_every_hold(self):
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        for name in list(parts)[:3]:
            approve(name, parts)
        result = rules.release_all(purchase.id, "organizer_cancelled")
        self.assertEqual(result["holds_released"], 3)
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, "released")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)
        self.assertEqual(Mandate.objects.filter(status="active").count(), 0)
        self.assertTrue(all(s.status == "released" for s in Share.objects.filter(purchase=purchase)))
        self.assertTrue(all(h["status"] == "RELEASED" for h in self.gateway.holds.values()))

    def test_member_withdraw_after_hold(self):
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        approve("Maya", parts)
        approve("Priya", parts)
        result = rules.member_withdraw(parts["Maya"][0].id, "not_for_me")
        self.assertEqual(result["share_status"], "released")
        self.assertEqual(result["group_counts"], {"held": 1, "active": 5})      # the member who backed out is no longer counted
        purchase.refresh_from_db()
        self.assertEqual(purchase.state, "collecting")             # the rest of the group is untouched

    def test_member_withdraw_before_hold_is_declined(self):
        _, parts = build_world(self.gateway, all_cards_ok=True)
        result = rules.member_withdraw(parts["Jordan"][0].id, "not_for_me")
        self.assertEqual(result["share_status"], "declined")

    def test_cannot_withdraw_twice(self):
        _, parts = build_world(self.gateway, all_cards_ok=True)
        rules.member_withdraw(parts["Jordan"][0].id, "not_for_me")
        self.assertEqual(rules.member_withdraw(parts["Jordan"][0].id, "again")["error"]["code"], "illegal_state")


class ConcurrencyTests(TransactionTestCase):
    """All six members tap Approve at the same moment. The purchase must become funded exactly once."""

    def test_no_capture_unless_all_held(self):
        gateway = SimulatedVisaAgentGateway(latency_ms=0)
        with mock.patch("purchases.rules.get_gateway", return_value=gateway):
            purchase, parts = build_world(gateway, all_cards_ok=True)
            checkout_calls = []
            errors = []

            def approve_in_thread(name):
                # SQLite (our local test database) only allows one writer at a time, so a thread that bumps
                # into "database is locked" just tries again. Postgres on Heroku locks rows and does not need this.
                for _ in range(50):
                    try:
                        approve(name, parts)
                        return
                    except OperationalError:
                        close_old_connections()
                        threading.Event().wait(0.05)
                errors.append(name)

            with mock.patch("purchases.rules.checkout", side_effect=lambda pid: checkout_calls.append(pid)):
                threads = [threading.Thread(target=approve_in_thread, args=(n,)) for n in parts]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            self.assertEqual(errors, [])
            purchase.refresh_from_db()
            self.assertEqual(purchase.state, "funded")
            self.assertEqual(len(checkout_calls), 1)                        # funded -> checkout, exactly once
            funded_events = [e for e in EventLog.objects.filter(type="purchase.state_changed")
                             if e.payload.get("to") == "funded"]
            self.assertEqual(len(funded_events), 1)
            self.assertEqual(gateway.captures, {})                          # and nothing was charged
            self.assertEqual(Hold.objects.filter(status="authorized").count(), 6)
