"""
Purchases API.

M2 (real, reads the database): group, preferences, ballots, purchase, leave, ledger, events.
Still Phase 0 stubs that return the example JSON from purchases/fixtures.py: options (M3), expenses (M5), demo (M8).

Who is calling? During the hackathon demo the caller sends the header  X-Tandem-Member: <member uuid>.
That header is only trusted when DEMO_MODE=1. A member who is not an active member of the group gets a 403.
"""
import functools
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
from purchases import consensus, rules, serializers as ser
from purchases.fixtures import FIXTURES
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
    if not settings.DEMO_MODE:
        raise ApiError("auth_not_configured", "Sign-in is not available yet.", 401)
    raw = request.headers.get("X-Tandem-Member", "")
    try:
        member = Member.objects.select_related("group").get(pk=uuid.UUID(raw))
    except (ValueError, Member.DoesNotExist):
        raise ApiError("unauthenticated", "Send a valid X-Tandem-Member header.", 401)
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
    with transaction.atomic():
        Ballot.objects.update_or_create(vote=vote, member=member, defaults={"choice_key": choice})   # change allowed
        log_event(EventType.BALLOT_CAST, actor=f"member:{member.id}", group_id=vote.group_id,
                  payload={"vote_id": str(vote.id), "member_id": str(member.id)})
        vote = consensus.close_if_decided(vote)               # closes right away once everybody has voted
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
def add_expense(request, gid):
    return Response(FIXTURES["expense_created"], status=201)      # real logic arrives in M5


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
def demo_action(request, action):
    return Response({"ok": True, "action": action, "stub": True, "simulated": True})
