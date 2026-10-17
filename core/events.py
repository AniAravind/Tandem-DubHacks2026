import contextlib, contextvars, hashlib, json
from django.db import transaction
from django.db.models import Max
from django.utils import timezone


class EventType:
    GROUP_CREATED = "group.created"
    MEMBER_JOINED = "member.joined";              MEMBER_VERIFIED = "member.verified";   MEMBER_LEFT = "member.left"
    PREFS_UPDATED = "prefs.updated"               # payload never contains budget values
    OPTIONS_RANKED = "options.ranked"
    VOTE_OPENED = "vote.opened";                  BALLOT_CAST = "vote.ballot_cast";      VOTE_CLOSED = "vote.closed"
    PURCHASE_CREATED = "purchase.created";        PURCHASE_STATE_CHANGED = "purchase.state_changed"
    SHARE_STATE_CHANGED = "share.state_changed"
    CONSENT_ACCEPTED = "consent.accepted";        CONSENT_DECLINED = "consent.declined"
    FALLBACK_PROPOSED = "fallback.proposed";      FALLBACK_APPLIED = "fallback.applied"
    # gateway.* (simulated=True): agent_token_provisioned, agent_token_revoked, spend_controls_set, mandate_created,
    # mandate_revoked, hold_authorized, hold_declined, hold_rejected, hold_adjusted, hold_released, captured, refunded
    SIGNER_REQUEST_SIGNED = "signer.request_signed"
    MERCHANT_ACCEPTED = "merchant.request_accepted"; MERCHANT_REJECTED = "merchant.request_rejected"  # payload.reason (6.9)
    BOOKING_CONFIRMED = "booking.confirmed";      BOOKING_CANCELLED = "booking.cancelled"
    EXPENSE_ADDED = "expense.added"
    AGENT_TOOL_CALLED = "agent.tool_called";      AGENT_TOOL_REJECTED = "agent.tool_rejected"; AGENT_MESSAGE_SENT = "agent.message_sent"
    REMINDER_SENT = "reminder.sent"
    AUTH_OTP_VERIFIED = "auth.otp_verified";      AUTH_DENIED = "auth.denied"
    DEMO_ACTION = "demo.action"


# Which group/purchase the payment steps belong to. rules.py sets this around gateway calls so the simulated
# gateway's events (hold_authorized, ...) show up under the right group on the dashboard.
_event_context: contextvars.ContextVar = contextvars.ContextVar("event_context", default={})


@contextlib.contextmanager
def event_context(group_id=None, purchase_id=None):
    token = _event_context.set({"group_id": group_id, "purchase_id": purchase_id})
    try:
        yield
    finally:
        _event_context.reset(token)


def _hash_event(prev: str, seq: int, type: str, actor: str, group_id, purchase_id, payload: dict, simulated: bool, at) -> str:
    """The one place that defines how an event is hashed. log_event and verify_chain must both use it."""
    body = {"seq": seq, "type": type, "actor": actor, "group_id": str(group_id or ""), "purchase_id": str(purchase_id or ""),
            "payload": payload or {}, "simulated": simulated, "at": at.isoformat()}
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


@transaction.atomic
def log_event(type: str, actor: str, group_id=None, purchase_id=None, payload: dict | None = None, simulated=False):
    from core.models import EventLog
    last = EventLog.objects.select_for_update().order_by("-seq").first()
    seq = (last.seq if last else 0) + 1
    prev = last.hash if last else ""
    at = timezone.now()
    h = _hash_event(prev, seq, type, actor, group_id, purchase_id, payload or {}, simulated, at)
    row = EventLog.objects.create(group_id=group_id, purchase_id=purchase_id, actor=actor, type=type,
                                  payload=payload or {}, simulated=simulated, seq=seq, prev_hash=prev, hash=h)
    # created_at is set automatically on create, a few microseconds after `at`. Save the exact time that went
    # into the hash so verify_chain can rebuild it later.
    EventLog.objects.filter(pk=row.pk).update(created_at=at)
    row.created_at = at
    return row


def verify_chain(up_to_seq: int | None = None) -> bool:
    """Re-hash every event from the start. False means a row was changed, deleted, or inserted."""
    from core.models import EventLog
    rows = EventLog.objects.order_by("seq")
    if up_to_seq is not None:
        rows = rows.filter(seq__lte=up_to_seq)
    prev, expected_seq = "", 1
    for e in rows.iterator():
        if e.seq != expected_seq or e.prev_hash != prev:
            return False
        if e.hash != _hash_event(prev, e.seq, e.type, e.actor, e.group_id, e.purchase_id, e.payload, e.simulated, e.created_at):
            return False
        prev, expected_seq = e.hash, expected_seq + 1
    return True


def log_event_simulated(type: str, payload: dict):
    """Hook for SimulatedVisaAgentGateway. Group and purchase come from `event_context` (set by purchases.rules)."""
    ctx = payload.pop("_ctx", {}) if isinstance(payload, dict) else {}
    ctx = ctx or _event_context.get()
    log_event(type, actor="gateway", group_id=ctx.get("group_id"), purchase_id=ctx.get("purchase_id"),
              payload=payload, simulated=True)
