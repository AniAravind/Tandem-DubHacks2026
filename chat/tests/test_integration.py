"""Real engine handoffs, independent of provider availability."""
from unittest import mock
from django.test import TestCase, override_settings
from agent.tests.helpers import seed
from chat import services
from chat.adapters.base import AgentMessage
from chat.models import ChatMessage
from purchases.rules import RulesEngine
from purchases.models import Purchase
from core.models import Member

@override_settings(DEMO_MODE=True, SECURE_SSL_REDIRECT=False)
class RealChatIntegrationTests(TestCase):
    def setUp(self):
        self.group = seed()
        self.members = list(self.group.members.all())
        self.actor = f"member:{self.members[0].id}"

    def test_vote_creates_one_purchase_and_private_links(self):
        engine = RulesEngine()
        options = engine.execute("search_options", {"category": "rental", "group_size": 6}, self.actor)["data"]["options"]
        vote = engine.execute("start_vote", {"option_ids": [o["id"] for o in options]}, self.actor)["data"]
        for m in self.members:
            services.handle_vote_reply(self.group.id, m.id, "1")
        self.assertEqual(Purchase.objects.filter(group=self.group).count(), 1)
        links = ChatMessage.objects.filter(group=self.group, text_redacted__contains="/c/")
        self.assertEqual(links.count(), 6)
        self.assertEqual(set(links.values_list("member_id", flat=True)), {m.id for m in self.members})
        self.assertFalse(links.filter(member__isnull=True).exists())
        services.handle_vote_reply(self.group.id, self.members[-1].id, "1")
        self.assertEqual(Purchase.objects.filter(group=self.group).count(), 1)

    def test_private_sender_rejects_outside_group(self):
        with self.assertRaises(ValueError):
            services.send_private(self.group.id, "00000000-0000-0000-0000-000000000001", AgentMessage(text="private"))

    @mock.patch("agent.orchestrator.run", return_value=[])
    def test_worker_uses_triggering_message(self, run):
        first = services.store_inbound(services.InboundMessage(group_id=str(self.group.id), member_id=str(self.members[0].id), text="@tandem first"))
        services.store_inbound(services.InboundMessage(group_id=str(self.group.id), member_id=str(self.members[0].id), text="@tandem second"))
        services.run_agent(str(self.group.id), str(self.members[0].id), message_id=str(first.id))
        self.assertEqual(run.call_args.args[1].text, "@tandem first")
