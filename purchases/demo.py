"""
Backstage controls for the live demo (milestone M8). Reached through  POST /api/v1/demo/<action>  (see api.demo_action,
which checks DEMO_MODE=1 and the X-Demo-Key header before anything here runs).

  simulate_decline  Jordan says "not for me" through the SAME code path as the consent page's Decline button
  tamper_next       the next signed checkout has its price lowered AFTER signing, so the merchant must refuse it
  replay_last       the next signed request is sent twice, so the merchant must refuse the repeat (replay protection)
  fast_forward      run everything that is due right now (optionally jump the deadline to now first)
  reset_seed        wipe the demo group's activity and load the original seed data again, with the same ids

tamper_next and replay_last only set a switch in the cache. The signing code reads the switch (purchases/rules.py
for tamper_next, Rishab's merchant hooks for replay_last).
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from core.events import EventType, _hash_event, log_event
from core.models import EventLog, Group, InviteLink, Member
from chat.models import ChatMessage
from merchant.models import MerchantBooking, Quote, SignedRequest
from payments.models import AgentToken, Consent, Hold, IdempotencyRecord, Mandate
from purchases.models import Booking, Expense, Option, Purchase, Refund, Share, Vote

FLAG_TTL_SECONDS = 15 * 60                        # a forgotten switch turns itself off after 15 minutes
FLAGS = {"tamper_next": "demo:tamper_next", "replay_last": "demo:replay_last"}
DECLINER = "Jordan"
SEED_GROUP_NAME = "Tahoe weekend"


class DemoError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def demo_group() -> Group:
    group = Group.objects.filter(name=SEED_GROUP_NAME).order_by("created_at").first()
    if group is None:
        raise DemoError("no_demo_group", "Run `python manage.py seed_demo` first.", 404)
    return group


def run(action: str, body: dict) -> dict:
    handlers = {"simulate_decline": simulate_decline, "tamper_next": lambda b: set_flag("tamper_next"),
                "replay_last": lambda b: set_flag("replay_last"), "fast_forward": fast_forward, "reset_seed": reset_seed}
    if action not in handlers:
        raise DemoError("unknown_action", f"Unknown demo action. Try one of: {', '.join(handlers)}.", 404)
    result = handlers[action](body)
    if action != "reset_seed":                    # a reset wipes the log and writes its own event
        group = demo_group()
        log_event(EventType.DEMO_ACTION, actor="demo", group_id=group.id, payload={"action": action})
    return result


def set_flag(name: str) -> dict:
    cache.set(FLAGS[name], True, FLAG_TTL_SECONDS)
    return {"armed": name, "expires_in_seconds": FLAG_TTL_SECONDS}


def simulate_decline(body: dict) -> dict:
    """Jordan declines their open share. Goes through ConsentService.decline, exactly like the consent page."""
    from payments.services import ConsentService, make_consent_token
    group = demo_group()
    purchase = (Purchase.objects.filter(group=group, state=Purchase.State.COLLECTING).order_by("-created_at").first())
    if purchase is None:
        raise DemoError("no_open_purchase", "There is no purchase collecting holds right now.")
    share = (Share.objects.select_related("purchase", "member")
             .filter(purchase=purchase, member__display_name=DECLINER, superseded_by__isnull=True).first())
    if share is None:
        raise DemoError("no_share", f"{DECLINER} is not in this purchase.")
    result = ConsentService().decline(make_consent_token(share), "prefer_not_to_say", header_member_id=str(share.member_id))
    return {"member": DECLINER, "purchase_id": str(purchase.id), **result}


def fast_forward(body: dict) -> dict:
    """Run every job that is due. With {"to_deadline": true}, first pretend the open purchase's deadline just arrived."""
    from purchases import tasks
    group = demo_group()
    moved = 0
    if body.get("to_deadline"):
        moved = Purchase.objects.filter(group=group, state=Purchase.State.COLLECTING).update(
            deadline_at=timezone.now() - timedelta(seconds=1))
    return {"deadline_moved": moved, "ran": tasks.sweep()}


def rechain_events() -> None:
    """The event log is ONE hash chain across all groups. After deleting rows, number and re-hash what is left so it still verifies."""
    prev, rows = "", []
    for seq, e in enumerate(EventLog.objects.order_by("seq"), start=1):
        e.seq, e.prev_hash = seq, prev
        e.hash = _hash_event(prev, seq, e.type, e.actor, e.group_id, e.purchase_id, e.payload, e.simulated, e.created_at)
        prev = e.hash
        rows.append(e)
    EventLog.objects.bulk_update(rows, ["seq", "prev_hash", "hash"], batch_size=500)


@transaction.atomic
def reset_seed(body: dict | None = None) -> dict:
    """Delete everything the demo group did, then load the seed data again. Same ids as before (they are derived from names)."""
    from payments.gateway import get_gateway
    group = demo_group()
    # Order matters: rows that other rows point at (with PROTECT) can only go after the rows pointing at them.
    purchases = Purchase.objects.filter(group=group)
    Refund.objects.filter(booking__purchase__in=purchases).delete()
    Hold.objects.filter(share__purchase__in=purchases).delete()
    Mandate.objects.filter(share__purchase__in=purchases).delete()
    Consent.objects.filter(share__purchase__in=purchases).delete()
    Booking.objects.filter(purchase__in=purchases).delete()
    MerchantBooking.objects.all().delete()                    # the pretend merchants only exist for the demo
    Quote.objects.all().delete()
    SignedRequest.objects.all().delete()
    Expense.objects.filter(group=group).delete()
    Vote.objects.filter(group=group).delete()
    # After a fallback, old shares point at their replacements. Deleting a replacement first would un-supersede
    # the old share and briefly give one member two active shares (a unique-constraint error), so go oldest first.
    shares = Share.objects.filter(purchase__in=purchases)
    while True:
        replaced = shares.filter(superseded_by__isnull=False)
        oldest = replaced.exclude(id__in=replaced.values("superseded_by"))
        if not oldest.exists():
            break
        oldest.delete()
    purchases.delete()                                        # the remaining shares go with them
    Option.objects.filter(group=group).delete()
    ChatMessage.objects.filter(group=group).delete()
    InviteLink.objects.filter(group=group).delete()
    AgentToken.objects.filter(member__group=group).delete()
    IdempotencyRecord.objects.all().delete()
    EventLog.objects.filter(group=group).delete()
    rechain_events()

    from core.management.commands.seed_demo import MEMBERS, uid
    Member.objects.filter(group=group).exclude(id__in=[uid(f"member:{m[0]}") for m in MEMBERS]).delete()
    call_command("seed_demo", stdout=StringIO())              # puts members, merchants and listings back exactly as seeded

    for key in FLAGS.values():
        cache.delete(key)
    gateway = get_gateway()                                   # forget the pretend bank's memory of deleted holds
    for store in (gateway.tokens, gateway.mandates, gateway.holds, gateway.captures, gateway._idem):
        store.clear()
    log_event(EventType.DEMO_ACTION, actor="demo", group_id=group.id, payload={"action": "reset_seed"})
    return {"group_id": str(group.id), "members": Member.objects.filter(group=group).count()}
