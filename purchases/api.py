"""Phase 0 stub views: each returns the example JSON from purchases/fixtures.py.

Eshaan replaces these with real logic (reads from the database, calls the rules engine) in the next milestones.
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response

from purchases.fixtures import FIXTURES


@api_view(["GET"])
def group_detail(request, gid):
    return Response(FIXTURES["group_detail"])


@api_view(["GET", "PUT"])
def my_preferences(request, gid):
    return Response(FIXTURES["my_preferences"])


@api_view(["GET"])
def options(request, gid):
    return Response(FIXTURES["options"])


@api_view(["POST"])
def cast_ballot(request, vid):
    return Response(FIXTURES["ballot"])


@api_view(["GET"])
def purchase_detail(request, pid):
    return Response(FIXTURES["purchase_detail"])


@api_view(["POST"])
def leave_share(request, sid):
    return Response(FIXTURES["leave"])


@api_view(["POST"])
def add_expense(request, gid):
    return Response(FIXTURES["expense_created"], status=201)


@api_view(["GET"])
def ledger(request, gid):
    return Response(FIXTURES["ledger"])


@api_view(["GET"])
def events(request, gid):
    return Response(FIXTURES["events"])


@api_view(["POST"])
def demo_action(request, action):
    return Response({"ok": True, "action": action, "stub": True, "simulated": True})
