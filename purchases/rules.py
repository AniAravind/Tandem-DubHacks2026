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
from datetime import timedelta, timezone as dt_timezone

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from chat.adapters.base import AgentMessage, Card
from core.events import EventType, event_context, log_event
from core.models import EventLog, Member
from payments.gateway import GatewayError, Money, get_gateway
from payments.models import AgentToken, Consent, Hold, Mandate
from purchases import consensus, serializers as ser, toolschema
from purchases.models import Option, Purchase, Share, Vote
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


def _send_consent_links(shares: list) -> int:
    """
    Hand each member their private link. Consent links are secrets (whoever holds one can act as that member),
    so they are NEVER posted in the group chat or returned to the AI. Delivery needs a private-message sender
    from the chat side (chat.services.send_private, Aryan). Until that exists, nothing is sent and we return 0.
    """
    try:
        from chat.services import send_private
    except ImportError:
        log.warning("chat.services.send_private is missing; %d consent links were created but not delivered", len(shares))
        return 0
    for share in shares:
        send_private(str(share.purchase.group_id), str(share.member_id),
                     AgentMessage(text="Your private link to approve your share (simulated payments).",
                                  link=consent_link(share), private_to_member_id=str(share.member_id)))
    return len(shares)


class RulesEngine:
    """
    execute(tool, args, actor) -> {"ok": True, "data": {...}} or {"ok": False, "error": {"code", "message"}}

    `actor` is "member:<uuid>", the person whose chat message led to this call. Every call is checked against
    the argument schema in agent/tools.json, rate limited, and logged (tool name and argument NAMES only).
    """

    def __init__(self):
        self.schemas = toolschema.load_tool_schemas()
        self.tools = {
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
        if args["group_size"] != len(members):
            raise RuleError("group_size_mismatch", f"The group has {len(members)} active members, not {args['group_size']}.")
        budget_max = args.get("budget_per_person_max")
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
                      "group_size": args["group_size"]}))
        log_event(EventType.OPTIONS_RANKED, actor="rules", group_id=member.group_id,
                  payload={"count": len(options), "top": options[0].title if options else None})
        return {"options": [ser.option_json(o) for o in options]}

    def _start_vote(self, member: Member, args: dict) -> dict:
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
        return {"vote_id": str(vote.id), "closes_at": ser.iso(vote.closes_at), "quorum": vote.quorum,
                "choices": [{"key": c["key"], "label": c["label"]} for c in vote.choices]}

    def _create_purchase(self, member: Member, args: dict) -> dict:
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
        if len(buyers) < 2 or any(b.status != Member.Status.ACTIVE for b in buyers):
            raise RuleError("invalid_arguments", "Choose at least two active members.")
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
        return {"purchase_id": str(purchase.id), "deadline_at": ser.iso(purchase.deadline_at),
                "shares_created": len(shares), "consent_links_sent": sent}

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
        raise RuleError("not_implemented", "Fallbacks are not built yet.")           # milestone M6

    def _add_expense(self, member: Member, args: dict) -> dict:
        raise RuleError("not_implemented", "Expenses are not built yet.")            # milestone M5


def ceil_split(total: int, people: int) -> int:
    return consensus.ceil_div(total, people)
