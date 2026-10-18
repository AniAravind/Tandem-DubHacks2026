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
    from chat.models import PhotonSession
    if PhotonSession.objects.filter(member__group=group).exists():
        state['mode'] = 'private_planning'
        state['planning_instructions'] = 'Plan for the intended trip size, not the one person in this private conversation. Once the user agrees on an option, publish_plan creates a separate website group. Never start a vote or collect payment here.'
        from purchases.serializers import latest_options_json
        state['planning_options'] = [
            {**{key: o[key] for key in ('id', 'rank', 'title', 'total_cents', 'per_person_cents', 'group_size', 'features')},
             'dates': {key: o['meta'].get(key) for key in ('check_in', 'check_out')}, 'location': o['meta'].get('location', '')}
            for o in latest_options_json(group)['options'][:3]]
        from purchases.models import TripPlan
        state['published_plans'] = [
            {'plan_id': str(plan.id), 'title': plan.title, 'option_id': plan.selected_option.meta.get('source_option_id')}
            for plan in TripPlan.objects.filter(source_group=group).select_related('selected_option').order_by('-created_at')[:3]]
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


def transcript(group: Group, *, user_only=False) -> str:
    """Last 50 messages as JSON inside the untrusted wrapper. JSON-escaping keeps users from closing the tag."""
    query = ChatMessage.objects.filter(group=group)
    if user_only:
        # Earlier assistant errors are not planning facts. Current options and
        # publications come from the database, while user requirements persist.
        query = query.filter(direction='in')
    msgs = list(query.select_related("member").order_by("-seq")[:MAX_MESSAGES])[::-1]
    rows = [{"from": "tandem" if m.direction == "out" else handle(m.member.display_name if m.member else "unknown"),
             "text": m.text_redacted} for m in msgs if not (m.direction == "out" and m.member_id)]   # skip private DMs
    body = json.dumps(rows, ensure_ascii=False)
    while len(body) > MAX_TRANSCRIPT_CHARS and len(rows) > 1:
        rows = rows[1:]
        body = json.dumps(rows, ensure_ascii=False)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<chat_transcript untrusted="true">{body}</chat_transcript>'


def build_messages(group: Group, system_prompt: str, engine=None, event: dict | None = None) -> list[dict]:
    state = build_group_state(group, engine, event)
    private = state.get('mode') == 'private_planning'
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": "group_state: " + json.dumps(state)},
        {"role": "user", "content": transcript(group, user_only=private)},
    ]
    if private:
        messages.append({'role': 'system', 'content': (
            'CURRENT MODE: individual organizer planning. Exactly one member here is normal and NEVER blocks a search for the intended trip size. '
            'Other travelers join only AFTER publication on the website. Never ask to add them before searching. '
            'Short replies such as add or 1 do not replace a previously specified trip headcount. '
            'Use the latest user requirements, not earlier assistant claims. Search actual options first. '
            'Reuse matching planning_options instead of searching again. For an existing published plan, call publish_plan with its source option_id to resend the real organizer link. '
            'When the user asks for an invite/link and has supplied trip details, publish_plan may use the top-ranked matching option as a proposed rough plan unless they chose another. '
            'Publishing never authorizes payment; each person reviews and accepts the proposed plan on the website. '
            'A link exists only after successful publish_plan. Never say a link was sent or is ready without that tool succeeding. '
            'Do not write organizer URLs yourself. The server attaches the real private link and correct sharing instructions.')})
    return messages
