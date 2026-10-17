"""
Tests for the real read APIs (milestone M2).

The shape tests compare against the example JSON in purchases/fixtures.py (the 6.3 contract), so if a field is
renamed or dropped, a test fails before Rishab's dashboard does.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.events import log_event_simulated, verify_chain
from core.models import EventLog, Group, Member
from payments.simulated import SimulatedVisaAgentGateway
from purchases.fixtures import FIXTURES
from purchases.models import Expense, ExpenseSplit, Vote
from purchases.tests.test_rules_holds import SHARE_CENTS, approve, build_world

BASE = "/api/v1"


class ApiTestBase(TestCase):
    def setUp(self):
        # A gateway that writes its events to the real event log, like production does.
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0, on_event=log_event_simulated)
        patcher = mock.patch("purchases.rules.get_gateway", return_value=self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)
        self.group = self.purchase.group
        self.client = APIClient()
        self.maya = Member.objects.get(display_name="Maya")
        self.priya = Member.objects.get(display_name="Priya")

    def get(self, url, member=None):
        headers = {"HTTP_X_TANDEM_MEMBER": str(member.id)} if member else {}
        return self.client.get(url, **headers)

    def send(self, method, url, member, data=None):
        return getattr(self.client, method)(url, data or {}, format="json", HTTP_X_TANDEM_MEMBER=str(member.id))


def keys(d):
    return set(d.keys())


class ShapeTests(ApiTestBase):
    def test_group_shape(self):
        data = self.get(f"{BASE}/groups/{self.group.id}", self.maya).json()
        self.assertEqual(keys(data), keys(FIXTURES["group_detail"]))
        self.assertEqual(keys(data["members"][0]), keys(FIXTURES["group_detail"]["members"][0]))
        self.assertEqual(len(data["members"]), 6)
        self.assertEqual(data["organizer_id"], str(self.maya.id))

    def test_purchase_shape_and_counts(self):
        approve("Maya", self.parts)
        approve("Priya", self.parts)
        data = self.get(f"{BASE}/purchases/{self.purchase.id}", self.maya).json()
        fx = FIXTURES["purchase_detail"]
        self.assertEqual(keys(data), keys(fx))
        self.assertEqual(keys(data["shares"][0]), keys(fx["shares"][0]))
        self.assertEqual(keys(data["counts"]), keys(fx["counts"]))
        self.assertEqual(data["held_cents"], 2 * SHARE_CENTS)
        self.assertEqual(data["counts"], {"held": 2, "viewing": 4, "invited": 0, "out": 0, "active": 6})
        self.assertTrue(data["simulated"])
        self.assertTrue(data["deadline_at"].endswith("Z"))

    def test_preferences_shape(self):
        data = self.get(f"{BASE}/groups/{self.group.id}/members/me/preferences", self.maya).json()
        self.assertEqual(keys(data), keys(FIXTURES["my_preferences"]))
        self.assertEqual(data["budget_cap_cents"], 25000)             # Maya's own seeded cap

    def test_ballot_shape_change_and_errors(self):
        vote = Vote.objects.create(
            group=self.group, purpose="pick_option", closes_at=timezone.now() + timedelta(hours=1), quorum=4,
            choices=[{"key": "1", "label": "Pinecrest"}, {"key": "2", "label": "Lakeview"}, {"key": "3", "label": "Cedar"}])
        url = f"{BASE}/votes/{vote.id}/ballots"
        data = self.send("post", url, self.maya, {"choice_key": "1"}).json()
        self.assertEqual(keys(data), keys(FIXTURES["ballot"]))
        self.assertEqual(data["tally"], {"1": 1, "2": 0, "3": 0})
        self.assertEqual(data["your_choice"], "1")
        self.send("post", url, self.priya, {"choice_key": "1"})
        changed = self.send("post", url, self.maya, {"choice_key": "2"}).json()      # changing your vote is allowed
        self.assertEqual(changed["tally"], {"1": 1, "2": 1, "3": 0})
        self.assertEqual(changed["voted"], 2)
        bad = self.send("post", url, self.maya, {"choice_key": "9"})
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]), (400, "invalid_choice"))
        vote.status = "closed"
        vote.save()
        closed = self.send("post", url, self.maya, {"choice_key": "1"})
        self.assertEqual((closed.status_code, closed.json()["error"]["code"]), (409, "vote_closed"))

    def test_ledger_shape(self):
        sam = Member.objects.get(display_name="Sam")
        expense = Expense.objects.create(group=self.group, payer=self.priya, amount_cents=6000, description="Groceries")
        for m in (self.maya, sam, Member.objects.get(display_name="Leo"), Member.objects.get(display_name="Alex"),
                  self.priya):
            ExpenseSplit.objects.create(expense=expense, member=m, amount_cents=1200)
        data = self.get(f"{BASE}/groups/{self.group.id}/ledger", self.maya).json()
        fx = FIXTURES["ledger"]
        self.assertEqual(keys(data), keys(fx))
        self.assertEqual(keys(data["expenses"][0]), keys(fx["expenses"][0]))
        self.assertEqual(keys(data["balances"][0]), keys(fx["balances"][0]))
        self.assertEqual(data["expenses"][0]["split_count"], 5)
        balances = {b["display_name"]: b["net_cents"] for b in data["balances"]}
        self.assertEqual(balances["Priya"], 4800)                 # paid 6000, owes 1200
        self.assertEqual(balances["Maya"], -1200)
        self.assertEqual(data["net_total_cents"], 0)


class PrivacyAndAuthTests(ApiTestBase):
    def test_budgets_never_serialized(self):
        """Every endpoint except the caller's own preferences must be free of budget numbers."""
        approve("Maya", self.parts)
        urls = [f"{BASE}/groups/{self.group.id}", f"{BASE}/purchases/{self.purchase.id}",
                f"{BASE}/groups/{self.group.id}/ledger", f"{BASE}/groups/{self.group.id}/events"]
        caps = [str(c) for c in (25000, 22000, 19000, 23000, 20000, 21000)]
        for url in urls:
            body = self.get(url, self.priya).content.decode()
            self.assertNotIn("budget", body.lower(), url)
            for cap in caps:
                self.assertNotIn(f":{cap}", body.replace(" ", ""), url)
        # preferences show only the caller's own cap
        self.assertEqual(self.get(f"{BASE}/groups/{self.group.id}/members/me/preferences", self.priya).json()
                         ["budget_cap_cents"], 22000)

    def test_preferences_update_logs_names_not_values(self):
        url = f"{BASE}/groups/{self.group.id}/members/me/preferences"
        data = self.send("put", url, self.priya, {"budget_cap_cents": 21500, "must_haves": ["kitchen"]}).json()
        self.assertEqual(data["budget_cap_cents"], 21500)
        self.assertEqual(data["must_haves"], ["kitchen"])
        event = EventLog.objects.filter(type="prefs.updated").latest("seq")
        self.assertEqual(event.payload["fields"], ["budget_cap_cents", "must_haves"])
        self.assertNotIn("21500", str(event.payload))
        # nobody else's cap changed
        self.assertEqual(self.get(url, self.maya).json()["budget_cap_cents"], 25000)

    def test_preferences_validation(self):
        url = f"{BASE}/groups/{self.group.id}/members/me/preferences"
        for bad in ({"budget_cap_cents": -5}, {"budget_cap_cents": "lots"}, {"budget_cap_cents": True},
                    {"must_haves": "hot_tub"}, {"flexibility": "whatever"}):
            r = self.send("put", url, self.priya, bad)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_request"), bad)

    def test_no_header_is_401(self):
        r = self.get(f"{BASE}/groups/{self.group.id}")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "unauthenticated"))

    def test_outsider_is_403_and_logged(self):
        other = Group.objects.create(name="Other group")
        outsider = Member.objects.create(group=other, display_name="Eve", phone_e164_hash="x" * 64)
        for url in (f"{BASE}/groups/{self.group.id}", f"{BASE}/purchases/{self.purchase.id}",
                    f"{BASE}/groups/{self.group.id}/ledger", f"{BASE}/groups/{self.group.id}/events"):
            r = self.get(url, outsider)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_a_member"), url)
        self.assertTrue(EventLog.objects.filter(type="auth.denied").exists())

    def test_left_member_is_403(self):
        self.priya.status = Member.Status.LEFT
        self.priya.save()
        self.assertEqual(self.get(f"{BASE}/groups/{self.group.id}", self.priya).status_code, 403)

    @override_settings(DEMO_MODE=False)
    def test_header_ignored_when_demo_mode_is_off(self):
        self.assertEqual(self.get(f"{BASE}/groups/{self.group.id}", self.maya).status_code, 401)

    def test_unknown_purchase_is_404_with_envelope(self):
        r = self.get(f"{BASE}/purchases/00000000-0000-4000-8000-000000000000", self.maya)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        self.assertIn("request_id", r.json()["error"])


class LeaveTests(ApiTestBase):
    def test_leave_releases_my_hold(self):
        approve("Priya", self.parts)
        share = self.parts["Priya"][0]
        r = self.send("post", f"{BASE}/shares/{share.id}/leave", self.priya)
        self.assertEqual((r.status_code, r.json()), (200, {"share_status": "released"}))
        self.assertEqual(self.gateway.holds[next(iter(self.gateway.holds))]["status"], "RELEASED")

    def test_cannot_leave_someone_elses_share(self):
        share = self.parts["Priya"][0]
        r = self.send("post", f"{BASE}/shares/{share.id}/leave", self.maya)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_your_share"))

    def test_leaving_twice_is_409(self):
        share = self.parts["Priya"][0]
        self.send("post", f"{BASE}/shares/{share.id}/leave", self.priya)
        r = self.send("post", f"{BASE}/shares/{share.id}/leave", self.priya)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "illegal_state"))


class EventsTests(ApiTestBase):
    def test_events_shape_summary_and_group(self):
        approve("Maya", self.parts)
        url = f"{BASE}/groups/{self.group.id}/events"
        data = self.get(url, self.maya).json()
        self.assertEqual(keys(data), keys(FIXTURES["events"]))
        self.assertEqual(keys(data["events"][0]), keys(FIXTURES["events"]["events"][0]))
        by_type = {e["type"]: e for e in data["events"]}
        authorized = by_type["gateway.hold_authorized"]               # the gateway's events show up under the group
        self.assertTrue(authorized["simulated"])
        self.assertEqual(authorized["summary"], "Hold authorized: Maya $175.00")
        self.assertIn("share.state_changed", by_type)
        self.assertTrue(data["chain_ok"])

    def test_after_seq_returns_only_newer_events(self):
        approve("Maya", self.parts)
        url = f"{BASE}/groups/{self.group.id}/events"
        everything = self.get(url, self.maya).json()["events"]
        newer = self.get(f"{url}?after_seq={everything[-2]['seq']}", self.maya).json()["events"]
        self.assertEqual([e["seq"] for e in newer], [everything[-1]["seq"]])
        self.assertEqual(self.get(f"{url}?after_seq=abc", self.maya).status_code, 400)

    def test_other_groups_events_are_not_shown(self):
        other = Group.objects.create(name="Other group")
        from core.events import log_event
        log_event("group.created", actor="test", group_id=other.id, payload={"secret": "nope"})
        approve("Maya", self.parts)
        body = self.get(f"{BASE}/groups/{self.group.id}/events", self.maya).content.decode()
        self.assertNotIn("nope", body)

    def test_chain_ok_true_then_false_after_tampering(self):
        approve("Maya", self.parts)
        approve("Priya", self.parts)
        url = f"{BASE}/groups/{self.group.id}/events"
        self.assertTrue(self.get(url, self.maya).json()["chain_ok"])
        self.assertTrue(verify_chain())
        # someone edits a stored event (quietly changes an amount)
        target = EventLog.objects.filter(type="gateway.hold_authorized").first()
        target.payload = {**target.payload, "amount": {"amount_cents": 1, "currency": "USD"}}
        target.save(update_fields=["payload"])
        self.assertFalse(self.get(url, self.maya).json()["chain_ok"])

    def test_chain_breaks_if_an_event_is_deleted(self):
        approve("Maya", self.parts)
        EventLog.objects.filter(type="gateway.mandate_created").delete()
        self.assertFalse(verify_chain())
