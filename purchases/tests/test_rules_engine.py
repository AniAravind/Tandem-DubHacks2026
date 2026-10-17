"""
Tests for the consensus engine and RulesEngine.execute (milestone M3).

The world is the seeded Tahoe demo: six people, four PineStay rentals. Maya is the organizer.
"""
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import EventLog, Group, Member
from purchases import consensus, rules
from purchases.models import Ballot, Option, Purchase, Share, Vote

PRIVATE_CAPS = ("25000", "22000", "23000", "20000", "21000")     # every seeded cap except the lowest (19000)


class EngineBase(TestCase):
    def setUp(self):
        call_command("seed_demo", stdout=StringIO())
        self.engine = rules.RulesEngine()
        self.members = {m.display_name: m for m in Member.objects.all()}
        self.maya = self.members["Maya"]
        self.group = self.maya.group
        self.actor = f"member:{self.maya.id}"

    def run_tool(self, tool, args, actor=None):
        return self.engine.execute(tool, args, actor or self.actor)

    def search(self):
        result = self.run_tool("search_options", {"category": "rental", "group_size": 6})
        self.assertTrue(result["ok"], result)
        return result["data"]["options"]

    def vote_for(self, option_ids, winner_index, voters=6):
        """Open a vote and have `voters` people vote for choice number `winner_index`. Returns the Vote."""
        data = self.run_tool("start_vote", {"option_ids": option_ids})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        for member in list(self.members.values())[:voters]:
            Ballot.objects.create(vote=vote, member=member, choice_key=str(winner_index))
        return consensus.close_if_decided(vote)

    def make_open_purchase(self):
        options = self.search()
        self.vote_for([options[0]["id"], options[1]["id"]], 1)
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertTrue(result["ok"], result)
        return Purchase.objects.get(pk=result["data"]["purchase_id"]), options


class ConsensusTests(EngineBase):
    def test_consensus_seed_ordering(self):
        options = self.search()
        self.assertEqual([o["title"] for o in options], ["Pinecrest A-Frame", "Lakeview Bungalow", "Cedar Hollow"])
        self.assertNotIn("Juniper Loft", [o["title"] for o in options])       # sleeps 5, the group is 6
        scores = [o["score"] for o in options]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([o["rank"] for o in options], [1, 2, 3])

    def test_best_option_facts(self):
        best = self.search()[0]
        self.assertEqual(best["per_person_cents"], 17500)
        facts = best["explanation_facts"]
        self.assertIn({"kind": "fits_all_budgets", "per_person_cents": 17500, "lowest_cap_cents": 19000}, facts)
        self.assertIn({"kind": "must_have_count", "feature": "hot_tub", "count": 4}, facts)        # Maya, Priya, Leo, Alex
        self.assertIn({"kind": "drive_min", "to": "Heavenly", "minutes": 12}, facts)

    def test_unmet_must_have_becomes_a_tradeoff(self):
        lakeview = next(o for o in self.search() if o["title"] == "Lakeview Bungalow")
        tradeoff = next(f for f in lakeview["explanation_facts"] if f["kind"] == "tradeoff")
        self.assertEqual(tradeoff["text"], "No hot tub (wanted by 4)")

    def test_ranking_is_deterministic(self):
        first = [(o["title"], o["score"]) for o in self.search()]
        second = [(o["title"], o["score"]) for o in self.search()]
        self.assertEqual(first, second)

    def test_budgets_never_in_results(self):
        text = json.dumps(self.search())
        for cap in PRIVATE_CAPS:
            self.assertNotIn(cap, text)
        self.assertNotIn("budget_cap", text.lower())

    def test_dealbreaker_excludes_an_option(self):
        Member.objects.filter(display_name="Sam").update(prefs={"must_haves": [], "dealbreakers": ["parking"]})
        self.assertNotIn("Pinecrest A-Frame", [o["title"] for o in self.search()])

    def test_strict_budget_filter_then_lenient_fallback(self):
        self.members["Sam"].budget_cap_cents = 15000          # lowest cap is now below every option
        self.members["Sam"].save()
        options = self.search()
        self.assertTrue(options)                              # lenient mode still returns options
        self.assertEqual(options[0]["explanation_facts"][0], {"kind": "over_budget_count", "count": 1})
        self.assertNotIn("Sam", json.dumps(options))          # a count, never a name

    def test_budget_per_person_max_filters(self):
        result = self.run_tool("search_options", {"category": "rental", "group_size": 6, "budget_per_person_max": 170})
        self.assertEqual([o["title"] for o in result["data"]["options"]], ["Cedar Hollow"])   # only $162 fits

    def test_group_size_must_match(self):
        result = self.run_tool("search_options", {"category": "rental", "group_size": 5})
        self.assertEqual(result["error"]["code"], "group_size_mismatch")


class ValidationTests(EngineBase):
    def test_schema_rejections(self):
        bad_calls = [
            ("search_options", {"category": "boats", "group_size": 6}),          # not in the allowed list
            ("search_options", {"category": "rental"}),                           # group_size missing
            ("search_options", {"category": "rental", "group_size": 1}),          # below the minimum
            ("search_options", {"category": "rental", "group_size": 6, "price": 5}),   # field not allowed
            ("search_options", {"category": "rental", "group_size": True}),       # a bool is not an integer
            ("start_vote", {"option_ids": ["only-one"]}),
            ("create_purchase", {"option_id": "x", "member_ids": ["a"], "deadline_hours": 99}),
            ("add_expense", {"amount": 0, "payer_id": "x", "split": {"mode": "equal"}}),
        ]
        for tool, args in bad_calls:
            result = self.run_tool(tool, args)
            self.assertEqual((result["ok"], result["error"]["code"]), (False, "invalid_arguments"), (tool, args))

    def test_unknown_tool(self):
        self.assertEqual(self.run_tool("move_money", {})["error"]["code"], "unknown_tool")

    def test_unauthorized_actors(self):
        for actor in ("nobody", "member:not-a-uuid", "member:00000000-0000-4000-8000-000000000000", "agent:" + str(self.maya.id)):
            result = self.run_tool("check_status", {}, actor)
            self.assertEqual(result["error"]["code"], "unauthorized", actor)
        Member.objects.filter(pk=self.maya.pk).update(status="left")
        self.assertEqual(self.run_tool("check_status", {})["error"]["code"], "unauthorized")

    def test_tool_log_has_names_not_values(self):
        self.run_tool("search_options", {"category": "rental", "group_size": 6, "budget_per_person_max": 190})
        event = EventLog.objects.filter(type="agent.tool_called").latest("seq")
        self.assertEqual(event.payload, {"tool": "search_options", "arg_names": ["budget_per_person_max", "category", "group_size"]})

    def test_rate_limit(self):
        for _ in range(rules.MAX_CALLS_PER_MINUTE):
            self.run_tool("check_status", {})
        self.assertEqual(self.run_tool("check_status", {})["error"]["code"], "rate_limited")

    def test_unbuilt_tools_say_not_implemented(self):
        purchase, _ = self.make_open_purchase()
        result = self.run_tool("propose_fallback", {"purchase_id": str(purchase.id), "reason": "dropout"})
        self.assertEqual(result["error"]["code"], "not_implemented")
        result = self.run_tool("add_expense", {"amount": 60, "payer_id": str(self.maya.id), "split": {"mode": "equal"}})
        self.assertEqual(result["error"]["code"], "not_implemented")


class VoteTests(EngineBase):
    def test_start_vote(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"], options[2]["id"]]})["data"]
        self.assertEqual(data["quorum"], 4)                   # majority of 6
        self.assertEqual([c["label"] for c in data["choices"]], ["Pinecrest A-Frame", "Lakeview Bungalow", "Cedar Hollow"])
        self.assertTrue(data["closes_at"].endswith("Z"))

    def test_vote_cannot_use_another_groups_option(self):
        other = Group.objects.create(name="Other")
        stranger = Option.objects.create(group=other, merchant_slug="pinestay", listing_ref="x", category="rental",
                                         title="Not yours", total_cents=1, capacity=6)
        options = self.search()
        result = self.run_tool("start_vote", {"option_ids": [options[0]["id"], str(stranger.id)]})
        self.assertEqual(result["error"]["code"], "not_found")

    def test_vote_closes_when_everyone_has_voted(self):
        options = self.search()
        vote = self.vote_for([options[0]["id"], options[1]["id"]], 2)
        self.assertEqual((vote.status, vote.winner_key), ("closed", "2"))

    def test_vote_stays_open_without_everyone(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        Ballot.objects.create(vote=vote, member=self.maya, choice_key="1")
        self.assertEqual(consensus.close_if_decided(vote).status, "open")

    def test_deadline_closes_a_vote_with_quorum(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        for name in ("Maya", "Priya", "Sam", "Leo"):
            Ballot.objects.create(vote=vote, member=self.members[name], choice_key="1")
        Vote.objects.filter(pk=vote.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
        vote.refresh_from_db()
        self.assertEqual(consensus.close_if_decided(vote).winner_key, "1")

    def test_deadline_without_quorum_has_no_winner(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        Ballot.objects.create(vote=vote, member=self.maya, choice_key="1")
        Vote.objects.filter(pk=vote.pk).update(closes_at=timezone.now() - timedelta(seconds=1))
        vote.refresh_from_db()
        closed = consensus.close_if_decided(vote)
        self.assertEqual((closed.status, closed.winner_key), ("closed", ""))

    def test_tie_has_no_winner(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        for i, member in enumerate(self.members.values()):
            Ballot.objects.create(vote=vote, member=member, choice_key="1" if i < 3 else "2")
        closed = consensus.close_if_decided(vote)
        self.assertEqual((closed.status, closed.winner_key), ("closed", ""))

    def test_last_ballot_through_the_api_closes_the_vote(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        client = APIClient()
        for member in self.members.values():
            r = client.post(f"/api/v1/votes/{data['vote_id']}/ballots", {"choice_key": "1"}, format="json",
                            HTTP_X_TANDEM_MEMBER=str(member.id))
        self.assertEqual(r.json()["status"], "closed")
        self.assertEqual(r.json()["tally"], {"1": 6, "2": 0})


class CreatePurchaseTests(EngineBase):
    def test_create_purchase_without_vote_is_rejected(self):
        options = self.search()
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "vote_required")
        self.assertEqual(Purchase.objects.count(), 0)

    def test_losing_option_cannot_be_bought(self):
        options = self.search()
        self.vote_for([options[0]["id"], options[1]["id"]], 1)                  # Pinecrest wins
        result = self.run_tool("create_purchase", {"option_id": options[1]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "vote_required")

    def test_tied_vote_cannot_be_bought(self):
        options = self.search()
        data = self.run_tool("start_vote", {"option_ids": [options[0]["id"], options[1]["id"]]})["data"]
        vote = Vote.objects.get(pk=data["vote_id"])
        for i, member in enumerate(self.members.values()):
            Ballot.objects.create(vote=vote, member=member, choice_key="1" if i < 3 else "2")
        for index in (0, 1):
            result = self.run_tool("create_purchase", {"option_id": options[index]["id"], "deadline_hours": 24,
                                                       "member_ids": [str(m.id) for m in self.members.values()]})
            self.assertEqual(result["error"]["code"], "vote_required")

    def test_full_flow_creates_shares_and_collecting_state(self):
        purchase, options = self.make_open_purchase()
        self.assertEqual(purchase.state, "collecting")
        self.assertEqual(purchase.total_cents, 105000)
        shares = list(purchase.shares.all())
        self.assertEqual(len(shares), 6)
        self.assertTrue(all(s.amount_cents == 17500 and s.status == "invited" for s in shares))
        self.assertEqual(sum(s.amount_cents for s in shares), purchase.total_cents)
        moves = [(e.payload["from"], e.payload["to"]) for e in EventLog.objects.filter(type="purchase.state_changed")]
        self.assertEqual(moves, [("proposed", "voting"), ("voting", "collecting")])
        self.assertGreater(purchase.deadline_at, timezone.now() + timedelta(hours=23))

    def test_leftover_cents_go_to_the_organizer(self):
        option = Option.objects.create(group=self.group, merchant_slug="pinestay", listing_ref="odd", category="rental",
                                       title="Odd price", total_cents=100_000, capacity=6, score=50)
        other = Option.objects.create(group=self.group, merchant_slug="pinestay", listing_ref="odd2", category="rental",
                                      title="Other", total_cents=90_000, capacity=6, score=40)
        self.vote_for([str(option.id), str(other.id)], 1)
        result = self.run_tool("create_purchase", {"option_id": str(option.id), "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        shares = {s.member.display_name: s for s in Share.objects.filter(purchase_id=result["data"]["purchase_id"])}
        self.assertEqual(shares["Maya"].amount_cents, 16_670)            # 16,666 + 4 leftover cents
        self.assertEqual(shares["Maya"].rounding_adj_cents, 4)
        self.assertTrue(all(s.amount_cents == 16_666 for n, s in shares.items() if n != "Maya"))
        self.assertEqual(sum(s.amount_cents for s in shares.values()), 100_000)       # test_splits_sum_to_total

    def test_second_purchase_for_same_option_is_rejected(self):
        purchase, options = self.make_open_purchase()
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "purchase_exists")

    def test_member_from_another_group_is_rejected(self):
        options = self.search()
        self.vote_for([options[0]["id"], options[1]["id"]], 1)
        outsider = Member.objects.create(group=Group.objects.create(name="Other"), display_name="Eve", phone_e164_hash="e" * 64)
        ids = [str(self.maya.id), str(outsider.id)]
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24, "member_ids": ids})
        self.assertEqual(result["error"]["code"], "not_found")

    def test_over_capacity_is_rejected(self):
        options = self.search()
        self.vote_for([options[0]["id"], options[1]["id"]], 1)
        Option.objects.filter(pk=options[0]["id"]).update(capacity=4)
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "over_capacity")

    def test_over_limit_is_rejected(self):
        options = self.search()
        self.vote_for([options[0]["id"], options[1]["id"]], 1)
        Option.objects.filter(pk=options[0]["id"]).update(total_cents=1_200_000)
        result = self.run_tool("create_purchase", {"option_id": options[0]["id"], "deadline_hours": 24,
                                                   "member_ids": [str(m.id) for m in self.members.values()]})
        self.assertEqual(result["error"]["code"], "over_limit")

    def test_consent_links_are_private_and_signed(self):
        from payments.services import read_consent_token         # Ani's reader: proves the link is a real consent token
        purchase, _ = self.make_open_purchase()
        share = purchase.shares.get(member=self.members["Priya"])
        link = rules.consent_link(share)
        self.assertIn("/c/", link)
        opened = read_consent_token(link.split("/c/")[1], header_member_id=str(share.member_id))
        self.assertEqual(opened["share"].id, share.id)
        self.assertEqual(opened["payload"]["member_id"], str(share.member_id))
        # links are never returned to the AI and never posted in the group chat
        self.assertNotIn("/c/", json.dumps(self.run_tool("check_status", {})))
        from chat.models import ChatMessage
        self.assertFalse(ChatMessage.objects.filter(text_redacted__contains="/c/").exists())


class StatusAndOptionsApiTests(EngineBase):
    def test_check_status_returns_counts_only(self):
        purchase, _ = self.make_open_purchase()
        data = self.run_tool("check_status", {"purchase_id": str(purchase.id)})["data"]
        self.assertEqual(set(data), {"purchase_id", "state", "total_cents", "held_cents", "deadline_at",
                                     "held", "viewing", "invited", "out", "active"})
        self.assertEqual((data["state"], data["invited"], data["active"]), ("collecting", 6, 6))
        for name in self.members:
            self.assertNotIn(name, json.dumps(data))

    def test_check_status_defaults_to_latest_purchase(self):
        purchase, _ = self.make_open_purchase()
        self.assertEqual(self.run_tool("check_status", {})["data"]["purchase_id"], str(purchase.id))

    def test_check_status_without_any_purchase(self):
        self.assertEqual(self.run_tool("check_status", {})["error"]["code"], "not_found")

    def test_options_endpoint_returns_the_latest_search(self):
        self.search()
        shown = APIClient().get(f"/api/v1/groups/{self.group.id}/options", HTTP_X_TANDEM_MEMBER=str(self.maya.id)).json()
        self.assertEqual([o["title"] for o in shown["options"]], ["Pinecrest A-Frame", "Lakeview Bungalow", "Cedar Hollow"])
        self.assertEqual(set(shown["options"][0]), set(__import__("purchases.fixtures", fromlist=["FIXTURES"]).FIXTURES["options"]["options"][0]))
