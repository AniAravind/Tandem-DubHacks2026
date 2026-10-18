from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from agent.fakes import FakeRulesEngine
from agent.tests.helpers import seed
from chat import services
from chat.adapters.base import AgentMessage, InboundMessage
from chat.models import ChatMessage
from core.models import Member
from purchases.models import Ballot, Option, Vote


class RedactionTest(TestCase):
    def test_phone_email_and_card_numbers_are_removed(self):
        out = services.redact("call me at (206) 555-0100 or +1 206 555 0101, maya@example.com, card 4111 1111 1111 1111")
        self.assertNotIn("555", out)
        self.assertNotIn("maya@", out)
        self.assertNotIn("4111", out)
        self.assertEqual(services.redact("like 150-200 each, nov 14-16"), "like 150-200 each, nov 14-16")


@mock.patch("agent.orchestrator.get_engine", new=lambda group_id=None: FakeRulesEngine(group_id))   # real engine exists now
class ChatApiTest(TestCase):
    def setUp(self):
        self.group = seed()
        self.m = {m.display_name: m for m in Member.objects.filter(group=self.group)}

    def post(self, who, text):
        return self.client.post("/api/v1/chat/web", {"group_id": str(self.group.id), "text": text},
                                content_type="application/json", HTTP_X_TANDEM_MEMBER=str(self.m[who].id))

    def get(self, who, after=0):
        return self.client.get(f"/api/v1/groups/{self.group.id}/messages?after_seq={after}",
                               HTTP_X_TANDEM_MEMBER=str(self.m[who].id))

    @mock.patch("chat.services.schedule_agent")
    def test_post_and_poll(self, schedule):
        r = self.post("Maya", "@tandem find us a cabin, call 206-555-0100")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "queued")
        schedule.assert_called_once()
        msg = self.get("Priya").json()["messages"][0]
        self.assertEqual(msg["member"]["display_name"], "Maya")
        self.assertIn("[phone removed]", msg["text"])
        self.assertEqual(self.get("Priya", after=msg["seq"]).json()["messages"], [])

    @mock.patch("chat.services.schedule_agent")
    def test_plain_chatter_does_not_wake_the_agent(self, schedule):
        self.post("Leo", "lol nice")
        schedule.assert_not_called()

    def test_needs_member_header_of_this_group(self):
        r = self.client.post("/api/v1/chat/web", {"group_id": str(self.group.id), "text": "hi"}, content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_private_messages_only_reach_their_member(self):
        services.post_card(self.group.id, AgentMessage(text="Your share: $175", link="/c/abc", private_to_member_id=str(self.m["Sam"].id)))
        services.post_card(self.group.id, AgentMessage(text="Hi all"))
        sam = [x["text"] for x in self.get("Sam").json()["messages"]]
        leo = [x["text"] for x in self.get("Leo").json()["messages"]]
        self.assertEqual(sam, ["Your share: $175\n/c/abc", "Hi all"])
        self.assertEqual(leo, ["Hi all"])

    @mock.patch("chat.services.schedule_agent")
    def test_rate_limit(self, schedule):
        for i in range(services.MAX_MESSAGES_PER_MIN):
            self.assertEqual(self.post("Leo", f"msg {i}").status_code, 202)
        r = self.post("Leo", "one too many")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "rate_limited"))

    @mock.patch("chat.services.schedule_agent")
    def test_numeric_reply_votes_without_the_llm(self, schedule):
        from agent.fakes import FakeRulesEngine
        patcher = mock.patch("agent.orchestrator.get_engine", side_effect=lambda gid=None: FakeRulesEngine(gid or self.group.id))
        patcher.start()
        self.addCleanup(patcher.stop)
        engine = FakeRulesEngine(self.group.id)
        engine.execute("search_options", {"category": "rental", "group_size": 6}, "agent")
        engine.execute("start_vote", {"option_ids": ["op-pinecrest", "op-lakeview", "op-cedar"]}, "agent")
        for who in ("Maya", "Priya", "Sam"):
            self.post(who, "1")
        schedule.assert_not_called()
        self.post("Leo", "1")
        texts = list(ChatMessage.objects.filter(group=self.group, direction="out").values_list("text_redacted", flat=True))
        self.assertIn("🗳️ Pinecrest A-Frame wins, 4 votes to 0.", texts)
        event = schedule.call_args.args[2]
        self.assertEqual((event["type"], event["winner_option_id"]), ("vote_closed", "op-pinecrest"))

    def test_numeric_reply_uses_the_real_ballot_when_a_vote_row_exists(self):
        opt = Option.objects.create(group=self.group, merchant_slug="pinestay", listing_ref="pinecrest-a-frame", category="rental",
                                    title="Pinecrest A-Frame", total_cents=105000, capacity=6)
        vote = Vote.objects.create(group=self.group, purpose="pick_option", quorum=4, closes_at=timezone.now() + timedelta(hours=1),
                                   choices=[{"key": "1", "option_id": str(opt.id), "label": opt.title, "per_person_cents": 17500}])
        self.post("Priya", "1")
        self.assertTrue(Ballot.objects.filter(vote=vote, member=self.m["Priya"], choice_key="1").exists())
        card = ChatMessage.objects.filter(group=self.group, direction="out").last().card
        self.assertEqual((card["type"], card["data"]["choices"][0]["count"]), ("VoteCard", 1))


class SlowDownTest(TestCase):
    def test_too_many_tool_calls_gets_a_polite_reply_not_the_llm(self):
        from core.events import EventType, log_event
        group = seed()
        for _ in range(services.MAX_TOOL_CALLS_PER_MIN):
            log_event(EventType.AGENT_TOOL_CALLED, actor="agent", group_id=group.id, payload={"tool": "check_status"})
        with mock.patch("agent.orchestrator.run") as run:
            services.run_agent(group.id, str(group.members.first().id))
            run.assert_not_called()
        self.assertEqual(ChatMessage.objects.filter(group=group).last().text_redacted, services.SLOW_DOWN_TEXT)

    def test_inbound_dataclass_routes_through_handle_inbound(self):
        group = seed()
        m = group.members.first()
        with mock.patch("chat.services.schedule_agent") as schedule:
            services.handle_inbound(InboundMessage(group_id=str(group.id), member_id=str(m.id), text="@Tandem hi"))
        schedule.assert_called_once()
