"""
Tests for expenses and the ledger (milestone M5).

The group is the six seeded demo members. Money is always whole cents.
"""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from chat.models import ChatMessage
from core.models import EventLog, Member
from purchases import ledger, rules
from purchases.models import Expense, ExpenseSplit

BASE = "/api/v1"


class SplitMathTests(TestCase):
    """The pure functions: no database needed."""

    def test_splits_sum_to_total(self):
        self.assertEqual(ledger.split_by_weights(6000, [1] * 5), [1200] * 5)                     # $60 / 5 = $12.00
        eight = ledger.split_by_weights(54900, [1] * 8)                                           # $549 / 8
        self.assertEqual(eight, [6863] * 4 + [6862] * 4)                                          # 4 x $68.63 + 4 x $68.62
        self.assertEqual(sum(eight), 54900)

    def test_every_split_adds_up_exactly(self):
        for total in (1, 2, 99, 100, 101, 54900, 123457):
            for people in range(1, 9):
                parts = ledger.split_by_weights(total, [1] * people)
                self.assertEqual(sum(parts), total)
                self.assertLessEqual(max(parts) - min(parts), 1)          # nobody pays more than a cent extra

    def test_weighted_split(self):
        self.assertEqual(ledger.split_by_weights(1000, [2, 1, 1]), [500, 250, 250])
        self.assertEqual(ledger.split_by_weights(1001, [2, 1, 1]), [501, 250, 250])               # the leftover cent goes first

    def test_settle_up_pays_everyone_back(self):
        balances = [{"member_id": "a", "net_cents": 4800}, {"member_id": "b", "net_cents": 0},
                    {"member_id": "c", "net_cents": -1200}, {"member_id": "d", "net_cents": -1200},
                    {"member_id": "e", "net_cents": -1200}, {"member_id": "f", "net_cents": -1200}]
        payments = ledger.settle_up(balances)
        self.assertEqual(sorted((p["from"], p["to"], p["amount_cents"]) for p in payments),
                         [("c", "a", 1200), ("d", "a", 1200), ("e", "a", 1200), ("f", "a", 1200)])

    def test_settle_up_never_needs_more_than_people_minus_one_payments(self):
        balances = [{"member_id": str(i), "net_cents": n} for i, n in enumerate([700, 500, -300, -400, -500])]
        payments = ledger.settle_up(balances)
        self.assertLessEqual(len(payments), 4)
        net = {b["member_id"]: b["net_cents"] for b in balances}
        for p in payments:
            net[p["from"]] += p["amount_cents"]
            net[p["to"]] -= p["amount_cents"]
        self.assertEqual(set(net.values()), {0})                          # after paying, everyone is square


class LedgerBase(TestCase):
    def setUp(self):
        call_command("seed_demo", stdout=StringIO())
        self.members = {m.display_name: m for m in Member.objects.order_by("created_at", "display_name")}
        self.order = list(self.members)                                   # stable order, oldest first
        self.group = next(iter(self.members.values())).group
        self.maya, self.priya = self.members["Maya"], self.members["Priya"]
        self.client = APIClient()

    def post(self, member, body):
        return self.client.post(f"{BASE}/groups/{self.group.id}/expenses", body, format="json",
                                HTTP_X_TANDEM_MEMBER=str(member.id))

    def ledger(self, member=None):
        return self.client.get(f"{BASE}/groups/{self.group.id}/ledger", HTTP_X_TANDEM_MEMBER=str((member or self.maya).id)).json()

    def ids(self, *names):
        return [str(self.members[n].id) for n in names]


class AddExpenseTests(LedgerBase):
    def test_equal_split_between_five(self):
        five = [n for n in self.order if n != "Priya"][:5]
        body = {"payer_id": str(self.priya.id), "amount_cents": 6000, "description": "Groceries",
                "split": {"mode": "equal", "member_ids": self.ids(*five)}}
        response = self.post(self.maya, body)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual([s["amount_cents"] for s in data["splits"]], [1200] * 5)
        self.assertEqual(data["balances_net_cents"], 0)
        self.assertEqual(Expense.objects.get().split_mode, "equal")
        self.assertEqual(ExpenseSplit.objects.count(), 5)

    def test_remainder_cents_go_to_the_first_members_in_stable_order(self):
        body = {"payer_id": str(self.maya.id), "amount_cents": 1000, "description": "Snacks",
                "split": {"mode": "equal", "member_ids": list(reversed(self.ids(*self.order)))}}   # sent in reverse order
        splits = self.post(self.maya, body).json()["splits"]
        by_name = {s["member_id"]: s["amount_cents"] for s in splits}
        self.assertEqual([by_name[str(self.members[n].id)] for n in self.order], [167, 167, 167, 167, 166, 166])

    def test_no_member_list_means_everyone(self):
        response = self.post(self.maya, {"payer_id": str(self.maya.id), "amount_cents": 6000, "description": "Gas",
                                         "split": {"mode": "equal"}})
        self.assertEqual(len(response.json()["splits"]), 6)

    def test_shares_split_counts_each_mention_as_one_share(self):
        ids = self.ids("Maya", "Maya", "Priya")                           # Maya has 2 shares, Priya 1
        response = self.post(self.maya, {"payer_id": str(self.maya.id), "amount_cents": 3000, "description": "Room",
                                         "split": {"mode": "shares", "member_ids": ids}})
        amounts = {s["member_id"]: s["amount_cents"] for s in response.json()["splits"]}
        self.assertEqual(amounts, {str(self.maya.id): 2000, str(self.priya.id): 1000})

    def test_custom_split(self):
        custom = {str(self.maya.id): 2500, str(self.priya.id): 500}
        response = self.post(self.maya, {"payer_id": str(self.priya.id), "amount_cents": 3000, "description": "Dinner",
                                         "split": {"mode": "custom", "custom_amounts": custom}})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.ledger()["balances"][0]["display_name"], "Priya")      # Priya paid $30 and owes $5

    def test_custom_amounts_must_add_up(self):
        custom = {str(self.maya.id): 2500, str(self.priya.id): 400}
        response = self.post(self.maya, {"payer_id": str(self.priya.id), "amount_cents": 3000, "description": "Dinner",
                                         "split": {"mode": "custom", "custom_amounts": custom}})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "split_mismatch"))
        self.assertEqual(Expense.objects.count(), 0)                                  # nothing half-saved

    def test_bad_requests_are_refused(self):
        good = {"payer_id": str(self.maya.id), "amount_cents": 1000, "description": "x", "split": {"mode": "equal"}}
        cases = [
            ({**good, "amount_cents": 0}, "invalid_request"),
            ({**good, "amount_cents": -5}, "invalid_request"),
            ({**good, "amount_cents": 500_001}, "invalid_request"),
            ({**good, "amount_cents": 10.5}, "invalid_request"),
            ({**good, "amount_cents": True}, "invalid_request"),
            ({**good, "description": "y" * 81}, "invalid_request"),
            ({**good, "split": {"mode": "weird"}}, "invalid_request"),
            ({**good, "payer_id": "not-a-member"}, "not_a_member"),
            ({**good, "split": {"mode": "equal", "member_ids": ["nope"]}}, "not_a_member"),
        ]
        for body, code in cases:
            response = self.post(self.maya, body)
            self.assertEqual(response.json()["error"]["code"], code, body)
        self.assertEqual(Expense.objects.count(), 0)

    def test_people_who_left_cannot_be_in_a_split(self):
        leaver = list(self.members.values())[-1]
        leaver.status = Member.Status.LEFT
        leaver.save()
        response = self.post(self.maya, {"payer_id": str(self.maya.id), "amount_cents": 1000, "description": "x",
                                         "split": {"mode": "equal", "member_ids": [str(leaver.id)]}})
        self.assertEqual(response.json()["error"]["code"], "not_a_member")

    def test_outsiders_are_blocked(self):
        response = self.client.post(f"{BASE}/groups/{self.group.id}/expenses", {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_event_and_chat_card(self):
        self.post(self.maya, {"payer_id": str(self.priya.id), "amount_cents": 6000, "description": "Groceries",
                              "split": {"mode": "equal"}})
        event = EventLog.objects.get(type="expense.added")
        self.assertEqual((event.payload["amount_cents"], event.payload["split_count"]), (6000, 6))
        card = ChatMessage.objects.get(card__type="ExpenseCard").card["data"]
        self.assertEqual((card["payer"], card["amount_cents"], card["per_person_cents"]), ("Priya", 6000, 1000))


class LedgerViewTests(LedgerBase):
    def test_balances_net_to_zero_and_settle_up(self):
        self.post(self.maya, {"payer_id": str(self.priya.id), "amount_cents": 6000, "description": "Groceries",
                              "split": {"mode": "equal"}})
        self.post(self.maya, {"payer_id": str(self.maya.id), "amount_cents": 3000, "description": "Gas",
                              "split": {"mode": "equal"}})
        data = self.ledger()
        self.assertEqual(data["net_total_cents"], 0)
        self.assertEqual(sum(b["net_cents"] for b in data["balances"]), 0)
        nets = {b["display_name"]: b["net_cents"] for b in data["balances"]}
        self.assertEqual((nets["Priya"], nets["Maya"]), (4500, 1500))      # paid $60 owes $15, paid $30 owes $15
        self.assertEqual(data["balances"][0]["display_name"], "Priya")      # biggest creditor first
        self.assertEqual(sum(p["amount_cents"] for p in data["settle_up"]), 6000)     # the four others owe $15 each
        self.assertEqual({p["to"] for p in data["settle_up"]}, {"Priya", "Maya"})
        self.assertEqual([e["description"] for e in data["expenses"]], ["Groceries", "Gas"])

    def test_empty_ledger(self):
        data = self.ledger()
        self.assertEqual((data["expenses"], data["net_total_cents"], data["settle_up"]), ([], 0, []))


class AgentToolTests(LedgerBase):
    def run_tool(self, args):
        return rules.RulesEngine().execute("add_expense", args, f"member:{self.maya.id}")

    def test_add_expense_tool_uses_dollars(self):
        result = self.run_tool({"amount": 549, "payer_id": str(self.maya.id), "description": "Cabin food",
                                "split": {"mode": "equal", "member_ids": self.ids(*self.order)}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["amount_cents"], 54900)
        self.assertEqual((result["data"]["split_count"], result["data"]["per_person_cents"]), (6, 9150))
        self.assertEqual(Expense.objects.get().amount_cents, 54900)

    def test_tool_errors_use_the_standard_envelope(self):
        result = self.run_tool({"amount": 60, "payer_id": "nobody", "split": {"mode": "equal"}})
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "not_a_member"))
        result = self.run_tool({"amount": 30, "payer_id": str(self.maya.id),
                                "split": {"mode": "custom", "custom_amounts": {str(self.maya.id): 10}}})
        self.assertEqual(result["error"]["code"], "split_mismatch")

    def test_tool_custom_amounts_in_dollars(self):
        custom = {str(self.maya.id): 20.25, str(self.priya.id): 9.75}
        result = self.run_tool({"amount": 30, "payer_id": str(self.maya.id), "split": {"mode": "custom", "custom_amounts": custom}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(sorted(ExpenseSplit.objects.values_list("amount_cents", flat=True)), [975, 2025])
