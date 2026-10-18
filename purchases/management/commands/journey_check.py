"""Run scripted private buying conversations against the REAL configured AI model and check each reply.

    python manage.py journey_check                  # every journey
    python manage.py journey_check --journey topic  # one journey
    python manage.py journey_check --list

Each run creates its own isolated private group named "Journey check ..." (no demo data is touched, no payment is
approved). Each user turn costs roughly 3-6k AI tokens, so on Groq's free tier (200k tokens/day per model) run one
journey at a time, not in a loop. Exit status is 1 if any expectation fails.
"""
import hashlib
import time
import uuid

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

STUCK = ("slow moment", "AI provider is busy", "trouble thinking")

# (user message, expected substrings: the reply must contain at least one of them)
JOURNEYS = {
    "topic": [
        ("Help me plan a Tahoe cabin weekend Nov 14-16 for me and 5 friends, about $200 each", ["Your shortlist"]),
        ("actually forget the trip, I need a birthday gift for my sister, just me buying, around $40",
         ["Your shortlist", "?"]),
        ("1", ["is ready", "isn’t on this shortlist", "don’t have current options"]),
    ],
    "gift": [
        ("me and 3 friends want to get Rishab a birthday gift, $30 each", ["Your shortlist", "?"]),
        ("send me the link", ["is ready"]),
    ],
    "vague": [
        ("I want to buy something nice for our apartment", ["?"]),
        ("a couch, 4 roommates splitting, $300 each max", ["Your shortlist", "No demo-catalog offer", "?"]),
    ],
}


class Command(BaseCommand):
    help = "Check real AI conversations end to end (uses AI quota)."

    def add_arguments(self, parser):
        parser.add_argument("--journey", choices=sorted(JOURNEYS))
        parser.add_argument("--pause", type=float, default=15, help="Seconds between turns (respects per-minute limits).")
        parser.add_argument("--list", action="store_true")

    def handle(self, *args, **opts):
        if opts["list"]:
            for name, turns in JOURNEYS.items():
                self.stdout.write(f"{name}: " + " | ".join(t for t, _ in turns))
            return
        from agent import llm
        if llm.client.api_key in ("", "missing-xai-api-key"):
            raise CommandError("XAI_API_KEY is not set; this command talks to the real AI provider.")
        call_command("seed_buying_catalog", verbosity=0)
        failures = 0
        for name in [opts["journey"]] if opts["journey"] else list(JOURNEYS):
            failures += self.run_journey(name, JOURNEYS[name], opts["pause"])
        if failures:
            raise CommandError(f"{failures} expectation(s) failed.")
        self.stdout.write(self.style.SUCCESS("All journey expectations passed."))

    def run_journey(self, name, turns, pause):
        from chat import services
        from chat.adapters.base import InboundMessage
        from chat.models import ChatMessage, PhotonSession
        from core.models import Group, Member
        tag = uuid.uuid4().hex[:8]
        group = Group.objects.create(name=f"Journey check {name} {tag}", channel="imessage")
        member = Member.objects.create(group=group, display_name="Journey check", role="organizer",
                                       phone_e164_hash=hashlib.sha256(tag.encode()).hexdigest(), phone_last4="0000")
        group.organizer = member
        group.save(update_fields=["organizer"])
        PhotonSession.objects.create(key=hashlib.sha256(("journey" + tag).encode()).hexdigest(), member=member)
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n== {name} ({group.id})"))
        seen, failures = 0, 0
        for i, (text, expected) in enumerate(turns):
            if i:
                time.sleep(pause)
            started = time.time()
            msg = InboundMessage(str(group.id), str(member.id), text, "imessage")
            stored = services.store_inbound(msg)
            vote = services.VOTE_REPLY.match(text)
            if not (vote and services.handle_vote_reply(msg.group_id, msg.member_id, vote.group(1))):
                services.run_agent(msg.group_id, msg.member_id, None, str(stored.id))
            replies = list(ChatMessage.objects.filter(group=group, direction="out", seq__gt=seen).order_by("seq"))
            seen = replies[-1].seq if replies else seen
            body = "\n".join(r.text_redacted for r in replies)
            cards = [r.card["type"] for r in replies if r.card]
            self.stdout.write(f"> {text}")
            self.stdout.write(f"< ({time.time() - started:.1f}s, cards={cards})\n  " + body.replace("\n", "\n  "))
            ok = any(e in body for e in expected) and not any(s in body for s in STUCK)
            if not ok:
                failures += 1
                self.stdout.write(self.style.ERROR(f"  FAIL: expected one of {expected}"))
        brief = PhotonSession.objects.get(member=member).planning_state
        self.stdout.write(f"  brief: {brief.get('goal')} · {brief.get('category')} · {brief.get('group_size')} buyer(s)" if brief else "  brief: none")
        return failures
