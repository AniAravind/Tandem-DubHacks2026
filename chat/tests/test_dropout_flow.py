"""Chat-side regressions found by driving the full Tahoe demo: fallback ballots and post-dropout expenses."""
from chat import services
from chat.models import ChatMessage
from purchases.models import Purchase, Vote
from purchases.tests.test_fallbacks import FallbackBase


class DropoutChatFlowTests(FallbackBase):
    def reply(self, name, text):
        member = self.members[name]
        stored = ChatMessage.objects.create(group=self.group, member=member, direction="in", text_redacted=text,
                                            seq=(ChatMessage.objects.filter(group=self.group).count() + 1))
        return services.handle_vote_reply(str(self.group.id), str(member.id), text) if text.isdigit() else stored

    def test_chat_reply_on_fallback_vote_counts_and_applies_cheaper_option(self):
        self.hold_everyone_but_jordan()
        proposal = self.propose()
        vote = Vote.objects.get(pk=proposal["vote_id"])
        cheaper = next(c["key"] for c in vote.choices if c["option_id"] == "cheaper")
        for name in ("Maya", "Priya", "Sam"):
            self.assertTrue(self.reply(name, cheaper))             # used to crash: "cheaper" is not a UUID
        cards = [m.card for m in ChatMessage.objects.filter(group=self.group, direction="out") if m.card]
        vote_cards = [c for c in cards if c["type"] == "VoteCard"]
        self.assertTrue(vote_cards)
        prices = {c["key"]: c["per_person_cents"] for c in vote_cards[-1]["data"]["choices"]}
        self.assertEqual(prices[cheaper], next(c["per_person_cents"] for c in vote.choices if c["key"] == cheaper))
        self.purchase.refresh_from_db()
        self.assertIn("Juniper", self.purchase.option.title)

    def test_expense_after_dropout_splits_among_trip_members_once(self):
        from agent.context import trip_member_ids
        from agent.orchestrator import Orchestrator
        from agent import demo_script
        self.hold_everyone_but_jordan()
        Purchase.objects.filter(pk=self.purchase.pk).update(state="booked")
        trip = trip_member_ids(self.group)
        self.assertEqual(len(trip), 5)
        self.assertNotIn(str(self.members["Jordan"].id), trip)
        priya = self.members["Priya"]
        text = "@tandem I paid $60 for groceries, split with everyone"
        before = ChatMessage.objects.filter(group=self.group, direction="out").count()
        out = Orchestrator(self.group.id).run_scripted(f"member:{priya.id}", None, text)
        services.post_all(self.group.id, out)
        self.assertIn("split 5 ways ($12.00 each)", out[0].text)
        new_cards = [m.card["type"] for m in ChatMessage.objects.filter(group=self.group, direction="out")[before:] if m.card]
        self.assertEqual(new_cards.count("ExpenseCard"), 1)
        self.assertIsNotNone(demo_script)
