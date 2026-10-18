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
import json
import logging
import uuid
from datetime import date, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from chat.adapters.base import AgentMessage, Card
from core.events import EventType, event_context, log_event
from core.models import EventLog, Member
from payments.gateway import GatewayError, Money, get_gateway
from merchant import signing as agent_signing
from merchant.models import Listing, SignedRequest
from payments.models import AgentToken, Consent, Hold, Mandate
from purchases import consensus, fallbacks, ledger, serializers as ser, toolschema
from purchases.merchant_client import get_merchant_client, parse_signature_input
from purchases.models import Booking, Option, Purchase, Refund, Share, Vote
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
    """Shares that count toward funding: not replaced by a newer share, not declined/released/refunded,
    and the member is still active. (Somebody who said "not for me" is no longer part of the all-or-nothing.)"""
    return (Share.objects.filter(purchase=purchase, superseded_by__isnull=True, member__status=Member.Status.ACTIVE)
            .exclude(status__in=(S.DECLINED, S.RELEASED, S.REFUNDED)).select_related("member"))


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
def _post(group_id, message: AgentMessage) -> None:
    """Put a message in the group chat. Never lets a chat problem break a payment step."""
    try:
        try:
            from chat.services import post_card          # Aryan's helper, if it exists yet
            post_card(str(group_id), message)
        except ImportError:
            from chat.adapters.web import WebChatAdapter
            WebChatAdapter().send(str(group_id), message)
    except Exception:                                     # noqa: BLE001  (chat must never block money logic)
        log.exception("could not post to group %s", group_id)


def _post_group_update(purchase_id) -> None:
    purchase = Purchase.objects.select_related("option").get(pk=purchase_id)
    shares = list(_active_shares(purchase))
    held = [s for s in shares if s.status == S.HELD]
    data = {
        "purchase_id": str(purchase.id), "title": purchase.option.title, "total_cents": purchase.total_cents,
        "held_cents": sum(s.amount_cents for s in held), "deadline_at": purchase.deadline_at.isoformat(),
        "members": [{"id": str(s.member_id), "initials": s.member.display_name[:1].upper(),
                     "display_name": s.member.display_name, "status": s.status} for s in shares],
        "simulated": True}
    _post(purchase.group_id, AgentMessage(text=f"{len(held)} of {len(shares)} held (simulated)", card=Card("FundingCard", data)))


# --------------------------------------------------------------------------------------
# Checkout: book with the merchant (signed request), then capture every hold. Milestone M4.
# --------------------------------------------------------------------------------------
def _dates_label(check_in, check_out) -> str:
    """'Nov 14 to 16' (or 'Nov 30 to Dec 2'); empty when the trip has no dates."""
    try:
        a, b = date.fromisoformat(str(check_in)), date.fromisoformat(str(check_out))
    except ValueError:
        return ""
    end = f"{b.day}" if (a.year, a.month) == (b.year, b.month) else f"{b:%b} {b.day}"
    return f"{a:%b} {a.day} to {end}"


def _maybe_tamper(body: bytes) -> bytes:
    """Demo switch (milestone M8 sets it): change the price AFTER signing, so the merchant must reject the request."""
    if not cache.get("demo:tamper_next"):
        return body
    cache.delete("demo:tamper_next")
    data = json.loads(body)
    data["amount_cents"] = max(1, data["amount_cents"] - 10_000)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _signed_send(client, purchase: Purchase, slug: str, action: str, body: dict, purpose: str, extra_headers=None):
    """Sign a request, send it, and keep an audit row. Retries ONCE, and only if the merchant has a server error."""
    tag = "agent-browser-auth" if action == "quotes" else "agent-payer-auth"
    base = urlparse(settings.MERCHANT_BASE_URL)
    authority, path = base.netloc, f"{base.path.rstrip('/')}/{slug}/{action}"
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    for attempt in (1, 2):
        headers = {**agent_signing.sign_request(authority, path, raw, tag), **(extra_headers or {})}
        info = parse_signature_input(headers["Signature-Input"])
        audit = SignedRequest.objects.create(
            purpose=purpose, method="POST", authority=authority, path=path, keyid=info["keyid"], nonce=info["nonce"],
            created=info["created"], expires=info["expires"], tag=tag, content_digest=headers["Content-Digest"],
            signature_b64=headers["Signature"].split(":")[1])
        log_event(EventType.SIGNER_REQUEST_SIGNED, actor="signer", group_id=purchase.group_id, purchase_id=purchase.id,
                  payload={"purpose": purpose, "path": path, "keyid": info["keyid"], "tag": tag})
        status, data = client.send(slug, action, headers, _maybe_tamper(raw) if purpose == "checkout" else raw)
        audit.response_code, audit.verified = status, status in (200, 201)
        if status >= 400:
            audit.reject_reason = (data.get("error") or {}).get("code", "")[:32]
        audit.save(update_fields=["response_code", "verified", "reject_reason", "updated_at"])
        if status < 500 or attempt == 2:
            return status, data, audit


# Rejections that are the merchant's temporary trouble (worth trying again later), not a real "no".
TRANSIENT_REJECTIONS = ("merchant_error", "quote_failed")


def _reject(purchase: Purchase, status: int, data: dict, why: str = "") -> dict:
    reason = (data.get("error") or {}).get("code") or why or "merchant_error"
    if status >= 500:
        reason = "merchant_error"                                    # a server error is always "try again later"
    log_event(EventType.MERCHANT_REJECTED, actor="merchant", group_id=purchase.group_id, purchase_id=purchase.id,
              payload={"reason": reason, "status": status})
    return {"status": "rejected", "reason": reason}                  # nothing was captured; holds stay in place


def checkout(purchase_id) -> dict:
    """
    Book the purchase. Order matters: check every rule, ask the merchant for a fresh quote, send the SIGNED checkout,
    and only when the merchant says 201 do we capture (charge) the holds. Any rejection means no capture.
    """
    gateway, client = get_gateway(), get_merchant_client()
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().select_related("option").get(pk=purchase_id)
        if purchase.state != P.FUNDED:
            return {"status": "already_booked" if purchase.state in (P.BOOKED, P.COMPLETED) else "not_funded"}
        shares = list(_active_shares(purchase))
        # R8: every active share is held and they add up to the total.
        if not (shares and all(sh.status == S.HELD for sh in shares)
                and sum(sh.amount_cents for sh in shares) == purchase.total_cents):
            return {"status": "not_funded"}
        # R7: nothing after the deadline or after a mandate has expired.
        mandates = {sh.id: sh.mandates.filter(status=Mandate.Status.ACTIVE).order_by("-created_at").first() for sh in shares}
        if timezone.now() >= purchase.deadline_at or any(m is None or m.expires_at <= timezone.now() for m in mandates.values()):
            return _reject(purchase, 0, {}, "expired")
        # R6: merchant and listing must equal what every member agreed to.
        option = purchase.option
        if any(m.merchant_slug != option.merchant_slug or m.listing_ref != option.listing_ref for m in mandates.values()):
            return _reject(purchase, 0, {}, "merchant_lock")

        with event_context(purchase.group_id, purchase.id):
            # R9: ask for a fresh quote; the price must still equal what the group agreed to.
            dates = {"check_in": option.meta.get("check_in"), "check_out": option.meta.get("check_out")}
            status, quote, _ = _signed_send(client, purchase, option.merchant_slug, "quotes",
                                            {"listing_ref": option.listing_ref, "guests": len(shares), **dates}, "browse")
            if status != 200:
                return _reject(purchase, status, quote, "quote_failed")
            if quote["amount_cents"] != purchase.total_cents:
                return _price_changed(purchase, len(shares), quote["amount_cents"])

            body = {"quote_id": quote["quote_id"], "listing_ref": option.listing_ref, "check_in": quote.get("check_in"),
                    "check_out": quote.get("check_out"), "guests": len(shares), "amount_cents": purchase.total_cents,
                    "currency": purchase.currency,
                    "payment": {"type": "group_mandates", "mandate_ids": [m.gateway_mandate_id for m in mandates.values()],
                                "simulated": True},
                    "agent": {"name": "Tandem", "purchase_id": str(purchase.id)}}
            key = hashlib.sha256(f"{purchase.id}:checkout:{purchase.version}".encode()).hexdigest()
            status, booked, audit = _signed_send(client, purchase, option.merchant_slug, "agent/checkout", body, "checkout",
                                                 {"Idempotency-Key": key})
            if status != 201:
                return _reject(purchase, status, booked)                         # 401 / 409 / 5xx: no capture
            log_event(EventType.MERCHANT_ACCEPTED, actor="merchant", group_id=purchase.group_id, purchase_id=purchase.id,
                      payload={"booking_ref": booked["booking_ref"]})

            booking = Booking.objects.create(
                purchase=purchase, merchant_slug=option.merchant_slug, merchant_booking_ref=booked["booking_ref"],
                amount_cents=purchase.total_cents, signed_request=audit, cancellation_policy={"text": option.meta.get("cancellation", "")})
            transition_purchase(purchase.id, P.BOOKED, actor="rules", booking_ref=booking.merchant_booking_ref)
            log_event(EventType.BOOKING_CONFIRMED, actor="rules", group_id=purchase.group_id, purchase_id=purchase.id,
                      payload={"booking_ref": booking.merchant_booking_ref})
            try:
                for sh in shares:
                    _capture_share(gateway, sh, purchase)
            except GatewayError as e:                                           # R13: undo everything
                return _compensate(gateway, client, purchase, booking, shares, reason=e.code)

        _post(purchase.group_id, AgentMessage(
            text=f"Booked! Reference {booking.merchant_booking_ref} (simulated payments).",
            card=Card("BookingConfirmed", {
                "title": option.title, "dates": _dates_label(quote.get("check_in"), quote.get("check_out")),
                "booking_ref": booking.merchant_booking_ref, "per_person_cents": consensus.ceil_div(purchase.total_cents, len(shares)),
                "signature_verified": True, "simulated": True,
                "dashboard_url": f"{settings.PUBLIC_BASE_URL.rstrip('/')}/g/{purchase.group_id}"})))
        return {"status": "booked", "booking_ref": booking.merchant_booking_ref, "captured": len(shares)}


def _price_changed(purchase: Purchase, people: int, new_total: int) -> dict:
    """The merchant's price moved. Keep every hold, go back to the group, and let them vote."""
    transition_purchase(purchase.id, P.COLLECTING, actor="rules", reason="price_change", quoted_cents=new_total)
    active = len(consensus.active_members(purchase.group_id))
    vote = Vote.objects.create(
        group_id=purchase.group_id, purchase=purchase, purpose=Vote.Purpose.PRICE_CHANGE, quorum=consensus.quorum_for(active),
        closes_at=timezone.now() + timedelta(minutes=30),
        choices=[{"key": "accept", "label": "Pay the new price"}, {"key": "cancel", "label": "Cancel the purchase"}])
    log_event(EventType.VOTE_OPENED, actor="rules", group_id=purchase.group_id, purchase_id=purchase.id,
              payload={"vote_id": str(vote.id), "purpose": "price_change"})
    from purchases import tasks
    tasks.schedule(tasks.close_vote, str(vote.id), eta=vote.closes_at)
    _post(purchase.group_id, AgentMessage(
        text=f"The price changed from {ser.money(purchase.total_cents)} to {ser.money(new_total)}. Nobody was charged.",
        card=Card("VoteCard", {"vote_id": str(vote.id), "closes_at": ser.iso(vote.closes_at), "quorum": vote.quorum, "voted": 0,
                               "choices": [{"key": "accept", "label": "Pay the new price", "count": 0,
                                            "per_person_cents": consensus.ceil_div(new_total, people)},
                                           {"key": "cancel", "label": "Cancel the purchase", "count": 0, "per_person_cents": 0}]})))
    return {"status": "price_changed", "quoted_cents": new_total, "expected_cents": purchase.total_cents}


def _capture_share(gateway, share: Share, purchase: Purchase) -> None:
    """Charge one member's hold. The idempotency key means a repeat of this call can never charge twice."""
    hold = share.holds.filter(status=Hold.Status.AUTHORIZED).order_by("-created_at").first()
    resp = gateway.capture(hold.gateway_hold_id, Money(hold.amount_cents, purchase.currency),
                           _idem_key(share.id, "capture", purchase.version))
    hold.status, hold.gateway_capture_id = Hold.Status.CAPTURED, resp["capture_id"]
    hold.save(update_fields=["status", "gateway_capture_id", "updated_at"])
    share.mandates.filter(status=Mandate.Status.ACTIVE).update(status=Mandate.Status.CONSUMED)
    transition_share(share.id, S.CAPTURED, actor="rules", capture_id=resp["capture_id"])


def _compensate(gateway, client, purchase: Purchase, booking: Booking, shares: list, reason: str) -> dict:
    """R13: a capture failed after the merchant booked. Cancel the booking, refund what was charged, release the rest."""
    status, data, _ = _signed_send(client, purchase, booking.merchant_slug, "agent/cancel",
                                   {"booking_ref": booking.merchant_booking_ref}, "cancel")
    for share in Share.objects.filter(pk__in=[sh.pk for sh in shares]):
        if share.status == S.CAPTURED:
            hold = share.holds.filter(status=Hold.Status.CAPTURED).order_by("-created_at").first()
            resp = gateway.refund(hold.gateway_capture_id, Money(hold.amount_cents, purchase.currency),
                                  _idem_key(share.id, "refund", purchase.version))
            Refund.objects.create(booking=booking, share=share, amount_cents=hold.amount_cents,
                                  gateway_refund_id=resp["refund_id"])
            transition_share(share.id, S.REFUNDED, actor="rules", reason=reason)
        elif share.status == S.HELD:
            _release_share_money(gateway, share, purchase, "booking_cancelled")
            transition_share(share.id, S.RELEASED, actor="rules", reason=reason)
    booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    log_event(EventType.BOOKING_CANCELLED, actor="rules", group_id=purchase.group_id, purchase_id=purchase.id,
              payload={"booking_ref": booking.merchant_booking_ref, "reason": reason, "merchant_cancel_status": status})
    transition_purchase(purchase.id, P.CANCELLED, actor="rules", reason=reason)
    transition_purchase(purchase.id, P.REFUNDED, actor="rules", reason=reason)
    _post(purchase.group_id, AgentMessage(text="The booking could not be completed, so it was cancelled and "
                                               "everyone was refunded (simulated)."))
    return {"status": "compensated", "reason": reason}

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
        # Book only after this database transaction has committed, so a booking problem can never undo "funded".
        transaction.on_commit(lambda: _run_checkout(purchase.id))
    return {"funded": True}


def _run_checkout(purchase_id) -> None:
    from purchases import tasks
    try:
        result = checkout(purchase_id) or {}
    except Exception:                                     # noqa: BLE001  (logged; the purchase stays funded)
        log.exception("checkout failed for purchase %s", purchase_id)
        result = {"status": "rejected", "reason": "merchant_error"}
    if result.get("status") == "rejected" and result.get("reason") in TRANSIENT_REJECTIONS:
        tasks.schedule(tasks.retry_checkout, str(purchase_id), 2, countdown=tasks.retry_delay(1))


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
            _idem_key(share.id, "mandate", purchase.version, str(consent.id)))
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
        _retire_superseded_money(gateway, share, purchase, "replaced_by_new_hold")      # after a fallback: free the old hold
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


def _retire_superseded_money(gateway, share: Share, purchase: Purchase, reason: str) -> None:
    """A fallback replaces a member's share with a new one. The old share keeps its hold (status HELD) until the
    member confirms the new amount. Once they do (or back out), this lets the old one go. Caller holds the locks."""
    for old in Share.objects.select_for_update().filter(superseded_by=share, status=S.HELD).exclude(pk=share.pk):
        _release_share_money(gateway, old, purchase, reason)           # does nothing if the hold was already moved
        transition_share(old.id, S.RELEASED, actor="rules", reason=reason)


def release_all(purchase_id, reason: str) -> dict:
    """Give everyone's money back (nothing was ever charged) and end the purchase."""
    gateway = get_gateway()
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().get(pk=purchase_id)
        released = 0
        for share in Share.objects.select_for_update().filter(purchase=purchase):      # includes replaced shares still holding money
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
            with event_context(purchase.group_id, purchase.id):
                _retire_superseded_money(gateway, share, purchase, reason)      # backing out also frees a replaced share's hold
    except (RuleError, IllegalTransition) as e:
        return _error(getattr(e, "code", "illegal_state"), str(e))
    _post_group_update(purchase_id)
    transaction.on_commit(lambda: _notify_dropout(purchase_id))
    share.refresh_from_db()
    return {"share_status": share.status, "group_counts": _group_counts(purchase)}


def _notify_dropout(purchase_id) -> None:
    """Tell the chat side that somebody dropped, so the agent can propose fallbacks. Never breaks the withdrawal."""
    try:
        purchase = Purchase.objects.get(pk=purchase_id)
        if purchase.state != P.COLLECTING:
            return
        from chat.services import on_share_declined
        on_share_declined(purchase.group_id, purchase.id)
    except Exception:                                      # noqa: BLE001
        log.exception("could not ask for fallbacks on purchase %s", purchase_id)


# ======================================================================================================
# Fallbacks and share changes (milestone M6)
# ======================================================================================================
def apply_fallback(vote_id) -> dict:
    """The group voted on a fallback. Apply the winner. Safe to call more than once: the second call does nothing."""
    vote = Vote.objects.get(pk=vote_id)
    if vote.purpose != Vote.Purpose.FALLBACK or vote.purchase_id is None:
        return _error("not_a_fallback_vote", "That is not a fallback vote.")
    vote = consensus.close_if_decided(vote)
    if vote.status != Vote.Status.CLOSED:
        return _error("vote_open", "The vote is still open.")
    choice = next((c for c in vote.choices if c["key"] == vote.winner_key), None)
    if choice is None:                                      # R3: no winner (a tie or too few votes), nothing changes
        return _error("no_winner", "The vote had no winner, so nothing changed.")
    kind = choice["option_id"]

    new_shares = []
    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().select_related("option", "group").get(pk=vote.purchase_id)
        if EventLog.objects.filter(type=EventType.FALLBACK_APPLIED, purchase_id=purchase.id,
                                   payload__vote_id=str(vote.id)).exists():
            return {"status": "already_applied", "kind": kind}
        if purchase.state != P.COLLECTING:
            return _error("illegal_state", f"Purchase is {purchase.state}, not collecting.")
        remaining = fallbacks.remaining_shares(purchase)
        if not remaining:
            return _error("nobody_left", "Nobody is left in this purchase.")

        if kind == "replacement":
            purchase.deadline_at = purchase.deadline_at + fallbacks.REPLACEMENT_EXTENSION
            purchase.save(update_fields=["deadline_at", "updated_at"])
            from purchases import tasks
            tasks.schedule_purchase_jobs(purchase, _active_shares(purchase))      # new deadline, new reminders
            log_event(EventType.FALLBACK_APPLIED, actor="rules", group_id=purchase.group_id, purchase_id=purchase.id,
                      payload={"vote_id": str(vote.id), "kind": kind, "deadline_at": ser.iso(purchase.deadline_at)})
            summary = {"status": "applied", "kind": kind, "deadline_at": ser.iso(purchase.deadline_at)}
            message = "More time: the deadline moved 12 hours so someone can be invited to the empty spot."
        else:
            people = len(remaining)
            if kind == "cheaper":
                listing = Listing.objects.select_related("merchant").filter(
                    merchant__slug=choice["merchant_slug"], ref=choice["listing_ref"], available=True).first()
                if listing is None:
                    return _error("sold_out", "That listing is no longer available.")
                if listing.capacity < people:
                    return _error("over_capacity", f"{listing.title} does not sleep {people}.")
                old = purchase.option
                purchase.option = Option.objects.create(
                    group=purchase.group, merchant_slug=listing.merchant.slug, listing_ref=listing.ref, category=old.category,
                    title=listing.title, total_cents=fallbacks.listing_total(listing, people), capacity=listing.capacity,
                    features=listing.features, currency=old.currency,
                    meta={**listing.meta, **{k: v for k, v in old.meta.items() if k in ("check_in", "check_out")}})
                purchase.total_cents = purchase.option.total_cents
            if consensus.ceil_div(purchase.total_cents, people) > MAX_SHARE_CENTS:
                return _error("over_limit", "Above the $2,000 per-share demo limit.")
            purchase.version += 1
            purchase.save(update_fields=["option", "total_cents", "version", "updated_at"])
            transition_purchase(purchase.id, P.COLLECTING, actor="rules", reason="fallback_applied", vote_id=str(vote.id))

            # New equal shares; leftover cents go to the organizer (or the first member), like create_purchase.
            base, leftover = divmod(purchase.total_cents, people)
            carrier = next((s for s in remaining if s.member_id == purchase.group.organizer_id), remaining[0])
            for old_share in remaining:
                extra = leftover if old_share.id == carrier.id else 0
                # Only one current share per member is allowed, so park the old one (pointing at itself) first.
                Share.objects.filter(pk=old_share.pk).update(superseded_by=old_share)
                fresh = Share.objects.create(
                    purchase=purchase, member=old_share.member, amount_cents=base + extra, rounding_adj_cents=extra,
                    purchase_version=purchase.version, status=S.INVITED if old_share.status == S.INVITED else S.VIEWING)
                Share.objects.filter(pk=old_share.pk).update(superseded_by=fresh)
                Share.objects.filter(superseded_by=old_share).exclude(pk=old_share.pk).update(superseded_by=fresh)
                if old_share.status != S.HELD:               # a held share keeps its hold until the member confirms
                    transition_share(old_share.id, S.RELEASED, actor="rules", reason="replaced_by_fallback")
                new_shares.append(fresh)
            log_event(EventType.FALLBACK_APPLIED, actor="rules", group_id=purchase.group_id, purchase_id=purchase.id,
                      payload={"vote_id": str(vote.id), "kind": kind, "version": purchase.version,
                               "per_person_cents": base, "shares": people, "total_cents": purchase.total_cents})
            summary = {"status": "applied", "kind": kind, "version": purchase.version, "per_person_cents": base,
                       "shares": people}
            message = (f"Plan updated: {purchase.option.title}, {ser.money(base)} each for {people} people. "
                       "Everyone confirms their new amount before anything is held. Nobody is charged until all are in.")

    summary["consent_links_sent"] = _send_consent_links(new_shares) if new_shares else 0
    _post(purchase.group_id, AgentMessage(text=message))
    _post_group_update(purchase.id)
    return summary


def previous_cap_cents(share: Share):
    """The spending limit this member already approved on the share this one replaced (None if they had no hold)."""
    old = Share.objects.filter(superseded_by=share, status=S.HELD).exclude(pk=share.pk).first()
    hold = old.holds.filter(status=Hold.Status.AUTHORIZED).select_related("mandate").first() if old else None
    return hold.mandate.cap_cents if hold else None


def apply_share_change(share_id, consent_id) -> dict:
    """The member confirmed their new share amount (Ani's reconfirm endpoint calls this with the consent they gave).

    Same listing and the new amount fits under the cap they already approved: the existing hold is adjusted.
    New listing, no earlier hold, or an amount above the old cap: that needs a NEW mandate and a new hold, which
    only works if the consent carries a cap high enough (R5, R6). The old hold is released once the new one is in.
    """
    gateway = get_gateway()
    try:
        with transaction.atomic():
            purchase_id = Share.objects.values_list("purchase_id", flat=True).get(pk=share_id)
            purchase = Purchase.objects.select_for_update().select_related("option").get(pk=purchase_id)
            share = Share.objects.select_for_update().select_related("member").get(pk=share_id)
            member = share.member
            if share.status == S.HELD and not share.superseded_by_id:           # same request twice -> same answer
                return {"share_status": share.status, "amount_cents": share.amount_cents, "simulated": True,
                        "group_counts": _group_counts(purchase)}
            if member.status != Member.Status.ACTIVE or member.group_id != purchase.group_id:     # R1
                raise RuleError("not_a_member", "Only active members of this group can confirm a share.")
            if purchase.state != P.COLLECTING:                                                      # R2
                raise RuleError("illegal_state", f"Purchase is {purchase.state}, not collecting.")
            if share.superseded_by_id:
                raise RuleError("share_superseded", "This share was replaced by a newer one.")
            if share.status not in (S.INVITED, S.VIEWING, S.CARD_FAILED):
                raise RuleError("illegal_state", f"Share is {share.status}; there is nothing to confirm.")
            if timezone.now() >= purchase.deadline_at:                                              # R7
                raise RuleError("deadline_passed", "The deadline for this purchase has passed.")
            consent = Consent.objects.filter(pk=consent_id, share=share, member=member).first()     # R4
            if consent is None:
                raise RuleError("consent_required", "No consent on file for this share.")
            if consent.amount_cents != share.amount_cents:
                raise RuleError("consent_mismatch", "The consent was for a different amount.")

            old = Share.objects.select_for_update().filter(superseded_by=share, status=S.HELD).exclude(pk=share.pk).first()
            old_hold = old.holds.filter(status=Hold.Status.AUTHORIZED).select_related("mandate").first() if old else None
            option = purchase.option
            adjustable = (old_hold is not None and old_hold.mandate.merchant_slug == option.merchant_slug
                          and old_hold.mandate.listing_ref == option.listing_ref
                          and share.amount_cents <= old_hold.mandate.cap_cents)
            if adjustable:
                with event_context(purchase.group_id, purchase.id):
                    resp = gateway.adjust_hold(old_hold.gateway_hold_id, Money(share.amount_cents, purchase.currency),
                                               _idem_key(share.id, "adjust", purchase.version))
                    if resp["status"] != "AUTHORIZED":
                        raise RuleError(resp.get("reason", "hold_rejected"), "The bank would not change the hold.")
                    old_hold.share, old_hold.amount_cents = share, share.amount_cents          # the hold follows the member
                    old_hold.save(update_fields=["share", "amount_cents", "updated_at"])
                    old_hold.mandate.share = share
                    old_hold.mandate.save(update_fields=["share", "updated_at"])
                    if share.status != S.VIEWING:
                        transition_share(share.id, S.VIEWING, actor="rules", reason="reconfirm")
                    transition_share(share.id, S.HELD, actor="rules", hold_id=old_hold.gateway_hold_id, reason="reconfirmed")
                    _retire_superseded_money(gateway, share, purchase, "replaced_by_new_hold")    # old share: HELD -> RELEASED
                check_funded(purchase.id)
                share.refresh_from_db()
                result = {"share_status": share.status, "amount_cents": share.amount_cents, "hold": _hold_dict(old_hold),
                          "group_counts": _group_counts(purchase), "simulated": True}
                transaction.on_commit(lambda: _post_group_update(purchase.id))
                return result

            # A new mandate is needed, so the consent itself must allow this amount.
            if consent.cap_cents < share.amount_cents:
                raise RuleError("new_consent_required", "This amount is above the limit you approved. "
                                                        "Approve a higher limit to continue.")
            token = (AgentToken.objects.filter(member=member, status="active", expires_at__gt=timezone.now())
                     .order_by("-created_at").first())
            if token is None:
                raise RuleError("token_inactive", "The payment token is missing, revoked, or expired.")
            result = place_hold(share.id, consent.id, token.id, consent.cap_cents)            # new mandate + new hold
            if "error" in result:
                return result
            return {**result, "amount_cents": share.amount_cents, "simulated": True}
    except RuleError as e:
        return _error(e.code, e.message)
    except GatewayError as e:
        return _error(e.code, e.detail or "gateway error")


# ======================================================================================================
# RulesEngine: the single door the AI agent goes through (milestone M3)
# ======================================================================================================
MAX_CALLS_PER_MINUTE = 20             # R11: agent tool calls per group
MAX_OPEN_PURCHASES_PER_MEMBER = 2     # R12
MAX_PURCHASE_CENTS = 1_000_000        # R12: $10,000 per purchase
OPEN_PURCHASE_STATES = (P.COLLECTING, P.FUNDED)


def consent_token(share: Share) -> str:
    """Private link token for one share. Uses Ani's version once it exists, otherwise a signed stand-in."""
    try:
        from payments.services import make_consent_token          # Ani's real one
        return make_consent_token(share)
    except ImportError:
        payload = {"share_id": str(share.id), "member_id": str(share.member_id), "purchase_version": share.purchase_version}
        return signing.TimestampSigner(salt="consent").sign(json.dumps(payload, sort_keys=True))


def consent_link(share: Share) -> str:
    return f"{settings.PUBLIC_BASE_URL.rstrip('/')}/c/{consent_token(share)}"


def _when(dt, tz: str) -> str:
    """'Tue 6:00 PM' in the group's own timezone."""
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %-I:%M %p")


def _send_private(group_id, member_id, text: str, link: str | None = None) -> bool:
    """A message only ONE member can see (consent links, reminders). Never raises: chat must not break money logic."""
    try:
        from chat.services import post_card
        post_card(str(group_id), AgentMessage(text=text, link=link, private_to_member_id=str(member_id)))
        return True
    except Exception:                                                            # noqa: BLE001
        log.exception("could not send a private message to member %s", member_id)
        return False


def _send_consent_links(shares: list) -> int:
    """
    Hand each member their private link. Consent links are secrets (whoever holds one can act as that member), so
    they go only to that member as a private message. They are NEVER posted in the group chat or returned to the AI.
    Returns how many were delivered.
    """
    sent = 0
    for share in shares:
        purchase = share.purchase
        text = (f"Your share for {purchase.option.title}: {ser.money(share.amount_cents)}. "
                f"Hold it by {_when(purchase.deadline_at, purchase.group.timezone)}. "
                "It's a hold, not a charge, and you can say no on the page. (Payments are simulated.)")
        sent += _send_private(purchase.group_id, share.member_id, text, consent_link(share))
    return sent


class RulesEngine:
    """
    execute(tool, args, actor) -> {"ok": True, "data": {...}} or {"ok": False, "error": {"code", "message"}}

    `actor` is "member:<uuid>", the person whose chat message led to this call. Every call is checked against
    the argument schema in agent/tools.json, rate limited, and logged (tool name and argument NAMES only).
    """

    def __init__(self):
        self.schemas = toolschema.load_tool_schemas()
        self.tools = {
            "save_buying_brief": self._save_buying_brief,
            "publish_buying_plan": self._publish_buying_plan,
            "publish_plan": self._publish_plan,
            "search_options": self._search_options, "start_vote": self._start_vote,
            "create_purchase": self._create_purchase, "check_status": self._check_status,
            "propose_fallback": self._propose_fallback, "add_expense": self._add_expense}

    def execute(self, tool: str, args: dict, actor: str) -> dict:
        group_id = None
        try:
            if tool not in self.tools:
                raise RuleError("unknown_tool", f"There is no tool called {tool!r}.")
            problems = toolschema.validate(self.schemas[tool], args)
            if problems:
                raise RuleError("invalid_arguments", "; ".join(problems))
            member = self._actor(actor)                                   # R1
            group_id = member.group_id
            self._rate_limit(group_id)                                    # R11
            log_event(EventType.AGENT_TOOL_CALLED, actor=actor, group_id=group_id,
                      payload={"tool": tool, "arg_names": sorted(args)})  # never the values (they can include budgets)
            with transaction.atomic():
                return {"ok": True, "data": self.tools[tool](member, args)}
        except RuleError as e:
            log_event(EventType.AGENT_TOOL_REJECTED, actor=actor, group_id=group_id,
                      payload={"tool": tool, "code": e.code})
            return {"ok": False, "error": {"code": e.code, "message": e.message}}

    # ------------------------------------------------------------------ checks
    @staticmethod
    def _actor(actor: str) -> Member:
        try:
            prefix, raw = actor.split(":", 1)
            member = Member.objects.get(pk=uuid.UUID(raw))
        except (ValueError, Member.DoesNotExist):
            raise RuleError("unauthorized", "The caller is not a known member.")
        if prefix != "member" or member.status != Member.Status.ACTIVE:
            raise RuleError("unauthorized", "Only active members of the group can use Tandem.")
        return member

    @staticmethod
    def _rate_limit(group_id) -> None:
        since = timezone.now() - timedelta(minutes=1)
        recent = EventLog.objects.filter(group_id=group_id, type=EventType.AGENT_TOOL_CALLED, created_at__gte=since).count()
        if recent >= MAX_CALLS_PER_MINUTE:
            raise RuleError("rate_limited", "Too many requests this minute. Try again shortly.")

    @staticmethod
    def _own(model, pk, member: Member, what: str):
        """Fetch a row by id, but only if it belongs to the caller's group (a bad id never leaks other groups)."""
        try:
            obj = model.objects.get(pk=uuid.UUID(str(pk)))
        except (ValueError, model.DoesNotExist):
            raise RuleError("not_found", f"No such {what}.")
        if obj.group_id != member.group_id:
            raise RuleError("not_found", f"No such {what}.")
        return obj

    # ------------------------------------------------------------------ the six tools
    def _search_options(self, member: Member, args: dict) -> dict:
        members = consensus.active_members(member.group_id)
        from purchases.plans import private_planner
        if args["group_size"] != len(members) and not private_planner(member):
            raise RuleError("group_size_mismatch", f"The group has {len(members)} active members, not {args['group_size']}.")
        budget_max = args.get("budget_per_person_max")
        from chat.models import PhotonSession
        session = PhotonSession.objects.filter(member=member).first()
        brief = session.planning_state if session else {}
        ranked = consensus.rank_options(
            members, args["category"], args["group_size"], extra_must_haves=args.get("must_haves", []),
            budget_per_person_max_cents=round(budget_max * 100) if budget_max is not None else None)
        search_id = uuid.uuid4().hex
        options = []
        for rank, c in enumerate(ranked, start=1):
            listing = c.listing
            options.append(Option.objects.create(
                group_id=member.group_id, merchant_slug=listing.merchant.slug, listing_ref=listing.ref,
                category=args["category"], title=listing.title, total_cents=c.total_cents, capacity=listing.capacity,
                features=listing.features, score=c.score, explanation_facts=c.facts,
                meta={**listing.meta, "search_id": search_id, "rank": rank, "per_person_cents": c.per_person_cents,
                      "location": args.get("location", ""), "brief_revision": brief.get('revision'),
                      "group_size": args["group_size"], **self._trip_dates(args)}))
        log_event(EventType.OPTIONS_RANKED, actor="rules", group_id=member.group_id,
                  payload={"count": len(options), "top": options[0].title if options else None})
        return {"options": [ser.option_json(o) for o in options]}

    @staticmethod
    def _trip_dates(args: dict) -> dict:
        dates = args.get("dates") or {}
        return {k: v for k, v in (("check_in", dates.get("start")), ("check_out", dates.get("end"))) if v}

    def _start_vote(self, member: Member, args: dict) -> dict:
        from purchases.plans import private_planner
        if private_planner(member):
            raise RuleError('publish_plan_required', 'This is a private planning conversation. Agree on an option, then use publish_plan to create the group link; people join and accept on the website.')
        ids = args["option_ids"]
        if len(set(ids)) != len(ids):
            raise RuleError("invalid_arguments", "option_ids must be different from each other.")
        options = [self._own(Option, oid, member, "option") for oid in ids]
        active = len(consensus.active_members(member.group_id))
        vote = Vote.objects.create(
            group_id=member.group_id, purpose=Vote.Purpose.PICK_OPTION, quorum=consensus.quorum_for(active),
            closes_at=timezone.now() + timedelta(minutes=args.get("closes_in_minutes", 30)),
            choices=[{"key": str(i), "option_id": str(o.id), "label": o.title} for i, o in enumerate(options, start=1)])
        log_event(EventType.VOTE_OPENED, actor=f"member:{member.id}", group_id=member.group_id,
                  payload={"vote_id": str(vote.id), "choices": len(options)})
        from purchases import tasks
        tasks.schedule(tasks.close_vote, str(vote.id), eta=vote.closes_at)
        return {"vote_id": str(vote.id), "closes_at": ser.iso(vote.closes_at), "quorum": vote.quorum,
                "choices": [{"key": c["key"], "label": c["label"]} for c in vote.choices]}

    def _create_purchase(self, member: Member, args: dict) -> dict:
        from purchases.models import TripPlan
        from purchases.plans import private_planner
        if private_planner(member):
            raise RuleError('publish_plan_required', 'Publish the shared plan first. Payments happen on the website after participants join.')
        plan = TripPlan.objects.filter(group_id=member.group_id).first()
        if plan:
            if not plan.selected_option_id or not plan.vote_id:
                raise RuleError('quote_required', 'This decision has no supported merchant offer yet. No payment can be collected.')
            participants = {str(i) for i in member.group.members.filter(status='active').values_list('id', flat=True)}
            if (member.group.organizer_id != member.id or len(participants) != plan.expected_members
                    or set(args['member_ids']) != participants or plan.vote.ballots.count() != plan.expected_members):
                raise RuleError('roster_not_ready', 'The organizer can open payments for the complete roster once everyone has joined and accepted the plan.')
        option = self._own(Option, args["option_id"], member, "option")
        # R3: only the option that WON a closed vote can be bought.
        won = False
        for vote in Vote.objects.filter(group_id=member.group_id, purpose=Vote.Purpose.PICK_OPTION):
            vote = consensus.close_if_decided(vote)
            winner = next((c for c in vote.choices if c["key"] == vote.winner_key), None)
            if vote.status == Vote.Status.CLOSED and winner and winner["option_id"] == str(option.id):
                won = True
        if not won:
            raise RuleError("vote_required", "That option has not won a group vote yet.")
        if Purchase.objects.filter(group_id=member.group_id, option=option, state__in=OPEN_PURCHASE_STATES).exists():
            raise RuleError("purchase_exists", "A purchase for this option is already open.")

        ids = list(dict.fromkeys(args["member_ids"]))
        buyers = [self._own(Member, i, member, "member") for i in ids]
        minimum = 1 if plan and plan.expected_members == 1 else 2
        if len(buyers) < minimum or any(b.status != Member.Status.ACTIVE for b in buyers):
            raise RuleError("invalid_arguments", f"Choose at least {minimum} active members.")
        if len(buyers) > option.capacity:
            raise RuleError("over_capacity", f"This option holds {option.capacity} people, not {len(buyers)}.")
        total = option.total_cents
        if total > MAX_PURCHASE_CENTS or ceil_split(total, len(buyers)) > MAX_SHARE_CENTS:      # R12
            raise RuleError("over_limit", "Above the demo limits ($2,000 per share, $10,000 per purchase).")
        for b in buyers:
            open_now = Share.objects.filter(member=b, superseded_by__isnull=True, purchase__state__in=OPEN_PURCHASE_STATES,
                                            status__in=[S.INVITED, S.VIEWING, S.HELD, S.CARD_FAILED]).count()
            if open_now >= MAX_OPEN_PURCHASES_PER_MEMBER:
                raise RuleError("too_many_open_purchases", f"{b.display_name} is already in {open_now} open purchases.")

        purchase = Purchase.objects.create(group_id=member.group_id, option=option, total_cents=total,
                                           currency=option.currency, deadline_at=timezone.now() + timedelta(hours=args["deadline_hours"]))
        # Equal shares. Any leftover cents go to the organizer, recorded as rounding_adj_cents.
        base, leftover = divmod(total, len(buyers))
        organizer_id = member.group.organizer_id
        carrier = next((b for b in buyers if b.id == organizer_id), buyers[0])
        shares = [Share.objects.create(purchase=purchase, member=b, amount_cents=base + (leftover if b == carrier else 0),
                                       rounding_adj_cents=leftover if b == carrier else 0) for b in buyers]
        log_event(EventType.PURCHASE_CREATED, actor=f"member:{member.id}", group_id=member.group_id, purchase_id=purchase.id,
                  payload={"option": option.title, "shares": len(shares), "total_cents": total})
        transition_purchase(purchase.id, P.VOTING, actor="rules", reason="vote_won")
        transition_purchase(purchase.id, P.COLLECTING, actor="rules", reason="shares_created")
        sent = _send_consent_links(shares)
        from purchases import tasks
        tasks.schedule_purchase_jobs(purchase, shares)
        return {"purchase_id": str(purchase.id), "deadline_at": ser.iso(purchase.deadline_at),
                "shares_created": len(shares), "consent_links_sent": sent}

    def _publish_plan(self, member: Member, args: dict) -> dict:
        from purchases.plans import publish
        return publish(member, args)

    def _save_buying_brief(self, member, args):
        from purchases.briefs import save
        return save(member, args)

    def _publish_buying_plan(self, member, args):
        from purchases.briefs import publish
        return publish(member)

    def _check_status(self, member: Member, args: dict) -> dict:
        if "purchase_id" in args:
            purchase = self._own(Purchase, args["purchase_id"], member, "purchase")
        else:
            purchase = Purchase.objects.filter(group_id=member.group_id).order_by("-created_at").first()
            if purchase is None:
                raise RuleError("not_found", "There is no purchase yet.")
        full = ser.purchase_json(purchase)
        # counts only: no member names, no individual statuses, nothing private
        return {"purchase_id": full["id"], "state": full["state"], "total_cents": full["total_cents"],
                "held_cents": full["held_cents"], "deadline_at": full["deadline_at"], **full["counts"]}

    def _propose_fallback(self, member: Member, args: dict) -> dict:
        purchase = self._own(Purchase, args["purchase_id"], member, "purchase")
        try:
            return fallbacks.propose(purchase, args["reason"], actor=f"member:{member.id}")
        except fallbacks.FallbackError as e:
            raise RuleError(e.code, e.message)

    def _add_expense(self, member: Member, args: dict) -> dict:
        """The agent talks in dollars; the ledger works in whole cents."""
        split = args["split"]
        custom = split.get("custom_amounts")
        try:
            result = ledger.add_expense(
                member.group, args["payer_id"], round(args["amount"] * 100), args.get("description", ""), split["mode"],
                split.get("member_ids"), {k: round(v * 100) for k, v in custom.items()} if custom else None,
                actor=f"member:{member.id}")
        except ledger.LedgerError as e:
            raise RuleError(e.code, e.message)
        return {k: result[k] for k in ("expense_id", "description", "amount_cents", "per_person_cents", "split_count")}


def ceil_split(total: int, people: int) -> int:
    return consensus.ceil_div(total, people)
