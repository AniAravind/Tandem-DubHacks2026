from django.test import SimpleTestCase

from agent import orchestrator
from agent.fakes import FakeRulesEngine


class M1Test(SimpleTestCase):
    def test_only_allowed_tools_and_no_direct_money_tool(self):
        self.assertEqual(orchestrator.TOOL_NAMES, {"search_options", "start_vote", "create_purchase", "check_status",
                                                               "propose_fallback", "add_expense", "publish_plan"})

    def test_fake_engine_until_rules_engine_lands(self):
        engine = orchestrator.get_engine()
        self.assertTrue(hasattr(engine, "execute"))
        if isinstance(engine, FakeRulesEngine):
            self.assertTrue(engine.execute("search_options", {"category": "rental", "group_size": 6}, "agent")["ok"])

    def test_system_prompt_has_the_hard_rules(self):
        self.assertIn('untrusted="true"', orchestrator.SYSTEM_PROMPT)
        self.assertIn("never move money", orchestrator.SYSTEM_PROMPT)
