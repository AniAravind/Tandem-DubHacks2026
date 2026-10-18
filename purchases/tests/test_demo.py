"""
Tests for the demo tooling and session cookie (milestone M8).

The backstage buttons must be LOCKED unless DEMO_MODE=1 and the right X-Demo-Key is sent, and reset_seed must put the
database back to exactly the original seed.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from chat.models import ChatMessage
from core.events import log_event, verify_chain
from core.management.commands.seed_demo import MEMBERS, uid
from core.models import EventLog, Group, Member
from core.session import COOKIE_NAME, make_session_value
from merchant.models import Listing, SignedRequest
from payments.models import AgentToken, Hold
from payments.services import make_consent_token
from payments.simulated import SimulatedVisaAgentGateway
from purchases import demo
from purchases.models import Expense, Option, Purchase, Share, Vote
from purchases.tests.test_rules_holds import approve, build_world

KEY = "test-demo-key"
BASE = "/api/v1"


@override_settings(DEMO_MODE=True, DEMO_ADMIN_KEY=KEY)
class DemoBase(TestCase):
    def setUp(self):
        cache.clear()
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0)
        for target in ("purchases.rules.get_gateway", "purchases.tasks.get_gateway", "payments.gateway.get_gateway"):
            patcher = mock.patch(target, return_value=self.gateway)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("chat.services.schedule_agent")        # no AI calls in these tests
        patcher.start()
        self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)
        self.group = self.purchase.group
        self.client = APIClient()

    def post(self, action, body=None, key=KEY):
        headers = {"HTTP_X_DEMO_KEY": key} if key is not None else {}
        return self.client.post(f"{BASE}/demo/{action}", body or {}, format="json", **headers)


class LockTests(DemoBase):
    def test_every_action_is_locked_without_the_key(self):
        for action in ("simulate_decline", "tamper_next", "replay_last", "fast_forward", "reset_seed"):
            r = self.post(action, key=None)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"), action)
        self.assertEqual(Purchase.objects.count(), 1)                       # nothing ran
        self.assertIsNone(cache.get("demo:tamper_next"))

    def test_wrong_key_is_forbidden(self):
        self.assertEqual(self.post("tamper_next", key="nope").status_code, 403)
        self.assertIsNone(cache.get("demo:tamper_next"))

    @override_settings(DEMO_MODE=False)
    def test_demo_mode_off_locks_it_even_with_the_right_key(self):
        self.assertEqual(self.post("reset_seed").status_code, 403)
        self.assertEqual(Purchase.objects.count(), 1)

    @override_settings(DEMO_ADMIN_KEY="")
    def test_an_unset_admin_key_never_matches_an_empty_header(self):
        self.assertEqual(self.post("tamper_next", key="").status_code, 403)

    def test_unknown_action(self):
        r = self.post("launch_rockets")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "unknown_action"))


class FlagTests(DemoBase):
    def test_tamper_next_sets_the_switch_the_signer_reads(self):
        r = self.post("tamper_next")
        self.assertEqual((r.status_code, r.json()["armed"]), (200, "tamper_next"))
        self.assertTrue(cache.get("demo:tamper_next"))

    def test_replay_last_sets_its_switch(self):
        self.assertEqual(self.post("replay_last").status_code, 200)
        self.assertTrue(cache.get("demo:replay_last"))

    def test_actions_are_written_to_the_event_log(self):
        self.post("tamper_next")
        event = EventLog.objects.filter(type="demo.action").get()
        self.assertEqual((event.actor, event.payload["action"]), ("demo", "tamper_next"))


class SimulateDeclineTests(DemoBase):
    def test_jordan_declines_through_the_consent_path(self):
        r = self.post("simulate_decline")
        self.assertEqual((r.status_code, r.json()["member"], r.json()["share_status"]), (200, "Jordan", "declined"))
        statuses = {s.member.display_name: s.status for s in Share.objects.filter(purchase=self.purchase).select_related("member")}
        self.assertEqual(statuses["Jordan"], "declined")
        self.assertTrue(all(v == "viewing" for k, v in statuses.items() if k != "Jordan"))   # nobody else touched
        self.assertTrue(EventLog.objects.filter(type="consent.declined").exists())            # the same event the page logs

    def test_a_held_jordan_gets_the_hold_released(self):
        approve("Jordan", self.parts)
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 1)
        self.post("simulate_decline")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)

    def test_without_an_open_purchase(self):
        Purchase.objects.update(state="released")
        r = self.post("simulate_decline")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "no_open_purchase"))


class FastForwardTests(DemoBase):
    def test_nothing_due_means_nothing_happens(self):
        r = self.post("fast_forward")
        self.assertEqual(r.json()["ran"], {"votes": 0, "purchases": 0, "reminders": 0, "holds": 0})

    def test_to_deadline_releases_every_hold(self):
        approve("Maya", self.parts)
        approve("Priya", self.parts)
        r = self.post("fast_forward", {"to_deadline": True})
        self.assertEqual((r.json()["deadline_moved"], r.json()["ran"]["purchases"]), (1, 1))
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.state, "released")
        self.assertEqual(Hold.objects.filter(status="authorized").count(), 0)


class ResetSeedTests(DemoBase):
    def make_a_mess(self):
        approve("Maya", self.parts)
        Vote.objects.create(group=self.group, purpose="pick_option", quorum=4, closes_at=timezone.now(),
                            choices=[{"key": "1", "option_id": "x", "label": "X"}])
        maya = Member.objects.get(display_name="Maya")
        from purchases import ledger
        ledger.add_expense(self.group, str(maya.id), 6000, "Gas", "equal")
        ChatMessage.objects.create(group=self.group, direction="in", text_redacted="hello", seq=1)
        cache.set("demo:tamper_next", True)
        Listing.objects.filter(ref="pinecrest-a-frame").update(total_cents=1, available=False)
        jordan = Member.objects.get(display_name="Jordan")
        jordan.status = "left"
        jordan.save()
        Member.objects.create(group=self.group, display_name="Extra", role="member", phone_e164_hash="x", phone_last4="9999")
        log_event("reminder.sent", actor="scheduler", group_id=None, payload={"note": "an event from some other group"})

    def test_reset_restores_the_exact_seed(self):
        self.make_a_mess()
        r = self.post("reset_seed")
        self.assertEqual((r.status_code, r.json()["members"]), (200, 6))
        self.assertEqual((Purchase.objects.count(), Share.objects.count(), Option.objects.count(), Vote.objects.count(),
                          Expense.objects.count(), ChatMessage.objects.count(), Hold.objects.count(),
                          AgentToken.objects.count(), SignedRequest.objects.count()), (0,) * 9)
        self.assertIsNone(cache.get("demo:tamper_next"))
        self.assertEqual(self.gateway.holds, {})
        call_command("demo_check", stdout=StringIO())                       # ids, caps, listings and prices are back
        self.assertEqual(Group.objects.get().id, uid("group:tahoe"))
        self.assertEqual(sorted(Member.objects.values_list("display_name", flat=True)), sorted(m[0] for m in MEMBERS))
        self.assertEqual(Member.objects.get(display_name="Maya").id, uid("member:Maya"))
        self.assertEqual(Listing.objects.get(ref="pinecrest-a-frame").total_cents, 105000)

    def test_the_event_log_still_verifies_after_a_reset(self):
        self.make_a_mess()
        self.assertTrue(verify_chain())
        self.post("reset_seed")
        self.assertTrue(verify_chain())
        self.assertEqual(list(EventLog.objects.filter(group=self.group).values_list("type", flat=True)), ["demo.action"])
        self.assertEqual(EventLog.objects.filter(group__isnull=True).count(), 1)         # other groups' events were kept

    def test_resetting_twice_is_fine(self):
        self.post("reset_seed")
        self.assertEqual(self.post("reset_seed").status_code, 200)
        call_command("demo_check", stdout=StringIO())

    def test_a_fresh_purchase_can_be_built_after_a_reset(self):
        self.post("reset_seed")
        purchase, parts = build_world(self.gateway, all_cards_ok=True)
        self.assertEqual(approve("Maya", parts)["share_status"], "held")


class DemoCheckTests(DemoBase):
    def test_check_passes_on_a_fresh_seed(self):
        call_command("demo_check", stdout=StringIO())

    def test_check_fails_when_a_price_changes(self):
        Listing.objects.filter(ref="cedar-hollow").update(total_cents=1)
        with self.assertRaises(CommandError) as ctx:
            call_command("demo_check", stdout=StringIO())
        self.assertIn("cedar-hollow", str(ctx.exception))

    def test_check_fails_when_a_cap_changes(self):
        sam = Member.objects.get(display_name="Sam")
        sam.budget_cap_cents = 5
        sam.save()
        with self.assertRaises(CommandError):
            call_command("demo_check", stdout=StringIO())


# ---------------------------------------------------------------------------------------- session cookie
class SessionCookieTests(DemoBase):
    def setUp(self):
        super().setUp()
        self.maya = Member.objects.get(display_name="Maya")
        self.url = f"{BASE}/groups/{self.group.id}"

    def test_opening_a_consent_link_signs_the_browser_in(self):
        share = self.parts["Maya"][0]
        share.refresh_from_db()
        r = self.client.get(f"{BASE}/consent/{make_consent_token(share)}")
        self.assertEqual(r.status_code, 200)
        cookie = r.cookies[COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(self.client.get(self.url).status_code, 200)       # no header needed any more

    @override_settings(DEMO_MODE=False)
    def test_the_cookie_works_even_when_the_demo_header_is_off(self):
        self.client.cookies[COOKIE_NAME] = make_session_value(self.maya.id)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_tampered_cookie_is_refused(self):
        self.client.cookies[COOKIE_NAME] = make_session_value(self.maya.id)[:-3] + "xyz"
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_a_cookie_for_someone_outside_the_group_is_403(self):
        other = Group.objects.create(name="Other")
        stranger = Member.objects.create(group=other, display_name="Stranger", role="member", phone_e164_hash="y",
                                         phone_last4="1234")
        self.client.cookies[COOKIE_NAME] = make_session_value(stranger.id)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    @override_settings(DEMO_MODE=False)
    def test_no_cookie_and_no_demo_mode_means_signed_out(self):
        r = self.client.get(self.url, HTTP_X_TANDEM_MEMBER=str(self.maya.id))
        self.assertEqual(r.status_code, 401)
