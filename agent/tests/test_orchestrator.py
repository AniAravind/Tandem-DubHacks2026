import json

from django.test import TestCase

from agent.context import build_messages
from agent.fakes import FakeRulesEngine
from agent.orchestrator import FAILURE_TEXT, SYSTEM_PROMPT, Orchestrator
from agent.tests.helpers import ScriptedLLM, reply, seed, tool_call
from agent.validate import validate
from core.models import EventLog, Group, Member

SEARCH = {"category": "rental", "group_size": 6, "location": "Tahoe"}


class OrchestratorTest(TestCase):
    def setUp(self):
        self.group = seed()
        self.maya = Member.objects.get(group=self.group, display_name="Maya")
        self.actor = f"member:{self.maya.id}"

    def orch(self, *responses):
        llm = ScriptedLLM(*responses)
        return Orchestrator(self.group.id, engine=FakeRulesEngine(self.group.id), llm_chat=llm), llm

    def test_search_then_vote_builds_cards_from_tool_data(self):
        o, _ = self.orch(reply(calls=[tool_call("search_options", SEARCH)]),
                         reply(calls=[tool_call("start_vote", {"option_ids": ["op-pinecrest", "op-lakeview", "op-cedar"]})]),
                         reply("Here are 3 cabins that fit everyone. Reply 1, 2, or 3."))
        out = o.run(self.actor)
        types = [m.card.type for m in out if m.card]
        self.assertEqual(types, ["OptionCard", "OptionCard", "OptionCard", "VoteCard"])
        first = next(m.card.data for m in out if m.card and m.card.type == "OptionCard")
        self.assertEqual((first["per_person_cents"], first["title"]), (17500, "Pinecrest A-Frame"))
        self.assertEqual(out[0].text, "Here are 3 cabins that fit everyone. Reply 1, 2, or 3.")

    def test_create_purchase_sends_private_consent_links(self):
        engine = FakeRulesEngine(self.group.id)
        engine.execute("search_options", SEARCH, self.actor)
        ids = [str(m.id) for m in self.group.members.all()]
        o = Orchestrator(self.group.id, engine=engine, llm_chat=ScriptedLLM(
            reply(calls=[tool_call("create_purchase", {"option_id": "op-pinecrest", "member_ids": ids, "deadline_hours": 6})]),
            reply("Next: everyone holds their share.")))
        out = o.run("agent")
        self.assertEqual(out[0].card.type, "FundingCard")
        private = [m for m in out if m.private_to_member_id]
        self.assertEqual(len(private), 6)
        self.assertTrue(all(m.link.startswith("/c/") for m in private))

    def test_unknown_tool_rejected(self):
        o, llm = self.orch(reply(calls=[tool_call("set_cap", {"member_id": "x", "cap_cents": 50000})]), reply("No."))
        o.run(self.actor)
        tool_msg = [m for m in llm.requests[1] if m["role"] == "tool"][0]
        self.assertEqual(json.loads(tool_msg["content"])["error"]["code"], "unknown_tool")
        self.assertTrue(EventLog.objects.filter(type="agent.tool_rejected", payload__reason="unknown_tool").exists())

    def test_bad_args_rejected_then_repaired(self):
        o, llm = self.orch(reply(calls=[tool_call("search_options", {"category": "castle", "group_size": 6, "cap": 5})]),
                           reply(calls=[tool_call("search_options", SEARCH)]),
                           reply("Found 3."))
        out = o.run(self.actor)
        first = json.loads([m for m in llm.requests[1] if m["role"] == "tool"][0]["content"])
        self.assertEqual(first["error"]["code"], "schema_invalid")
        self.assertEqual(sum(1 for m in out if m.card and m.card.type == "OptionCard"), 3)

    def test_ids_from_another_group_rejected(self):
        other = Group.objects.create(name="Other")
        stranger = Member.objects.create(group=other, display_name="Zed", phone_e164_hash="z" * 64)
        engine = FakeRulesEngine(self.group.id)
        engine.execute("search_options", SEARCH, self.actor)
        o = Orchestrator(self.group.id, engine=engine, llm_chat=ScriptedLLM(
            reply(calls=[tool_call("create_purchase", {"option_id": "op-pinecrest", "member_ids": [str(self.maya.id), str(stranger.id)],
                                                       "deadline_hours": 6})]), reply("Hmm.")))
        out = o.run(self.actor)
        self.assertFalse(any(m.card for m in out))
        self.assertTrue(EventLog.objects.filter(type="agent.tool_rejected", payload__reason="unknown_id").exists())

    def test_budgets_never_in_llm_context(self):
        caps = [m.budget_cap_cents for m in self.group.members.all()]
        blob = json.dumps(build_messages(self.group, SYSTEM_PROMPT, FakeRulesEngine(self.group.id)))
        self.assertNotIn("budget_cap", blob)
        self.assertNotIn("demo_card_last4", blob)
        for cap in caps:
            self.assertNotIn(str(cap), blob)
            self.assertNotIn(f"${cap // 100}", blob)

    def test_llm_failure_gives_safe_message_and_no_state_change(self):
        o, _ = self.orch(TimeoutError("slow"), TimeoutError("slow again"))
        before = EventLog.objects.count()
        self.assertEqual([m.text for m in o.run(self.actor)], [FAILURE_TEXT])
        self.assertEqual(EventLog.objects.count(), before)

    def test_tool_call_limit_per_turn(self):
        o, llm = self.orch(*[reply(calls=[tool_call("check_status", {})]) for _ in range(6)], reply("ok"))
        o.run(self.actor)
        self.assertLessEqual(EventLog.objects.filter(type__startswith="agent.tool").count(), 4)

    def test_only_one_money_tool_per_step(self):
        engine = FakeRulesEngine(self.group.id)
        engine.execute("search_options", SEARCH, self.actor)
        ids = [str(m.id) for m in self.group.members.all()]
        two = [tool_call("create_purchase", {"option_id": "op-pinecrest", "member_ids": ids, "deadline_hours": 6}),
               tool_call("add_expense", {"amount": 60, "payer_id": ids[0], "split": {"mode": "equal"}})]
        o = Orchestrator(self.group.id, engine=engine, llm_chat=ScriptedLLM(reply(calls=two), reply("ok")))
        o.run(self.actor)
        self.assertEqual(EventLog.objects.filter(type="agent.tool_called").count(), 1)


class ValidateTest(TestCase):
    def test_schema_keywords(self):
        from agent.orchestrator import SCHEMAS
        self.assertEqual(validate(SEARCH, SCHEMAS["search_options"]), [])
        self.assertTrue(validate({"category": "rental", "group_size": 1}, SCHEMAS["search_options"]))
        self.assertTrue(validate({"category": "rental", "group_size": True}, SCHEMAS["search_options"]))
        self.assertTrue(validate({**SEARCH, "dates": {"start": "nov 14"}}, SCHEMAS["search_options"]))
        self.assertTrue(validate({"amount": 0, "payer_id": "x", "split": {"mode": "equal"}}, SCHEMAS["add_expense"]))
        self.assertTrue(validate({"option_ids": ["a"]}, SCHEMAS["start_vote"]))
