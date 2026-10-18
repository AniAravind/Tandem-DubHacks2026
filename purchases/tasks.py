"""
Background jobs (milestone M7). They run in the Celery worker.

  close_vote(vote_id)           a vote's time is up (or it was decided early): close it, announce it, apply a fallback
  send_reminder(share_id, tier) nudge someone who has not approved yet (12 hours, 2 hours, 30 minutes before the deadline)
  expire_purchase(purchase_id)  deadline passed and not funded: release every hold, nobody is charged
  refresh_hold(hold_id)         a bank hold is about to expire while the purchase is still open: replace it
  retry_checkout(purchase_id)   the merchant had a temporary problem: try booking again (backoff, at most 3 times)
  sweep()                       runs every minute and does whatever above is due, in case a queued task was lost

Three rules every task follows, so running one twice (or too early, or after things changed) is always safe:
  1. It re-reads the row under select_for_update() and does nothing if the state has already moved on.
  2. It checks the clock itself instead of trusting that it was queued for the right time.
  3. It records what it did (events), so a repeat can tell it already did it.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from chat.adapters.base import AgentMessage
from core.events import EventType, event_context, log_event
from core.models import EventLog, Member
from payments.gateway import Money, get_gateway
from payments.models import Hold, Mandate
from purchases import consensus, rules, serializers as ser
from purchases.models import Purchase, Share, Vote

log = logging.getLogger(__name__)
S = Share.Status
P = Purchase.State

# ---- reminders
TIERS = (("12h", timedelta(hours=12)), ("2h", timedelta(hours=2)), ("30m", timedelta(minutes=30)))   # least urgent first
TIER_WINDOW = dict(TIERS)
TIER_TEXT = {"12h": "About 12 hours", "2h": "About 2 hours", "30m": "About 30 minutes"}
MAX_REMINDERS = 3
QUIET_START_HOUR, QUIET_END_HOUR = 23, 8          # no reminders from 11 PM until 8 AM, group-local time
EARLY_SLACK = timedelta(minutes=5)                # a reminder may run this much before its exact time
NEEDS_APPROVAL = (S.INVITED, S.VIEWING, S.CARD_FAILED)

# ---- holds and checkout
REFRESH_WINDOW = timedelta(days=1)                # replace a hold this long before the bank would drop it
MAX_CHECKOUT_RETRIES = 3
RETRY_BASE_SECONDS = 30                           # 30s, then 60s, then 120s


def _now():
    return timezone.now()                         # one place to change the clock in tests


# =====================================================================================================
# Queueing
# =====================================================================================================
def _enqueue(task, args, eta=None, countdown=None) -> None:
    """Hand a task to the queue. Never raises: if the queue is down the once-a-minute sweep still catches up."""
    try:
        task.apply_async(args=args, eta=eta, countdown=countdown, retry=False)
    except Exception:                                                   # noqa: BLE001
        log.warning("could not queue %s; the sweep will pick it up", task.name)


def schedule(task, *args, eta=None, countdown=None) -> None:
    """Queue a task, but only once the current database transaction has committed (so the task can see the data)."""
    transaction.on_commit(lambda: _enqueue(task, args, eta, countdown))


def retry_delay(attempt: int) -> int:
    return RETRY_BASE_SECONDS * 2 ** (attempt - 1)


def schedule_purchase_jobs(purchase: Purchase, shares) -> None:
    """When a purchase opens or its plan changes: queue the deadline check and each person's reminders."""
    schedule(expire_purchase, str(purchase.id), eta=purchase.deadline_at)
    now = _now()
    for share in shares:
        for tier, window in TIERS:
            when = purchase.deadline_at - window
            if when > now:
                schedule(send_reminder, str(share.id), tier, eta=when)


def schedule_hold_refresh(hold: Hold, purchase: Purchase) -> None:
    """Queue a refresh only when the hold would expire before the purchase itself ends (rare with short deadlines)."""
    if hold.expires_at is None:
        return
    when = hold.expires_at - REFRESH_WINDOW
    if when < purchase.deadline_at:
        schedule(refresh_hold, str(hold.id), eta=max(when, _now()))


# =====================================================================================================
# close_vote
# =====================================================================================================
@shared_task(ignore_result=True)
def close_vote(vote_id):
    """Close a vote whose time is up (or that is decided), announce the result, and apply a winning fallback."""
    with transaction.atomic():
        vote = Vote.objects.select_for_update().filter(pk=vote_id).first()
        if vote is None:
            return "gone"
        was_open = vote.status == Vote.Status.OPEN
        vote = consensus.close_if_decided(vote)
        if vote.status == Vote.Status.OPEN:
            return "still_open"                      # asked too early; the sweep will come back
    if vote.purpose == Vote.Purpose.FALLBACK:
        rules.apply_fallback(vote.id)                # safe to repeat: the second call says already_applied
    if was_open:
        _announce_vote(vote)
    return "closed" if was_open else "already_closed"


def _announce_vote(vote: Vote) -> None:
    winner = next((c for c in vote.choices if c["key"] == vote.winner_key), None)
    if winner is None:
        rules._post(vote.group_id, AgentMessage(text="The vote ended without a winner, so nothing changed."))
        return
    if vote.purpose == Vote.Purpose.PICK_OPTION:
        votes_for = vote.ballots.filter(choice_key=winner["key"]).count()
        others = vote.ballots.count() - votes_for
        rules._post(vote.group_id, AgentMessage(text=f"🗳️ {winner['label']} wins, {votes_for} votes to {others}."))
        try:                                         # tell the agent so it can open funding
            from chat.services import schedule_agent
            schedule_agent(vote.group_id, None, {"type": "vote_closed", "vote_id": str(vote.id),
                                                 "winner_option_id": winner["option_id"], "winner_label": winner["label"]})
        except Exception:                            # noqa: BLE001
            log.exception("could not tell the agent that vote %s closed", vote.id)
    elif vote.purpose == Vote.Purpose.PRICE_CHANGE:
        if winner["key"] == "cancel" and vote.purchase_id:
            with transaction.atomic():
                purchase = Purchase.objects.select_for_update().get(pk=vote.purchase_id)
                if purchase.state == P.COLLECTING:
                    rules.release_all(purchase.id, "price_change_cancelled")
            rules._post(vote.group_id, AgentMessage(text="The group cancelled. Nobody was charged and every hold is released."))
        else:
            rules._post(vote.group_id, AgentMessage(
                text="The group chose to pay the new price. Re-pricing is not built yet, so nothing was charged "
                     "and the holds stay in place until the deadline."))


# =====================================================================================================
# send_reminder
# =====================================================================================================
def in_quiet_hours(local) -> bool:
    return local.hour >= QUIET_START_HOUR or local.hour < QUIET_END_HOUR


def next_quiet_end(local):
    end = local.replace(hour=QUIET_END_HOUR, minute=0, second=0, microsecond=0)
    return end + timedelta(days=1) if local.hour >= QUIET_END_HOUR else end


@shared_task(ignore_result=True)
def send_reminder(share_id, tier):
    """Nudge one person who has not approved yet. Returns a short word saying what happened."""
    if tier not in TIER_WINDOW:
        return "unknown_tier"
    with transaction.atomic():
        share = (Share.objects.select_for_update(of=("self",)).select_related("member", "purchase__option", "purchase__group")
                 .filter(pk=share_id).first())
        if share is None:
            return "gone"
        purchase = share.purchase
        if (purchase.state != P.COLLECTING or share.superseded_by_id or share.status not in NEEDS_APPROVAL
                or share.member.status != Member.Status.ACTIVE):
            return "skipped"                          # funded, replaced, declined, or the purchase ended
        now = _now()
        if now < purchase.deadline_at - TIER_WINDOW[tier] - EARLY_SLACK:
            return "too_early"
        if now >= purchase.deadline_at:
            return "too_late"
        sent = EventLog.objects.filter(type=EventType.REMINDER_SENT, purchase_id=purchase.id, payload__share_id=str(share.id))
        if sent.filter(payload__tier=tier).exists():
            return "already_sent"
        if sent.count() >= MAX_REMINDERS:
            return "max_reached"
        local = now.astimezone(ZoneInfo(purchase.group.timezone))
        if in_quiet_hours(local):
            wake = next_quiet_end(local)
            if wake < purchase.deadline_at:
                schedule(send_reminder, str(share.id), tier, eta=wake)       # try again at 8 AM
                return "delayed_quiet_hours"
            return "quiet_hours"                                              # the deadline comes first: stay quiet
        counts = rules._group_counts(purchase)
        text = (f"⏰ {TIER_TEXT[tier]} left to approve your {ser.money(share.amount_cents)} share of "
                f"{purchase.option.title} ({counts['held']} of {counts['active']} are in). "
                "It's a hold, not a charge, and you can say no on the page.")
        delivered = rules._send_private(purchase.group_id, share.member_id, text, rules.consent_link(share))
        log_event(EventType.REMINDER_SENT, actor="scheduler", group_id=purchase.group_id, purchase_id=purchase.id,
                  payload={"share_id": str(share.id), "tier": tier, "delivered": delivered})
        return "sent"


# =====================================================================================================
# expire_purchase
# =====================================================================================================
@shared_task(ignore_result=True)
def expire_purchase(purchase_id):
    """Deadline passed and the purchase is not funded: release every hold. Nobody is charged."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().filter(pk=purchase_id).first()
        if purchase is None or purchase.state != P.COLLECTING:
            return "skipped"                          # funded, booked, already released, ...
        if _now() < purchase.deadline_at:
            deadline = purchase.deadline_at           # the deadline moved (a fallback gave more time)
            if not settings.CELERY_TASK_ALWAYS_EAGER:
                schedule(expire_purchase, str(purchase.id), eta=deadline)
            return "not_yet"
        rules.release_all(purchase.id, "deadline")    # collecting -> failed -> released, holds released
    rules._post(purchase.group_id, AgentMessage(text="Time's up. Nobody was charged, and every hold has been released."))
    return "expired"


# =====================================================================================================
# refresh_hold
# =====================================================================================================
@shared_task(ignore_result=True)
def refresh_hold(hold_id):
    """Replace a hold that will expire at the bank soon. The new hold is placed BEFORE the old one is released."""
    with transaction.atomic():
        hold = (Hold.objects.select_for_update(of=("self",)).select_related("share__purchase", "mandate")
                .filter(pk=hold_id).first())
        if hold is None or hold.status != Hold.Status.AUTHORIZED or hold.expires_at is None:
            return "skipped"
        share, mandate = hold.share, hold.mandate
        purchase = share.purchase
        if purchase.state not in (P.COLLECTING, P.FUNDED):
            return "skipped"
        now = _now()
        if hold.expires_at - now > REFRESH_WINDOW + timedelta(hours=1):
            return "not_yet"
        gateway = get_gateway()
        with event_context(purchase.group_id, purchase.id):
            fail = None
            if mandate.status != Mandate.Status.ACTIVE or mandate.expires_at <= now:
                fail = "mandate_expired"
            else:
                resp = gateway.authorize_hold(
                    mandate.gateway_mandate_id, Money(hold.amount_cents, purchase.currency),
                    rules._idem_key(share.id, "refresh", purchase.version, str(hold.id)), member_id=str(share.member_id))
                if resp["status"] != "AUTHORIZED":
                    fail = resp.get("reason") or resp.get("decline_code") or "not_authorized"
            if fail:
                log_event(EventType.HOLD_REFRESH_FAILED, actor="scheduler", group_id=purchase.group_id,
                          purchase_id=purchase.id, payload={"share_id": str(share.id), "reason": fail})
                return "failed"
            fresh = Hold.objects.create(share=share, mandate=mandate, gateway_hold_id=resp["hold_id"],
                                        amount_cents=hold.amount_cents, status=Hold.Status.AUTHORIZED,
                                        expires_at=parse_datetime(resp["expires_at"]))
            gateway.release_hold(hold.gateway_hold_id, "refreshed", rules._idem_key(share.id, "release-refresh", purchase.version, str(hold.id)))
        hold.status = Hold.Status.RELEASED
        hold.save(update_fields=["status", "updated_at"])
        log_event(EventType.HOLD_REFRESHED, actor="scheduler", group_id=purchase.group_id, purchase_id=purchase.id,
                  payload={"share_id": str(share.id), "old_hold": hold.gateway_hold_id, "new_hold": fresh.gateway_hold_id})
        schedule_hold_refresh(fresh, purchase)
        return "refreshed"


# =====================================================================================================
# retry_checkout
# =====================================================================================================
@shared_task(ignore_result=True)
def retry_checkout(purchase_id, attempt=1):
    """Try booking again after a temporary merchant problem. The booking request keeps the SAME idempotency key,
    so even if an earlier try secretly worked, the merchant will not book twice."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().filter(pk=purchase_id).first()
        if purchase is None or purchase.state != P.FUNDED:
            return "skipped"                          # booked, cancelled, or no longer funded
    result = rules.checkout(purchase_id)
    status, reason = result.get("status"), result.get("reason")
    if status == "rejected" and reason in rules.TRANSIENT_REJECTIONS:
        if attempt < MAX_CHECKOUT_RETRIES:
            schedule(retry_checkout, str(purchase_id), attempt + 1, countdown=retry_delay(attempt))
            return "retry_scheduled"
        rules._post(purchase.group_id, AgentMessage(
            text="The merchant is not answering right now. Every hold is still in place and nobody was charged."))
        return "gave_up"
    return status or "done"


# =====================================================================================================
# sweep (every minute)
# =====================================================================================================
@shared_task(ignore_result=True)
def sweep():
    """Do whatever is due. Catches anything the queue lost (a restart can drop scheduled tasks)."""
    now = _now()
    done = {"votes": 0, "purchases": 0, "reminders": 0, "holds": 0}
    for vid in Vote.objects.filter(status=Vote.Status.OPEN, closes_at__lte=now).values_list("id", flat=True):
        close_vote(str(vid))
        done["votes"] += 1
    for pid in Purchase.objects.filter(state=P.COLLECTING, deadline_at__lte=now).values_list("id", flat=True):
        if expire_purchase(str(pid)) == "expired":
            done["purchases"] += 1
    waiting = (Share.objects.filter(purchase__state=P.COLLECTING, purchase__deadline_at__gt=now, superseded_by__isnull=True,
                                    status__in=NEEDS_APPROVAL).select_related("purchase"))
    for share in waiting:
        passed = [tier for tier, window in TIERS if now >= share.purchase.deadline_at - window]
        if passed and send_reminder(str(share.id), passed[-1]) == "sent":        # only the most urgent tier that has arrived
            done["reminders"] += 1
    for hid in (Hold.objects.filter(status=Hold.Status.AUTHORIZED, expires_at__lte=now + REFRESH_WINDOW,
                                    share__purchase__state__in=(P.COLLECTING, P.FUNDED)).values_list("id", flat=True)):
        if refresh_hold(str(hid)) == "refreshed":
            done["holds"] += 1
    return done
