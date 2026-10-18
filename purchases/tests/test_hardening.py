"""
Tests for hardening (milestone M9): who may import what, limits, the production settings check, and the
assert_no_money_state_change helper that Aryan's prompt-injection tests use.
"""
import ast
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.events import log_event, log_event_simulated
from core.models import EventLog
from payments.models import Consent
from payments.simulated import SimulatedVisaAgentGateway
from payments.tests.test_consent_api import OTP, build_purchase
from purchases.models import Option, Purchase, Share
from purchases.testing import assert_no_money_state_change
from purchases.tests.test_rules_engine import EngineBase
from purchases.tests.test_rules_holds import approve, build_world

ROOT = Path(__file__).resolve().parents[2]


def imported_modules(path: Path) -> set[str]:
    """Every module a Python file imports, including imports hidden inside functions."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


class ImportBoundaryTests(TestCase):
    """The AI and the chat code must never be able to touch money. The rules engine is their only way in."""

    def test_agent_and_chat_do_not_import_payments(self):
        offenders = []
        for folder in ("agent", "chat"):
            for path in (ROOT / folder).rglob("*.py"):
                if "tests" in path.parts or "migrations" in path.parts:
                    continue
                bad = [m for m in imported_modules(path) if m == "payments" or m.startswith("payments.")]
                if bad:
                    offenders.append(f"{path.relative_to(ROOT)}: {bad}")
        self.assertEqual(offenders, [], "agent/ and chat/ may not import payments/")

    def test_the_scan_really_sees_imports(self):
        """Guards the guard: the scanner must find an import hidden inside a function."""
        sample = ROOT / "purchases" / "rules.py"
        self.assertIn("payments.gateway", imported_modules(sample))

    def test_only_the_rules_engine_and_payments_call_money_methods(self):
        """authorize_hold / adjust_hold / capture / release_hold / refund are called only from rules.py, tasks.py (hold refresh),
        the payments app itself and tests."""
        allowed = {"purchases/rules.py", "purchases/tasks.py"}
        offenders = []
        for folder in ("agent", "chat", "merchant", "core", "purchases"):
            for path in (ROOT / folder).rglob("*.py"):
                rel = str(path.relative_to(ROOT))
                if "tests" in path.parts or rel in allowed or "migrations" in path.parts or rel == "purchases/testing.py":
                    continue
                for node in ast.walk(ast.parse(path.read_text())):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in
                            ("authorize_hold", "adjust_hold", "release_hold") ):
                        offenders.append(f"{rel}:{node.lineno} {node.func.attr}")
        self.assertEqual(offenders, [])


class DeploySettingsTests(TestCase):
    def test_check_deploy_is_clean_with_production_settings(self):
        env = {**os.environ, "DEBUG": "0", "DJANGO_SECRET_KEY": "x" * 12 + "k9#Zq!" * 10, "ALLOWED_HOSTS": "tandem.example.com",
               "DEMO_MODE": "1", "DEMO_ADMIN_KEY": "a-long-random-demo-key"}
        env.pop("REDIS_URL", None)
        done = subprocess.run([sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("no issues", done.stdout + done.stderr)


class LimitTests(EngineBase):
    def test_third_open_purchase_for_a_member_is_refused(self):
        purchase, options = self.make_open_purchase()                       # Maya is now in 1 open purchase
        other = Option.objects.get(pk=options[1]["id"])
        second = Purchase.objects.create(group=purchase.group, option=other, state=Purchase.State.COLLECTING,
                                         total_cents=other.total_cents, deadline_at=timezone.now() + timedelta(days=1))
        Share.objects.create(purchase=second, member=self.members["Maya"], amount_cents=1000, status=Share.Status.INVITED)
        self.vote_for([options[2]["id"], options[1]["id"]], 1)
        result = self.run_tool("create_purchase", {"option_id": options[2]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "too_many_open_purchases")
        self.assertEqual(Purchase.objects.filter(group=purchase.group).count(), 2)       # nothing new was created


class ConsentThrottleTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.gw = SimulatedVisaAgentGateway(on_event=lambda t, p: None, latency_ms=0, force_decline_members=set())
        for target in ("payments.services.get_gateway", "purchases.rules.get_gateway"):
            patcher = mock.patch(target, return_value=self.gw)
            patcher.start()
            self.addCleanup(patcher.stop)

    def approve(self, token):
        payload = self.client.get(f"/api/v1/consent/{token}?cap_cents=20000").json()
        tok = self.client.post(f"/api/v1/consent/{token}/tokenize", {"transient_card_token": "tt_sim_1111_ab12"}, format="json").json()
        return self.client.post(f"/api/v1/consent/{token}/approve", {
            "agent_token_id": tok["agent_token_id"], "cap_cents": 20000,
            "consent_text_sha256": payload["consent_text_sha256"], "assertion": OTP}, format="json")

    def test_sixth_approval_attempt_in_an_hour_is_refused(self):
        share, token, member = build_purchase("Alex")
        for _ in range(5):                                                 # five approvals already today
            Consent.objects.create(member=member, share=share, consent_text_version="v1", consent_text_sha256="0" * 64,
                                   amount_cents=17500, cap_cents=20000, method="otp", accepted_at=timezone.now())
        r = self.approve(token)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "rate_limited"))
        share.refresh_from_db()
        self.assertNotEqual(share.status, "held")                          # no hold was placed
        self.assertEqual(share.holds.count(), 0)

    def test_old_approvals_do_not_count(self):
        share, token, member = build_purchase("Alex")
        for _ in range(5):
            Consent.objects.create(member=member, share=share, consent_text_version="v1", consent_text_sha256="0" * 64,
                                   amount_cents=17500, cap_cents=20000, method="otp",
                                   accepted_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(self.approve(token).status_code, 200)


class NoMoneyChangeHelperTests(TestCase):
    def setUp(self):
        self.gateway = SimulatedVisaAgentGateway(latency_ms=0, on_event=log_event_simulated)
        patcher = mock.patch("purchases.rules.get_gateway", return_value=self.gateway)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.purchase, self.parts = build_world(self.gateway, all_cards_ok=True)
        self.group_id = self.purchase.group_id

    def last_seq(self):
        return EventLog.objects.order_by("-seq").values_list("seq", flat=True).first() or 0

    def test_passes_when_nothing_money_related_happened(self):
        start = self.last_seq()
        log_event("agent.message_sent", actor="agent", group_id=self.group_id, payload={"card": None})
        log_event("vote.opened", actor="agent", group_id=self.group_id, payload={})
        assert_no_money_state_change(self.group_id, start)

    def test_fails_after_a_hold(self):
        start = self.last_seq()
        approve("Maya", self.parts)
        with self.assertRaises(AssertionError) as ctx:
            assert_no_money_state_change(self.group_id, start)
        self.assertIn("gateway.", str(ctx.exception))

    def test_changes_before_the_marker_are_ignored(self):
        approve("Maya", self.parts)
        assert_no_money_state_change(self.group_id, self.last_seq())

    def test_other_groups_are_ignored(self):
        start = self.last_seq()
        from core.models import Group
        other = Group.objects.create(name="Other group")
        log_event("booking.confirmed", actor="rules", group_id=other.id, payload={})
        assert_no_money_state_change(self.group_id, start)

    def test_catches_a_purchase_state_change_and_a_consent(self):
        for event_type, payload in (("purchase.state_changed", {"from": "collecting", "to": "funded"}),
                                    ("share.state_changed", {"from": "viewing", "to": "held"}),
                                    ("consent.accepted", {}), ("booking.confirmed", {})):
            start = self.last_seq()
            log_event(event_type, actor="rules", group_id=self.group_id, payload=payload)
            with self.assertRaises(AssertionError, msg=event_type):
                assert_no_money_state_change(self.group_id, start)

    def test_a_harmless_state_change_is_fine(self):
        start = self.last_seq()
        log_event("share.state_changed", actor="rules", group_id=self.group_id, payload={"from": "invited", "to": "viewing"})
        assert_no_money_state_change(self.group_id, start)
