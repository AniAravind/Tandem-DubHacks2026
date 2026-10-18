"""
Purchases API.

M2 (real, reads the database): group, preferences, ballots, purchase, leave, ledger, events.
Still a Phase 0 stub that returns the example JSON from purchases/fixtures.py: demo (M8).

Who is calling? Two ways, checked in this order:
  1. A signed session cookie, handed out when someone opens their personal consent link (core/session.py).
  2. The header  X-Tandem-Member: <member uuid>, trusted ONLY when DEMO_MODE=1.
A member who is not an active member of the group gets a 403.
"""
import functools
import hmac
import uuid

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.events import EventType, log_event
from core.models import Group, Member
from core.session import member_id_from_request
from purchases import consensus, demo, ledger as ledger_lib, rules, serializers as ser
from purchases.models import Ballot, Purchase, Share, Vote

MAX_BUDGET_CENTS = 1_000_000            # $10,000 sanity limit for a private budget
FLEXIBILITY = ("firm", "flexible")


class ApiError(Exception):
    """Raise this anywhere in a view to send the standard error envelope  {"error": {code, message, request_id}}."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def api_errors(view):
    """Wrap a view so an ApiError (or a missing object) becomes the standard JSON error instead of a crash."""
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except ApiError as e:
            code, message, status = e.code, e.message, e.status
        except Http404:
            code, message, status = "not_found", "That item does not exist.", 404
        return Response({"error": {"code": code, "message": message, "request_id": "req_" + uuid.uuid4().hex[:12]}},
                        status=status)
    return wrapper


def current_member(request, group_id) -> Member:
    """Who is calling, and are they allowed to touch this group? 401 = we do not know you, 403 = not your group."""
    member_id = member_id_from_request(request)
    if member_id is None:
        if not settings.DEMO_MODE:
            raise ApiError("auth_not_configured", "Sign in with your personal link first.", 401)
        try:
            member_id = uuid.UUID(request.headers.get("X-Tandem-Member", ""))
        except ValueError:
            raise ApiError("unauthenticated", "Send a valid X-Tandem-Member header.", 401)
    try:
        member = Member.objects.select_related("group").get(pk=member_id)
    except Member.DoesNotExist:
        raise ApiError("unauthenticated", "We do not know who you are. Open your personal link again.", 401)
    if member.group_id != group_id or member.status != Member.Status.ACTIVE:
        log_event(EventType.AUTH_DENIED, actor=f"member:{member.id}", group_id=member.group_id,
                  payload={"reason": "not_a_member", "wanted_group": str(group_id)})
        raise ApiError("not_a_member", "You are not an active member of this group.", 403)
    return member


def _as_list_of_text(value, field: str) -> list:
    if not isinstance(value, list) or len(value) > 20 or not all(isinstance(v, str) and 0 < len(v) <= 40 for v in value):
        raise ApiError("invalid_request", f"{field} must be a list of up to 20 short words.")
    return value


# ------------------------------------------------------------------ groups and preferences
@api_view(["GET"])
@api_errors
def group_detail(request, gid):
    current_member(request, gid)
    return Response(ser.group_json(get_object_or_404(Group, pk=gid)))


@api_view(["GET", "PUT"])
@api_errors
def my_preferences(request, gid):
    """Private: you only ever see and change YOUR OWN budget and preferences."""
    member = current_member(request, gid)
    if request.method == "PUT":
        data = request.data if isinstance(request.data, dict) else {}
        prefs = dict(member.prefs or {})
        changed = []
        if "budget_cap_cents" in data:
            cents = data["budget_cap_cents"]
            if isinstance(cents, bool) or not isinstance(cents, int) or not 0 < cents <= MAX_BUDGET_CENTS:
                raise ApiError("invalid_request", "budget_cap_cents must be a whole number of cents between 1 and 1,000,000.")
            member.budget_cap_cents = cents
            changed.append("budget_cap_cents")
        for field in ("must_haves", "dealbreakers"):
            if field in data:
                prefs[field] = _as_list_of_text(data[field], field)
                changed.append(field)
        if "flexibility" in data:
            if data["flexibility"] not in FLEXIBILITY:
                raise ApiError("invalid_request", f"flexibility must be one of {', '.join(FLEXIBILITY)}.")
            prefs["flexibility"] = data["flexibility"]
            changed.append("flexibility")
        member.prefs = prefs
        member.save()
        if changed:
            log_event(EventType.PREFS_UPDATED, actor=f"member:{member.id}", group_id=gid,
                      payload={"member_id": str(member.id), "fields": changed})      # field NAMES only, never values
    return Response(ser.preferences_json(member))


@api_view(["GET"])
@api_errors
def options(request, gid):
    """Options from the group's latest search (ranking happens in the search_options tool)."""
    current_member(request, gid)
    return Response(ser.latest_options_json(get_object_or_404(Group, pk=gid)))


# ------------------------------------------------------------------ voting
@api_view(["POST"])
@api_errors
def cast_ballot(request, vid):
    vote = get_object_or_404(Vote, pk=vid)
    member = current_member(request, vote.group_id)
    choice = request.data.get("choice_key") if isinstance(request.data, dict) else None
    if vote.status != Vote.Status.OPEN or vote.closes_at <= timezone.now():
        raise ApiError("vote_closed", "This vote is closed.", 409)
    if not isinstance(choice, str) or choice not in {c["key"] for c in vote.choices}:
        raise ApiError("invalid_choice", "That is not one of the choices.")
    if member.id not in {m.id for m in consensus.eligible_voters(vote)}:
        raise ApiError("not_a_voter", "Only people still in this purchase can vote on this.", 403)
    with transaction.atomic():
        Ballot.objects.update_or_create(vote=vote, member=member, defaults={"choice_key": choice})   # change allowed
        log_event(EventType.BALLOT_CAST, actor=f"member:{member.id}", group_id=vote.group_id,
                  payload={"vote_id": str(vote.id), "member_id": str(member.id)})
        vote = consensus.close_if_decided(vote)               # closes right away once everybody has voted
    if vote.purpose == Vote.Purpose.FALLBACK and vote.status == Vote.Status.CLOSED:
        rules.apply_fallback(vote.id)                          # does nothing unless there is a winner; safe to repeat
    return Response(ser.vote_json(vote, member))


# ------------------------------------------------------------------ purchases and shares
@api_view(["GET"])
@api_errors
def purchase_detail(request, pid):
    purchase = get_object_or_404(Purchase.objects.select_related("option"), pk=pid)
    current_member(request, purchase.group_id)
    return Response(ser.purchase_json(purchase))


@api_view(["POST"])
@api_errors
def leave_share(request, sid):
    share = get_object_or_404(Share.objects.select_related("purchase"), pk=sid)
    member = current_member(request, share.purchase.group_id)
    if share.member_id != member.id:
        raise ApiError("not_your_share", "You can only leave your own share.", 403)
    result = rules.member_withdraw(share.id, reason="member_left")
    if "error" in result:
        raise ApiError(result["error"]["code"], result["error"]["message"], 409)
    return Response({"share_status": result["share_status"]})


# ------------------------------------------------------------------ ledger and events
@api_view(["POST"])
@api_errors
def add_expense(request, gid):
    """Any active member can record an expense. Amounts here are whole CENTS (the agent tool uses dollars)."""
    member = current_member(request, gid)
    data = request.data if isinstance(request.data, dict) else {}
    split = data.get("split") if isinstance(data.get("split"), dict) else {}
    member_ids, custom = split.get("member_ids"), split.get("custom_amounts")
    if member_ids is not None and not (isinstance(member_ids, list) and all(isinstance(m, str) for m in member_ids)):
        raise ApiError("invalid_request", "split.member_ids must be a list of member ids.")
    if custom is not None and not isinstance(custom, dict):
        raise ApiError("invalid_request", "split.custom_amounts must map member ids to cents.")
    if not isinstance(data.get("payer_id"), str) or not isinstance(data.get("description", ""), str):
        raise ApiError("invalid_request", "payer_id and description must be text.")
    try:
        result = ledger_lib.add_expense(get_object_or_404(Group, pk=gid), data["payer_id"], data.get("amount_cents"),
                                    data.get("description", ""), split.get("mode", "equal"), member_ids, custom,
                                    actor=f"member:{member.id}")
    except ledger_lib.LedgerError as e:
        raise ApiError(e.code, e.message, 403 if e.code == "not_a_member" else 400)
    return Response({"expense_id": result["expense_id"], "splits": result["splits"],
                     "balances_net_cents": result["balances_net_cents"]}, status=201)


@api_view(["GET"])
@api_errors
def ledger(request, gid):
    current_member(request, gid)
    return Response(ser.ledger_json(get_object_or_404(Group, pk=gid)))


@api_view(["GET"])
@api_errors
def events(request, gid):
    current_member(request, gid)
    try:
        after_seq = max(int(request.query_params.get("after_seq", 0)), 0)
    except ValueError:
        raise ApiError("invalid_request", "after_seq must be a whole number.")
    return Response(ser.events_json(get_object_or_404(Group, pk=gid), after_seq))


@api_view(["POST"])
@api_errors
def demo_action(request, action):
    """Backstage buttons for the live demo. Locked unless DEMO_MODE=1 AND the X-Demo-Key header matches DEMO_ADMIN_KEY."""
    key = request.headers.get("X-Demo-Key", "")
    allowed = settings.DEMO_MODE and bool(settings.DEMO_ADMIN_KEY) and hmac.compare_digest(
        key.encode(), settings.DEMO_ADMIN_KEY.encode())
    if not allowed:
        raise ApiError("forbidden", "Demo controls are locked.", 403)
    try:
        return Response({"ok": True, "action": action, "simulated": True, **demo.run(action, request.data or {})})
    except demo.DemoError as e:
        raise ApiError(e.code, e.message, e.status)
