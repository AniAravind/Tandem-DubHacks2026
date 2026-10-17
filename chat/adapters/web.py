from django.db import transaction
from django.db.models import Max
from chat.models import ChatMessage
from chat.adapters.base import AgentMessage, InboundMessage


def next_seq(group_id) -> int:
    """Call inside a transaction: locks the group's messages so two writers never get the same seq."""
    return (ChatMessage.objects.select_for_update().filter(group_id=group_id).aggregate(m=Max("seq"))["m"] or 0) + 1


class WebChatAdapter:
    channel = "web"

    def parse_inbound(self, request) -> InboundMessage:
        return InboundMessage(group_id=str(request.data["group_id"]),
                              member_id=request.headers["X-Tandem-Member"],   # Phase 0 demo auth (6.3)
                              text=str(request.data["text"])[:2000])

    @transaction.atomic
    def send(self, group_id: str, message: AgentMessage) -> str:
        # ChatMessage has no recipient or link column yet: an outbound row WITH a member is a private message to
        # that member, and the link is appended to the text (the web UI and iMessage both show it as a link).
        text = f"{message.text}\n{message.link}" if message.link else message.text
        m = ChatMessage.objects.create(group_id=group_id, member_id=message.private_to_member_id, direction="out",
                                       text_redacted=text,
                                       card={"type": message.card.type, "data": message.card.data} if message.card else None,
                                       quick_replies=message.quick_replies, channel="web", seq=next_seq(group_id))
        return str(m.id)
