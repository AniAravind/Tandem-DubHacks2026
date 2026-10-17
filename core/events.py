import hashlib, json
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


@transaction.atomic
def log_event(type: str, actor: str, group_id=None, purchase_id=None, payload: dict | None = None, simulated=False):
    from core.models import EventLog
    last = EventLog.objects.select_for_update().order_by("-seq").first()
    seq = (last.seq if last else 0) + 1
    prev = last.hash if last else ""
    body = {"seq": seq, "type": type, "actor": actor, "group_id": str(group_id or ""), "purchase_id": str(purchase_id or ""),
            "payload": payload or {}, "simulated": simulated, "at": timezone.now().isoformat()}
    h = hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()
    return EventLog.objects.create(group_id=group_id, purchase_id=purchase_id, actor=actor, type=type,
                                   payload=payload or {}, simulated=simulated, seq=seq, prev_hash=prev, hash=h)


def log_event_simulated(type: str, payload: dict):
    """Hook for SimulatedVisaAgentGateway. Rules engine passes group/purchase via payload['_ctx'] when known."""
    ctx = payload.pop("_ctx", {}) if isinstance(payload, dict) else {}
    log_event(type, actor="gateway", group_id=ctx.get("group_id"), purchase_id=ctx.get("purchase_id"),
              payload=payload, simulated=True)
