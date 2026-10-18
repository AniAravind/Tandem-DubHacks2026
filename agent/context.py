"""Builds what Grok sees: rules, server-side group state (never budgets), and the chat wrapped as untrusted data."""
import json
from collections import Counter
from zoneinfo import ZoneInfo

from django.utils import timezone

from chat.models import ChatMessage
from core.models import Group
from purchases.models import Purchase, Vote

MAX_MESSAGES = 50
MAX_TRANSCRIPT_CHARS = 24_000          # ~6k tokens
LIVE_PURCHASE_STATES = ("proposed", "voting", "collecting", "funded", "booked")


def handle(display_name: str) -> str:
    return "m_" + "".join(ch for ch in display_name.lower() if ch.isalnum())


def build_group_state(group: Group, engine=None, event: dict | None = None) -> dict:
    members = list(group.members.filter(status="active").order_by("created_at", "display_name"))
    wants = Counter(f for m in members for f in (m.prefs or {}).get("must_haves", []))
    state = {
        "current_date": timezone.localdate(timezone=ZoneInfo(group.timezone)).isoformat(),
        "group": {"id": str(group.id), "name": group.name, "timezone": group.timezone},
        "members": [{"id": str(m.id), "handle": handle(m.display_name), "display_name": m.display_name, "role": m.role}
                    for m in members],
        "must_have_counts": dict(wants),                       # how many people asked for each feature; no names, no budgets
    }
    purchase = Purchase.objects.filter(group=group, state__in=LIVE_PURCHASE_STATES).select_related("option").order_by("-created_at").first()
    if purchase:
        statuses = Counter(purchase.shares.filter(superseded_by__isnull=True).values_list("status", flat=True))
        state["purchase"] = {"id": str(purchase.id), "title": purchase.option.title, "state": purchase.state,
                             "deadline_at": purchase.deadline_at.isoformat(), "counts": dict(statuses)}
    vote = Vote.objects.filter(group=group, status="open").order_by("-created_at").first()
    if vote:
        state["open_vote"] = {"id": str(vote.id), "purpose": vote.purpose, "closes_at": vote.closes_at.isoformat(),
                              "quorum": vote.quorum, "choices": [{"key": c["key"], "label": c.get("label", "")} for c in vote.choices]}
    if engine is not None and hasattr(engine, "snapshot"):
        for key, value in engine.snapshot().items():
            if value and key not in state:
                state[key] = value
    if event:
        state["event"] = event
    return state


def transcript(group: Group) -> str:
    """Last 50 messages as JSON inside the untrusted wrapper. JSON-escaping keeps users from closing the tag."""
    msgs = list(ChatMessage.objects.filter(group=group).select_related("member").order_by("-seq")[:MAX_MESSAGES])[::-1]
    rows = [{"from": "tandem" if m.direction == "out" else handle(m.member.display_name if m.member else "unknown"),
             "text": m.text_redacted} for m in msgs if not (m.direction == "out" and m.member_id)]   # skip private DMs
    body = json.dumps(rows, ensure_ascii=False)
    while len(body) > MAX_TRANSCRIPT_CHARS and len(rows) > 1:
        rows = rows[1:]
        body = json.dumps(rows, ensure_ascii=False)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<chat_transcript untrusted="true">{body}</chat_transcript>'


def build_messages(group: Group, system_prompt: str, engine=None, event: dict | None = None) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": "group_state: " + json.dumps(build_group_state(group, engine, event))},
        {"role": "user", "content": transcript(group)},
    ]
