"""
purchases/rules.py: the money core (milestone M1).

This is the ONLY place in the project that places, releases, or checks holds on money.
The AI agent can only *propose* things; this file decides whether they are allowed.

Rules used here (see the brief, section "Rules"):
  R1  the member must be active in the group
  R2  the purchase must be "collecting" and the share must be "viewing" or "card_failed"
  R4  the member must have a Consent row for exactly this share and amount
  R5  share amount <= cap the member approved <= the demo hard limit
  R7  nothing happens after the purchase deadline
  R8  all-or-nothing: the purchase is "funded" only when EVERY active share is held
  R10 every gateway call carries an idempotency key (same request twice = same result)
  R11 at most 3 card declines per share
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timezone as dt_timezone

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from chat.adapters.base import AgentMessage, Card
from core.events import event_context
from core.models import Member
from payments.gateway import GatewayError, Money, get_gateway
from payments.models import AgentToken, Consent, Hold, Mandate
from purchases.models import Purchase, Share
from purchases.states import IllegalTransition, transition_purchase, transition_share

log = logging.getLogger(__name__)

P = Purchase.State
S = Share.Status

MAX_CARD_ATTEMPTS = 3                 # R11
MAX_SHARE_CENTS = 200_000             # R12: demo hard limit of $2,000 per share
MANDATE_SCOPE = ["authorize_hold", "capture", "release_hold", "refund"]


class RuleError(Exception):
    """Raised inside a rule check when an action is not allowed. Turned into {"error": ...} for callers."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _idem_key(share_id, action: str, version: int, extra: str = "") -> str:
    """R10: sha256(share_id:action:version). `extra` is only used so a retry after a decline is a NEW request."""
    raw = f"{share_id}:{action}:{version}" + (f":{extra}" if extra else "")
    return hashlib.sha256(raw.encode()).hexdigest()


def _iso_utc(dt) -> str:
    return dt.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _active_shares(purchase: Purchase):
    """Shares that count toward funding: not replaced by a newer share, and the member is still active."""
    return (Share.objects.filter(purchase=purchase, superseded_by__isnull=True, member__status=Member.Status.ACTIVE)
            .select_related("member"))


def _group_counts(purchase: Purchase) -> dict:
    shares = list(_active_shares(purchase))
    return {"held": sum(1 for s in shares if s.status == S.HELD), "active": len(shares)}


def _hold_dict(hold: Hold) -> dict:
    return {"hold_id": hold.gateway_hold_id, "status": hold.status, "amount_cents": hold.amount_cents,
            "decline_code": hold.decline_code, "expires_at": hold.expires_at.isoformat() if hold.expires_at else None,
            "simulated": True}


# --------------------------------------------------------------------------------------
# Chat updates ("3 of 6 held")
# --------------------------------------------------------------------------------------
def _post_group_update(purchase_id) -> None:
    """Tell the group chat how many shares are held. Never lets a chat problem break a payment step."""
    try:
        purchase = Purchase.objects.select_related("option").get(pk=purchase_id)
        shares = list(_active_shares(purchase))
        held = [s for s in shares if s.status == S.HELD]
        data = {
            "purchase_id": str(purchase.id), "title": purchase.option.title, "total_cents": purchase.total_cents,
            "held_cents": sum(s.amount_cents for s in held), "deadline_at": purchase.deadline_at.isoformat(),
            "members": [{"id": str(s.member_id), "initials": s.member.display_name[:1].upper(),
                         "display_name": s.member.display_name, "status": s.status} for s in shares],
            "simulated": True}
        message = AgentMessage(text=f"{len(held)} of {len(shares)} held (simulated)", card=Card("FundingCard", data))
        try:
            from chat.services import post_card          # Aryan's helper, if it exists yet
            post_card(str(purchase.group_id), message)
        except ImportError:
            from chat.adapters.web import WebChatAdapter
            WebChatAdapter().send(str(purchase.group_id), message)
    except Exception:                                     # noqa: BLE001  (chat must never block money logic)
        log.exception("could not post group update for purchase %s", purchase_id)


# --------------------------------------------------------------------------------------
# Checkout: filled in by milestone M4. For now it only exists so check_funded has something to call.
# --------------------------------------------------------------------------------------
def checkout(purchase_id) -> None:
    raise NotImplementedError("checkout is built in milestone M4")


# --------------------------------------------------------------------------------------
# check_funded
# --------------------------------------------------------------------------------------
def check_funded(purchase_id) -> dict:
    """R8 all-or-nothing. If every active share is held and they add up to the total, mark the purchase funded."""
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        if purchase.state != P.COLLECTING:
            return {"funded": False, "reason": f"purchase is {purchase.state}"}
        shares = list(_active_shares(purchase))
        all_held = bool(shares) and all(s.status == S.HELD for s in shares)
        total_ok = sum(s.amount_cents for s in shares) == purchase.total_cents
        if not (all_held and total_ok):
            return {"funded": False, "reason": "not every share is held yet"}
        transition_purchase(purchase.id, P.FUNDED, actor="rules", reason="all_shares_held")

    # Funded is saved. Now try to book. Checkout does not exist until M4, so that is caught and logged.
    try:
        checkout(purchase_id)
    except NotImplementedError:
        log.info("purchase %s funded; checkout not built yet (M4)", purchase_id)
    return {"funded": True}


# --------------------------------------------------------------------------------------
# place_hold
# --------------------------------------------------------------------------------------
def place_hold(share_id, consent_id, agent_token_id, cap_cents: int) -> dict:
    """Place a hold for ONE member's share. Returns a result dict, or {"error": {...}}."""
    try:
        result = _place_hold(share_id, consent_id, agent_token_id, cap_cents)
    except RuleError as e:
        return _error(e.code, e.message)
    except GatewayError as e:
        return _error(e.code, e.detail or "gateway error")
    _post_group_update(result["_purchase_id"])
    result.pop("_purchase_id")
    return result


def _place_hold(share_id, consent_id, agent_token_id, cap_cents: int) -> dict:
    gateway = get_gateway()

    # Lock order is always Purchase first, then Share, so two approvals can never deadlock each other.
    purchase_id = Share.objects.values_list("purchase_id", flat=True).get(pk=share_id)

    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().select_related("option").get(pk=purchase_id)
        share = Share.objects.select_for_update().select_related("member").get(pk=share_id)
        member = share.member
        with event_context(purchase.group_id, purchase.id):      # so gateway events land under this group
            return _place_hold_locked(gateway, purchase, share, member, consent_id, agent_token_id, cap_cents)


def _place_hold_locked(gateway, purchase, share, member, consent_id, agent_token_id, cap_cents) -> dict:
    """The body of place_hold. Runs inside the database transaction and holds the Purchase and Share locks."""
    # Same request twice -> same answer, no second hold.
    if share.status == S.HELD:
        hold = share.holds.filter(status=Hold.Status.AUTHORIZED).order_by("-created_at").first()
        mandate = hold.mandate if hold else None
        return {"share_status": share.status, "hold": _hold_dict(hold) if hold else None,
                "mandate_id": mandate.gateway_mandate_id if mandate else None,
                "group_counts": _group_counts(purchase), "_purchase_id": purchase.id}

    # R1: the member is active and belongs to this purchase's group.
    if member.status != Member.Status.ACTIVE or member.group_id != purchase.group_id:
        raise RuleError("not_a_member", "Only active members of this group can place a hold.")
    # R2: legal states.
    if purchase.state != P.COLLECTING:
        raise RuleError("illegal_state", f"Purchase is {purchase.state}, not collecting.")
    if share.status not in (S.VIEWING, S.CARD_FAILED):
        raise RuleError("illegal_state", f"Share is {share.status}; it must be viewing or card_failed.")
    if share.superseded_by_id:
        raise RuleError("share_superseded", "This share was replaced by a newer one.")
    # R11: max 3 card declines.
    if share.card_attempts >= MAX_CARD_ATTEMPTS:
        raise RuleError("too_many_declines", "This card was declined 3 times. Use a different card.")
    # R7: before the deadline.
    if timezone.now() >= purchase.deadline_at:
        raise RuleError("deadline_passed", "The deadline for this purchase has passed.")
    # R4: consent exists for exactly this share and amount.
    consent = Consent.objects.filter(pk=consent_id, share=share, member=member).first()
    if consent is None:
        raise RuleError("consent_required", "No consent on file for this share.")
    if consent.amount_cents != share.amount_cents:
        raise RuleError("consent_mismatch", "The consent was for a different amount.")
    # R5 and R12: share <= cap <= hard limit.
    if cap_cents < share.amount_cents:
        raise RuleError("cap_exceeded", "Your share is higher than the cap you approved.")
    if cap_cents > MAX_SHARE_CENTS or share.amount_cents > MAX_SHARE_CENTS:
        raise RuleError("over_limit", "Above the $2,000 per-share demo limit.")
    # The agent token must belong to this member and still be usable.
    token = AgentToken.objects.filter(pk=agent_token_id, member=member).first()
    if token is None or token.status != "active" or token.expires_at <= timezone.now():
        raise RuleError("token_inactive", "The payment token is missing, revoked, or expired.")

    option = purchase.option
    money_cap = Money(cap_cents, purchase.currency)

    # Reuse this consent's mandate if a previous attempt already created one (retry after a decline).
    mandate = Mandate.objects.filter(consent=consent, status=Mandate.Status.ACTIVE).first()
    if mandate is None:
        created = gateway.create_purchase_mandate(
            token.gateway_token_id, money_cap, option.merchant_slug, option.listing_ref,
            _iso_utc(purchase.deadline_at), str(consent.id),
            _idem_key(share.id, "mandate", purchase.version))
        mandate = Mandate.objects.create(
            share=share, member=member, agent_token=token, consent=consent, merchant_slug=option.merchant_slug,
            listing_ref=option.listing_ref, cap_cents=cap_cents, currency=purchase.currency,
            expires_at=purchase.deadline_at, scope=MANDATE_SCOPE, status=Mandate.Status.ACTIVE,
            gateway_mandate_id=created["mandate_id"])

    # Ask the (simulated) bank for a hold. A retry after a decline gets a fresh key.
    attempt = str(share.card_attempts) if share.card_attempts else ""
    resp = gateway.authorize_hold(mandate.gateway_mandate_id, Money(share.amount_cents, purchase.currency),
                                  _idem_key(share.id, "hold", purchase.version, attempt), member_id=str(member.id))

    if resp["status"] == "AUTHORIZED":
        hold = Hold.objects.create(
            share=share, mandate=mandate, gateway_hold_id=resp["hold_id"], amount_cents=share.amount_cents,
            status=Hold.Status.AUTHORIZED, expires_at=parse_datetime(resp["expires_at"]))
        if share.status == S.CARD_FAILED:
            transition_share(share.id, S.VIEWING, actor="rules", reason="card_retry")
        transition_share(share.id, S.HELD, actor="rules", hold_id=hold.gateway_hold_id)
        check_funded(purchase.id)                       # may flip the whole purchase to funded
        share.refresh_from_db()
        return {"share_status": share.status, "hold": _hold_dict(hold),
                "mandate_id": mandate.gateway_mandate_id, "group_counts": _group_counts(purchase),
                "_purchase_id": purchase.id}

    if resp["status"] == "DECLINED":
        hold = Hold.objects.create(
            share=share, mandate=mandate, gateway_hold_id=f"declined_{share.id}_{share.card_attempts + 1}",
            amount_cents=share.amount_cents, status=Hold.Status.DECLINED, decline_code=resp["decline_code"])
        share.card_attempts += 1
        share.save(update_fields=["card_attempts", "updated_at"])
        if share.status != S.CARD_FAILED:
            transition_share(share.id, S.CARD_FAILED, actor="rules", decline_code=resp["decline_code"])
        return {"share_status": S.CARD_FAILED, "hold": _hold_dict(hold), "mandate_id": mandate.gateway_mandate_id,
                "group_counts": _group_counts(purchase), "_purchase_id": purchase.id}

    # status REJECTED (mandate expired, cap exceeded at the bank, ...)
    raise RuleError(resp.get("reason", "hold_rejected"), resp.get("detail", "The hold was rejected."))


# --------------------------------------------------------------------------------------
# Releasing money
# --------------------------------------------------------------------------------------
def _release_share_money(gateway, share: Share, purchase: Purchase, reason: str) -> None:
    """Release the authorized hold (if any) and revoke the mandate for one share. Caller holds the DB locks."""
    hold = share.holds.filter(status=Hold.Status.AUTHORIZED).order_by("-created_at").first()
    with event_context(purchase.group_id, purchase.id):          # so gateway events land under this group
        if hold:
            gateway.release_hold(hold.gateway_hold_id, reason, _idem_key(share.id, "release", purchase.version))
            hold.status = Hold.Status.RELEASED
            hold.save(update_fields=["status", "updated_at"])
        for mandate in share.mandates.filter(status=Mandate.Status.ACTIVE):
            gateway.revoke_mandate(mandate.gateway_mandate_id, reason)
            mandate.status = Mandate.Status.REVOKED
            mandate.save(update_fields=["status", "updated_at"])


def release_all(purchase_id, reason: str) -> dict:
    """Give everyone's money back (nothing was ever charged) and end the purchase."""
    gateway = get_gateway()
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        released = 0
        for share in Share.objects.select_for_update().filter(purchase=purchase, superseded_by__isnull=True):
            if share.status == S.HELD:
                _release_share_money(gateway, share, purchase, reason)
                released += 1
            if share.status in (S.HELD, S.INVITED, S.VIEWING, S.CARD_FAILED):
                transition_share(share.id, S.RELEASED, actor="rules", reason=reason)
        if purchase.state == P.COLLECTING:
            transition_purchase(purchase.id, P.FAILED, actor="rules", reason=reason)
            transition_purchase(purchase.id, P.RELEASED, actor="rules", reason=reason)
    _post_group_update(purchase_id)
    return {"purchase_state": Purchase.objects.get(pk=purchase_id).state, "holds_released": released}


def member_withdraw(share_id, reason: str) -> dict:
    """One member backs out ("Not for me"). Their own hold is released; everyone else's stays put."""
    gateway = get_gateway()
    purchase_id = Share.objects.values_list("purchase_id", flat=True).get(pk=share_id)
    try:
        with transaction.atomic():
            purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
            share = Share.objects.select_for_update().get(pk=share_id)
            if share.status == S.HELD:
                _release_share_money(gateway, share, purchase, reason)
                transition_share(share.id, S.RELEASED, actor="rules", reason=reason)
            elif share.status in (S.INVITED, S.VIEWING, S.CARD_FAILED):
                transition_share(share.id, S.DECLINED, actor="rules", reason=reason)
            else:
                raise RuleError("illegal_state", f"A {share.status} share cannot be withdrawn.")
    except (RuleError, IllegalTransition) as e:
        return _error(getattr(e, "code", "illegal_state"), str(e))
    _post_group_update(purchase_id)
    share.refresh_from_db()
    return {"share_status": share.status, "group_counts": _group_counts(purchase)}
