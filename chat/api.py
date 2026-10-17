"""Web chat endpoints (PHASE_0.md 6.3). Deliberately does not import anything from payments/."""
import uuid

from rest_framework.decorators import api_view
from rest_framework.response import Response

from chat import services
from chat.models import ChatMessage
from purchases.api import ApiError, api_errors, current_member

MAX_PAGE = 200


def message_json(m: ChatMessage) -> dict:
    inbound = m.direction == "in"
    return {"id": str(m.id), "seq": m.seq, "direction": m.direction,
            "member": {"id": str(m.member_id), "display_name": m.member.display_name} if inbound and m.member else None,
            "text": m.text_redacted, "card": m.card, "quick_replies": m.quick_replies or [],
            "private": not inbound and m.member_id is not None,
            "created_at": m.created_at.isoformat().replace("+00:00", "Z")}


@api_view(["POST"])
@api_errors
def chat_web(request):
    data = request.data if isinstance(request.data, dict) else {}
    try:
        group_id = uuid.UUID(str(data.get("group_id")))
    except ValueError:
        raise ApiError("invalid_request", "group_id must be a group id.")
    if not isinstance(data.get("text"), str) or not data["text"].strip():
        raise ApiError("invalid_request", "text is required.")
    member = current_member(request, group_id)
    inbound = services.WebChatAdapter().parse_inbound(request)
    inbound.member_id = str(member.id)
    try:
        stored = services.handle_inbound(inbound)
    except services.RateLimited:
        raise ApiError("rate_limited", "This chat is sending a lot of messages. Wait a minute and try again.", 429)
    return Response({"message_id": str(stored.id), "seq": stored.seq, "status": "queued"}, status=202)


@api_view(["GET"])
@api_errors
def group_messages(request, gid):
    member = current_member(request, gid)
    try:
        after_seq = max(int(request.query_params.get("after_seq", 0)), 0)
    except ValueError:
        raise ApiError("invalid_request", "after_seq must be a whole number.")
    rows = (ChatMessage.objects.filter(group_id=gid, seq__gt=after_seq).select_related("member").order_by("seq"))
    visible = [m for m in rows[:MAX_PAGE] if m.direction == "in" or m.member_id in (None, member.id)]   # DMs only to their owner
    return Response({"messages": [message_json(m) for m in visible]})


@api_view(["POST"])
@api_errors
def demo_share(request, gid):
    """DEMO_MODE + fake engine only: hold or decline your own share without the consent page (all simulated)."""
    member = current_member(request, gid)
    action = request.data.get("action") if isinstance(request.data, dict) else None
    if action not in ("hold", "decline"):
        raise ApiError("invalid_request", "action must be hold or decline.")
    try:
        return Response(services.demo_share_action(member, action))
    except services.DemoUnavailable as e:
        raise ApiError("demo_unavailable", str(e), 409)
