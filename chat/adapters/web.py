from django.db import transaction
from django.db.models import Max
from chat.models import ChatMessage
from chat.adapters.base import AgentMessage, InboundMessage


class WebChatAdapter:
    channel = "web"

    def parse_inbound(self, request) -> InboundMessage:
        return InboundMessage(group_id=str(request.data["group_id"]),
                              member_id=request.headers["X-Tandem-Member"],   # Phase 0 demo auth (6.3)
                              text=str(request.data["text"])[:2000])

    @transaction.atomic
    def send(self, group_id: str, message: AgentMessage) -> str:
        seq = (ChatMessage.objects.select_for_update().filter(group_id=group_id).aggregate(m=Max("seq"))["m"] or 0) + 1
        m = ChatMessage.objects.create(group_id=group_id, member=None, direction="out", text_redacted=message.text,
                                       card={"type": message.card.type, "data": message.card.data} if message.card else None,
                                       quick_replies=message.quick_replies, channel="web", seq=seq)
        return str(m.id)
