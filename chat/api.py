"""Phase 0 stub chat endpoints (Aryan replaces these with the real orchestrator-backed views).

Deliberately does not import anything from payments/ (the agent and chat layers never touch money).
"""
from rest_framework.decorators import api_view
from rest_framework.response import Response


@api_view(["POST"])
def chat_web(request):
    return Response({"message_id": "0f3e...", "seq": 41, "status": "queued"}, status=202)


@api_view(["GET"])
def group_messages(request, gid):
    return Response({"messages": []})
