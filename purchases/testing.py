"""
Test helpers shared by other people's tests (milestone M9). Nothing in production code imports this file.

assert_no_money_state_change(group_id, since_seq)
    Call `last = EventLog.objects.order_by("-seq").values_list("seq", flat=True).first() or 0` BEFORE the thing you
    are testing (for example, a prompt-injection attempt), then call this AFTER. It fails the test if any money
    moved or any money-related approval happened for that group in between:
      - any gateway event (hold, adjust, release, capture, refund, mandate, token)
      - a booking confirmed or cancelled, or the merchant accepting a checkout
      - a consent accepted
      - a purchase moving to funded / booked / completed / cancelled / refunded
      - a share moving to held / captured / refunded / released
"""
from __future__ import annotations

from core.events import EventType
from core.models import EventLog

MONEY_EVENT_TYPES = (EventType.BOOKING_CONFIRMED, EventType.BOOKING_CANCELLED, EventType.MERCHANT_ACCEPTED,
                     EventType.CONSENT_ACCEPTED)
MONEY_PURCHASE_STATES = {"funded", "booked", "completed", "cancelled", "refunded"}
MONEY_SHARE_STATES = {"held", "captured", "refunded", "released"}


def money_events_since(group_id, since_seq: int) -> list[EventLog]:
    """Every event after `since_seq` for this group that counts as a money state change."""
    found = []
    for e in EventLog.objects.filter(group_id=group_id, seq__gt=since_seq).order_by("seq"):
        if (e.type.startswith("gateway.") or e.type in MONEY_EVENT_TYPES
                or (e.type == EventType.PURCHASE_STATE_CHANGED and e.payload.get("to") in MONEY_PURCHASE_STATES)
                or (e.type == EventType.SHARE_STATE_CHANGED and e.payload.get("to") in MONEY_SHARE_STATES)):
            found.append(e)
    return found


def assert_no_money_state_change(group_id, since_seq: int) -> None:
    """Raise AssertionError (so the test fails) if any money moved for this group after event number `since_seq`."""
    found = money_events_since(group_id, since_seq)
    if found:
        summary = ", ".join(f"#{e.seq} {e.type}" + (f"->{e.payload['to']}" if "to" in e.payload else "") for e in found)
        raise AssertionError(f"Money state changed after event {since_seq}: {summary}")
