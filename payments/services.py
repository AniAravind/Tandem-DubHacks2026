from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.conf import settings
from django.core.signing import BadSignature, TimestampSigner
from django.utils import timezone

from core.events import EventType, event_context, log_event
from merchant.models import Merchant
from payments.consent_text import VERSION, can_lines, can_never_lines, format_deadline, format_usd, render
from payments.gateway import get_gateway
from payments.models import AgentToken, Consent, IdempotencyRecord
from purchases.models import Share
from purchases.states import S, transition_share

log = logging.getLogger(__name__)

DECLINE_REASONS = frozenset({"too_expensive", "dates", "other", "prefer_not_to_say"})
PAN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
TRANSIENT_RE = re.compile(r"^tt_sim_\d{4}_[A-Za-z0-9]+$")
APPROVE_LIMIT_PER_HOUR = 5
MAX_CARD_DECLINES = 3
DEMO_OTP = "000000"


class ConsentError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra or {}


def _signer() -> TimestampSigner:
    return TimestampSigner(salt="consent")


def _idem(share_id, action: str, version: int) -> str:
    return hashlib.sha256(f"{share_id}:{action}:{version}".encode()).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def make_consent_token(share) -> str:
    purchase = share.purchase
    return _signer().sign_object({
        "share_id": str(share.id),
        "member_id": str(share.member_id),
        "purchase_version": purchase.version,
        "deadline_ts": purchase.deadline_at.timestamp(),
    })


def read_consent_token(token: str, header_member_id: str | None = None) -> dict:
    try:
        payload = _signer().unsign_object(token)
    except BadSignature as exc:
        raise ConsentError("link_expired", "This link expired. Ask Tandem in your chat for a new one.", 410) from exc
    deadline_ts = float(payload.get("deadline_ts") or 0)
    if deadline_ts and time.time() > deadline_ts:
        raise ConsentError("link_expired", "This link expired. Ask Tandem in your chat for a new one.", 410)
    try:
        share = Share.objects.select_related(
            "member", "purchase", "purchase__option", "purchase__group", "purchase__group__organizer",
        ).get(pk=payload["share_id"])
    except (Share.DoesNotExist, KeyError, ValueError) as exc:
        raise ConsentError("link_expired", "This link expired. Ask Tandem in your chat for a new one.", 410) from exc
    if str(share.member_id) != str(payload.get("member_id")):
        log_event(EventType.AUTH_DENIED, actor=f"member:{payload.get('member_id')}",
                  group_id=share.purchase.group_id, purchase_id=share.purchase_id,
                  payload={"reason": "wrong_member"})
        raise ConsentError("auth_denied", "This link isn't for you. Links are personal.", 403)
    if share.purchase.version != payload.get("purchase_version") or share.purchase_version != payload.get("purchase_version"):
        raise ConsentError("link_superseded", "This link expired. Ask Tandem in your chat for a new one.", 410)
    if timezone.now() >= share.purchase.deadline_at:
        raise ConsentError("link_expired", "This link expired. Ask Tandem in your chat for a new one.", 410)
    if header_member_id and str(share.member_id) != str(header_member_id):
        log_event(EventType.AUTH_DENIED, actor=f"member:{header_member_id}",
                  group_id=share.purchase.group_id, purchase_id=share.purchase_id,
                  payload={"reason": "wrong_member"})
        raise ConsentError("auth_denied", "This link isn't for you. Links are personal.", 403)
    return {"share": share, "payload": payload}


def assert_not_pan(raw: str) -> None:
    if PAN_RE.search(raw or ""):
        raise ConsentError("pan_not_allowed", "Card numbers are not allowed. Use the tokenized test field.", 400)


class ConsentService:
    def get_payload(self, token: str, header_member_id: str | None = None, cap_cents=None) -> dict:
        share = read_consent_token(token, header_member_id)["share"]
        if share.status == S.INVITED:
            transition_share(share.id, S.VIEWING, actor=f"member:{share.member_id}")
            share.refresh_from_db()
        return self._payload(share, cap_cents)

    @transaction.atomic
    def tokenize(self, token: str, transient_card_token: str, header_member_id: str | None = None) -> dict:
        share = read_consent_token(token, header_member_id)["share"]
        if not TRANSIENT_RE.match(transient_card_token or ""):
            raise ConsentError("invalid_transient_token", "Use the simulated secure card field.")
        share = Share.objects.select_for_update().get(pk=share.pk)
        purchase = share.purchase
        option = purchase.option
        cap = share.member.budget_cap_cents or share.amount_cents
        scope = {
            "purpose": "group_purchase",
            "purchase_id": str(purchase.id),
            "merchant_ids": [option.merchant_slug],
            "max_amount_cents": cap,
            "expires_at": _iso(purchase.deadline_at),
        }
        idem = _idem(share.id, "tokenize:" + _hash_text(transient_card_token), purchase.version)
        request = {"t": transient_card_token, "s": scope}
        request_hash = _hash_text(str(sorted(request.items())))
        existing = IdempotencyRecord.objects.filter(key=idem).first()
        if existing:
            if existing.request_hash != request_hash:
                raise ConsentError("idempotency_conflict", "This request does not match the original.", 409)
            return existing.response
        with event_context(purchase.group_id, purchase.id):
            resp = get_gateway().provision_agent_token(
                str(share.member_id), transient_card_token, scope, idem)
        last4 = resp["card"]["last4"]
        row = AgentToken.objects.create(
            member=share.member, gateway_token_id=resp["agent_token_id"], card_brand=resp["card"]["brand"],
            card_last4=last4, scope=scope, expires_at=purchase.deadline_at, status="active")
        out = {"agent_token_id": row.gateway_token_id, "card": {"brand": row.card_brand, "last4": row.card_last4},
               "simulated": True}
        IdempotencyRecord.objects.create(key=idem, request_hash=request_hash, response=out)
        return out

    def approve(self, token: str, agent_token_id: str, cap_cents: int, consent_text_sha256: str,
                assertion: dict, *, ip: str = "", user_agent: str = "",
                header_member_id: str | None = None) -> dict:
        share = read_consent_token(token, header_member_id)["share"]
        purchase = share.purchase
        default_cap = share.member.budget_cap_cents or share.amount_cents
        expected = self._payload(share, cap_cents)["consent_text_sha256"]
        if consent_text_sha256 != expected:
            raise ConsentError("consent_text_changed", "The consent text changed. Refresh and try again.", 409)
        if not (share.amount_cents <= int(cap_cents) <= default_cap):
            raise ConsentError("cap_out_of_range",
                               f"Cap must be between {format_usd(share.amount_cents)} and {format_usd(default_cap)}.")
        hour_ago = timezone.now() - timedelta(hours=1)
        recent = Consent.objects.filter(member=share.member, accepted_at__gte=hour_ago).count()
        if recent >= APPROVE_LIMIT_PER_HOUR:
            raise ConsentError("rate_limited", "Try again later or leave.", 429)
        if share.card_attempts >= MAX_CARD_DECLINES:
            raise ConsentError("too_many_declines", "Try again later or leave.", 429)
        method = self._verify_assertion(share, assertion)
        consent = Consent.objects.create(
            member=share.member, share=share, consent_text_version=VERSION, consent_text_sha256=expected,
            amount_cents=share.amount_cents, cap_cents=int(cap_cents), method=method,
            ip_hash=_hash_text(ip) if ip else "", user_agent_hash=_hash_text(user_agent) if user_agent else "",
            accepted_at=timezone.now())
        log_event(EventType.CONSENT_ACCEPTED, actor=f"member:{share.member_id}",
                  group_id=purchase.group_id, purchase_id=purchase.id,
                  payload={"share_id": str(share.id), "method": method, "amount_cents": share.amount_cents})
        agent_token = AgentToken.objects.filter(
            gateway_token_id=agent_token_id, member=share.member, status="active").first()
        if agent_token is None:
            raise ConsentError("token_inactive", "The payment token is missing, revoked, or expired.")
        from purchases.rules import place_hold
        result = place_hold(share.id, consent.id, agent_token.id, int(cap_cents))
        if "error" in result:
            raise ConsentError(result["error"]["code"], result["error"]["message"], 400)
        hold = result.get("hold") or {}
        status = str(hold.get("status") or "").upper()
        if result.get("share_status") == "card_failed" or status == "DECLINED":
            raise ConsentError(
                "card_declined", "Your bank didn't approve this hold. Nothing was charged.", 402,
                extra={"decline_code": hold.get("decline_code") or "do_not_honor"})
        hold_out = {
            "hold_id": hold.get("hold_id"),
            "amount_cents": hold.get("amount_cents", share.amount_cents),
            "status": "AUTHORIZED",
        }
        return {"share_status": "held", "hold": hold_out, "mandate_id": result.get("mandate_id"),
                "group_counts": result.get("group_counts") or {}, "simulated": True}

    def decline(self, token: str, reason: str, header_member_id: str | None = None) -> dict:
        share = read_consent_token(token, header_member_id)["share"]
        if reason not in DECLINE_REASONS:
            raise ConsentError("invalid_request", "Pick a private reason category.")
        log_event(EventType.CONSENT_DECLINED, actor=f"member:{share.member_id}",
                  group_id=share.purchase.group_id, purchase_id=share.purchase_id,
                  payload={"share_id": str(share.id), "reason": reason})
        from purchases import rules
        result = rules.member_withdraw(share.id, "declined")
        if "error" in result:
            raise ConsentError(result["error"].get("code", "illegal_state"), result["error"]["message"], 409)
        return {"share_status": result.get("share_status", "declined")}

    def reconfirm(self, share_id, assertion: dict) -> dict:
        share = Share.objects.select_related("member", "purchase").get(pk=share_id)
        from purchases import rules
        if not hasattr(rules, "apply_share_change"):
            raise ConsentError("not_ready", "Share changes are not available yet.", 501)
        if share.superseded_by_id or share.purchase_version != share.purchase.version:
            raise ConsentError("link_superseded", "Open your newest private consent link.", 410)
        previous = Share.objects.filter(superseded_by=share, member=share.member).first()
        latest = (previous or share).consents.order_by("-accepted_at").first()
        if latest is None or share.amount_cents > latest.cap_cents:
            raise ConsentError("consent_required", "Approve a new limit through fresh consent first.", 409)
        method = self._verify_assertion(share, assertion)
        payload = self._payload(share, latest.cap_cents)
        consent = Consent.objects.create(member=share.member, share=share, consent_text_version=VERSION,
            consent_text_sha256=payload["consent_text_sha256"], amount_cents=share.amount_cents,
            cap_cents=latest.cap_cents, method=method, accepted_at=timezone.now())
        log_event(EventType.CONSENT_ACCEPTED, actor=f"member:{share.member_id}",
            group_id=share.purchase.group_id, purchase_id=share.purchase_id,
            payload={"share_id": str(share.id), "method": method, "amount_cents": share.amount_cents})
        return rules.apply_share_change(share.id, consent.id)

    def _verify_assertion(self, share, assertion: dict) -> str:
        assertion = assertion or {}
        method = assertion.get("method")
        if method == "otp":
            code = str(assertion.get("code") or "")
            if not settings.DEMO_MODE:
                raise ConsentError("auth_not_configured", "Passkey confirmation is required.", 401)
            log.info("demo OTP issued for member %s", share.member_id)
            if code != DEMO_OTP:
                raise ConsentError("otp_invalid", "That code didn't match. Try again.")
            log_event(EventType.AUTH_OTP_VERIFIED, actor=f"member:{share.member_id}",
                      group_id=share.purchase.group_id, purchase_id=share.purchase_id, payload={"method": "otp"})
            return Consent.Method.OTP
        if method == "passkey":
            if not settings.DEMO_MODE:
                raise ConsentError("auth_not_configured", "Verified passkey authentication is not configured.", 401)
            if not (assertion.get("credential_id") and assertion.get("signature")):
                raise ConsentError("assertion_required", "Confirm with Face ID / passkey, or the demo OTP.")
            return Consent.Method.PASSKEY
        raise ConsentError("assertion_required", "Confirm with Face ID / passkey, or the demo OTP.")

    def _payload(self, share: Share, cap_cents=None) -> dict:
        purchase = share.purchase
        option = purchase.option
        group = purchase.group
        merchant = Merchant.objects.filter(slug=option.merchant_slug).first()
        merchant_name = merchant.name if merchant else option.merchant_slug
        dates = option.meta.get("dates")
        if isinstance(dates, dict):
            dates = f"{dates.get('start', '')} to {dates.get('end', '')}"
        dates = dates or " to ".join(filter(None, [option.meta.get("check_in"), option.meta.get("check_out")]))
        deadline_label = format_deadline(purchase.deadline_at, group.timezone)
        default_cap = share.member.budget_cap_cents or share.amount_cents
        selected_cap = default_cap
        if cap_cents is not None:
            try:
                selected_cap = int(cap_cents)
            except (ValueError, TypeError):
                raise ConsentError("cap_out_of_range", "Choose a valid limit.")
            if isinstance(cap_cents, (bool, float)) or not share.amount_cents <= selected_cap <= default_cap:
                raise ConsentError("cap_out_of_range", "Limit must cover your share and cannot exceed your current cap.")
        organizer = group.organizer.display_name if group.organizer_id else ""
        text = render(
            member_name=share.member.display_name, share_cents=share.amount_cents, cap_cents=selected_cap,
            split_count=purchase.shares.filter(superseded_by__isnull=True).count(),
            title=option.title, merchant_name=merchant_name, deadline_label=deadline_label)
        split_count = purchase.shares.filter(superseded_by__isnull=True).count()
        offers = get_gateway().list_offers("", option.merchant_slug).get("offers") or []
        previous = Share.objects.filter(superseded_by=share, member=share.member).first()
        from purchases import rules
        reconfirmation = None
        if previous:
            reconfirmation = {"from_cents": previous.amount_cents, "to_cents": share.amount_cents,
                              "available": hasattr(rules, "apply_share_change")}
        return {
            "reconfirmation": reconfirmation,
            "member": {"id": str(share.member_id), "display_name": share.member.display_name},
            "invited_by": organizer,
            "group_name": group.name,
            "purchase": {
                "id": str(purchase.id), "version": purchase.version, "title": option.title,
                "merchant_name": merchant_name, "merchant_slug": option.merchant_slug, "dates": dates,
                "total_cents": purchase.total_cents, "split_count": split_count,
                "photo_url": option.meta.get("photo_url") or "/static/demo/pinecrest.jpg",
            },
            "share_cents": share.amount_cents,
            "default_cap_cents": default_cap,
            "deadline_at": _iso(purchase.deadline_at),
            "can": can_lines(share_cents=share.amount_cents, split_count=split_count,
                             title=option.title, merchant_name=merchant_name),
            "can_never": can_never_lines(cap_cents=selected_cap, deadline_label=deadline_label),
            "consent_text_version": VERSION,
            "consent_text_sha256": _hash_text(text),
            "simulated": True,
            "offers": offers,
            "share_id": str(share.id),
            "group_id": str(group.id),
            "share_status": share.status,
        }
