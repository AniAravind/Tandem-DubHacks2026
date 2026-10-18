"""Chat entry points: store inbound messages, route them (vote reply, agent, or nothing), and post agent messages.

Eshaan's rules call post_card(group_id, AgentMessage) for status updates. Nothing here imports payments/.
"""
import logging
import uuid
import os
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from agent.cards import consent_links, funding_card, usd, vote_card, when
from chat.adapters.base import AgentMessage, InboundMessage
from chat.adapters.web import WebChatAdapter, next_seq
from chat.models import ChatMessage
from core.events import EventType, log_event
from core.models import EventLog, Group

log = logging.getLogger(__name__)

MAX_MESSAGES_PER_MIN = 30
MAX_TOOL_CALLS_PER_MIN = 20
SLOW_DOWN_TEXT = "Lots going on in here. Give me a minute, then ask again."
VOTE_REPLY = re.compile(r"^\s*([1-4])\s*[.!)]?\s*$")
MENTION = re.compile(r"@tandem\b", re.IGNORECASE)

_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class RateLimited(Exception):
    pass


def redact(text: str) -> str:
    text = _PAN.sub("[card number removed]", text)
    text = _PHONE.sub("[phone removed]", text)
    return _EMAIL.sub("[email removed]", text)


def get_adapter():
    if os.getenv("CHAT_ADAPTER", "web") == "photon":
        from chat.adapters.photon import PhotonAdapter
        return PhotonAdapter()
    return WebChatAdapter()


def post_card(group_id, message: AgentMessage) -> str:
    """Send one agent message to the group (or privately, if message.private_to_member_id is set)."""
    message_id = get_adapter().send(str(group_id), message)
    log_event(EventType.AGENT_MESSAGE_SENT, actor="agent", group_id=group_id,
              payload={"message_id": message_id, "card": message.card.type if message.card else None,
                       "private": bool(message.private_to_member_id)})
    return message_id


def send_private(group_id, member_id, message):
    from core.models import Member
    if not Member.objects.filter(pk=member_id, group_id=group_id, status="active").exists():
        raise ValueError("Recipient is not an active member of this group")
    message.private_to_member_id = str(member_id)
    return post_card(group_id, message)


def post_all(group_id, messages: list[AgentMessage]) -> None:
    for m in messages:
        post_card(group_id, m)


# ------------------------------------------------------------------ inbound
def store_inbound(msg: InboundMessage) -> ChatMessage:
    since = timezone.now() - timedelta(minutes=1)
    with transaction.atomic():
        seq = next_seq(msg.group_id)
        recent = ChatMessage.objects.filter(group_id=msg.group_id, direction="in", created_at__gte=since).count()
        if recent >= MAX_MESSAGES_PER_MIN:
            raise RateLimited()
        return ChatMessage.objects.create(group_id=msg.group_id, member_id=msg.member_id, direction="in",
                                          text_redacted=redact(msg.text), channel=msg.channel, seq=seq)


def handle_inbound(msg: InboundMessage) -> ChatMessage:
    stored = store_inbound(msg)
    vote = VOTE_REPLY.match(msg.text)
    if vote and handle_vote_reply(msg.group_id, msg.member_id, vote.group(1)):
        return stored
    if MENTION.search(msg.text):
        schedule_agent(msg.group_id, msg.member_id, message_id=str(stored.id))
    return stored


def tool_calls_last_minute(group_id) -> int:
    since = timezone.now() - timedelta(minutes=1)
    return EventLog.objects.filter(group_id=group_id, type=EventType.AGENT_TOOL_CALLED, created_at__gte=since).count()


def schedule_agent(group_id, member_id=None, event: dict | None = None, message_id=None) -> None:
    """Run the agent in the Celery worker; if the broker is down, run it right here so the chat still answers."""
    from chat.tasks import run_agent_task
    args = (str(group_id), str(member_id) if member_id else None, event, message_id)
    try:
        run_agent_task.delay(*args)
    except Exception:                                           # noqa: BLE001
        log.warning("Celery unavailable, running the agent inline", exc_info=True)
        run_agent(*args)


def run_agent(group_id, member_id=None, event: dict | None = None, message_id=None) -> None:
    from agent import orchestrator
    if tool_calls_last_minute(group_id) >= MAX_TOOL_CALLS_PER_MIN:
        post_card(group_id, AgentMessage(text=SLOW_DOWN_TEXT))
        return
    if event and member_id is None and event.get("type") in {"vote_closed", "share_declined"}:
        if hasattr(orchestrator.get_engine(group_id), "snapshot"):
            post_all(group_id, orchestrator.run(group_id, None, event))
            return
        # Trusted scheduler events still act through a real active group member.
        group = Group.objects.select_related("organizer").get(pk=group_id)
        if group.organizer_id and group.organizer.status == "active":
            if event["type"] == "vote_closed":
                from purchases.models import Purchase
                if Purchase.objects.filter(group=group, option_id=event.get("winner_option_id")).exists():
                    return
            post_all(group_id, orchestrator.Orchestrator(group_id).run(f"member:{group.organizer_id}", event=event))
        return
    inbound = InboundMessage(group_id=str(group_id), member_id=member_id, text="") if member_id else None
    if message_id and inbound:
        stored = ChatMessage.objects.get(pk=message_id, group_id=group_id, member_id=member_id, direction="in")
        inbound.text = stored.text_redacted
        # A queued private turn can become obsolete while the user chooses an
        # option synchronously. Never let its old shortlist bury the plan link.
        from chat.models import PhotonSession
        private = PhotonSession.objects.filter(member_id=member_id, member__group_id=group_id).exists()
        if private and ChatMessage.objects.filter(group_id=group_id, direction="in", seq__gt=stored.seq).exists():
            return
    replies = orchestrator.run(group_id, inbound, event)
    if message_id and inbound and private and ChatMessage.objects.filter(group_id=group_id, direction="in", seq__gt=stored.seq).exists():
        return
    post_all(group_id, replies)


# ------------------------------------------------------------------ "1" / "2" / "3" replies go to the ballot, no LLM
def handle_vote_reply(group_id, member_id, key: str) -> bool:
    from chat.models import PhotonSession
    if PhotonSession.objects.filter(member_id=member_id, member__group_id=group_id).exists():
        from agent.orchestrator import Orchestrator
        from agent.private_actions import handle
        direct = handle(Orchestrator(group_id), f'member:{member_id}', key)
        if direct is not None:
            post_all(group_id, direct)
            return True
    from purchases.models import Vote
    vote = Vote.objects.filter(group_id=group_id, status="open").order_by("-created_at").first()
    if vote:
        return _real_ballot(vote, member_id, key)
    from agent.orchestrator import get_engine
    engine = get_engine(group_id)
    if not hasattr(engine, "open_vote") or not (fake_vote := engine.open_vote()):
        return False
    result = engine.cast_ballot(fake_vote["id"], str(member_id), key)
    if not result["ok"]:
        post_card(group_id, AgentMessage(text=result["error"]["message"], private_to_member_id=str(member_id)))
        return True
    data = result["data"]
    post_card(group_id, AgentMessage(text="", card=vote_card(data)))
    if data.get("winner_key"):
        _announce_winner(group_id, engine, data)
    return True


@transaction.atomic
def _real_ballot(vote, member_id, key: str) -> bool:
    """Eshaan's ballot view does the validation and logging; we only relay the result to the chat."""
    from rest_framework.test import APIRequestFactory
    from purchases.api import cast_ballot
    from purchases.models import Vote
    vote = Vote.objects.select_for_update().get(pk=vote.pk)
    request = APIRequestFactory().post(f"/api/v1/votes/{vote.id}/ballots", {"choice_key": key}, format="json",
                                       HTTP_X_TANDEM_MEMBER=str(member_id))
    response = cast_ballot(request, vid=vote.id)
    if response.status_code >= 400:
        post_card(vote.group_id, AgentMessage(text=response.data["error"]["message"], private_to_member_id=str(member_id)))
        return True
    vote.refresh_from_db()
    tally = response.data["tally"]
    from purchases.models import Option
    def choice_data(choice):
        # Fallback choices carry their own price and use keys like "split_gap", not option UUIDs.
        if isinstance(choice.get("per_person_cents"), int):
            return {**choice, "count": tally.get(choice["key"], 0)}
        try:
            option = Option.objects.filter(pk=uuid.UUID(str(choice.get("option_id"))), group_id=vote.group_id).first()
        except ValueError:
            option = None
        return {**choice, "count": tally.get(choice["key"], 0),
                "per_person_cents": (option.meta or {}).get("per_person_cents", 0) if option else 0}
    post_card(vote.group_id, AgentMessage(text="", card=vote_card({
        "vote_id": str(vote.id), "closes_at": vote.closes_at.isoformat(), "quorum": vote.quorum,
        "voted": response.data["voted"],
        "winner_key": vote.winner_key,
        "choices": [choice_data(c) for c in vote.choices]})))
    if vote.status == "closed" and vote.winner_key:
        from purchases.rules import RulesEngine
        from purchases.models import Purchase
        winner = next(c for c in vote.choices if c["key"] == vote.winner_key)
        if vote.purpose == "pick_option":
            # The engine rejects duplicate open purchases under its own transaction.
            if not Purchase.objects.filter(group_id=vote.group_id, option_id=winner["option_id"]).exists():
                result = RulesEngine().execute("create_purchase", {
                    "option_id": winner["option_id"],
                    "member_ids": [str(m.id) for m in vote.group.members.filter(status="active")],
                    "deadline_hours": 6}, f"member:{member_id}")
                if result.get("ok"):
                    from purchases.rules import _post_group_update
                    _post_group_update(result["data"]["purchase_id"])
                    post_card(vote.group_id, AgentMessage(text=f"🗳️ {winner['label']} wins, {tally[vote.winner_key]} votes to {sum(tally.values()) - tally[vote.winner_key]}."))
    return True


def _announce_winner(group_id, engine, vote: dict) -> None:
    winner = next(c for c in vote["choices"] if c["key"] == vote["winner_key"])
    others = vote["voted"] - winner["count"]
    if vote["purpose"] == "fallback":
        p = engine.purchase_data()
        staying = p["counts"]["active"]
        if winner["option_id"] == "replacement":
            text = f"Option {winner['key']} it is ({winner['count']} of {staying}). Same price, and the deadline moved 12 hours. Invite a 6th person to the chat."
        else:
            text = (f"Option {winner['key']} it is ({winner['count']} of {staying}). {p['title']} at "
                    f"{usd(p['shares'][0]['amount_cents'])} each. I updated each of your holds. "
                    "Tap your link to confirm (you can still back out).")
        post_card(group_id, AgentMessage(text=text, card=funding_card(p)))
        post_all(group_id, consent_links(p, Group.objects.get(pk=group_id).timezone))
        return
    post_card(group_id, AgentMessage(text=f"🗳️ {winner['label']} wins, {winner['count']} votes to {others}."))
    schedule_agent(group_id, None, {"type": "vote_closed", "vote_id": vote["vote_id"], "winner_option_id": winner["option_id"],
                                    "winner_label": winner["label"]})


# ------------------------------------------------------------------ server events and fake-engine demo actions
def on_share_declined(group_id, purchase_id) -> None:
    """For the rules engine: a member declined or left, so ask the agent for fallbacks (it never says who)."""
    schedule_agent(group_id, None, {"type": "share_declined", "purchase_id": str(purchase_id)})


class DemoUnavailable(Exception):
    pass


def demo_share_action(member, action: str) -> dict:
    """Stand-in for the consent page while the fake engine is in use: hold or decline YOUR OWN share."""
    from agent.fakes import FakeRulesEngine
    from agent.orchestrator import get_engine
    engine = get_engine(member.group_id)
    if not isinstance(engine, FakeRulesEngine):
        raise DemoUnavailable("Use your consent link; the real payment flow is live.")
    p = engine.purchase_data()
    share = next((s for s in (p or {}).get("shares", []) if s["member_id"] == str(member.id)), None)
    if not share or share["status"] not in ("invited", "viewing"):
        raise DemoUnavailable("You don't have a share waiting for you.")
    p = engine.set_share_status(str(member.id), "held" if action == "hold" else "declined")
    counts = p["counts"]
    if action == "hold":
        text = f"{member.display_name}'s in. {counts['held']} of {counts['active']} held."
        if counts["held"] == counts["active"]:
            text = f"✅ All {counts['active']} in. Nobody fronted anything. Booking goes through signed merchant checkout next. (Simulated.)"
        elif counts["active"] - counts["held"] == 1:
            text += f" Waiting on 1 more, deadline {when(p['deadline_at'], member.group.timezone)}."
        post_card(member.group_id, AgentMessage(text=text, card=funding_card(p)))
    else:
        post_card(member.group_id, AgentMessage(text="", card=funding_card(p)))
        on_share_declined(member.group_id, p["purchase_id"])
    return {"share_status": "held" if action == "hold" else "declined", "simulated": True}
