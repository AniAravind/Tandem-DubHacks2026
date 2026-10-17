from chat.adapters.base import AgentMessage, InboundMessage


class PhotonAdapter:
    """iMessage via Photon. Not built yet: Aryan fills this in after the Photon spike."""
    channel = "imessage"

    def parse_inbound(self, request) -> InboundMessage:
        raise NotImplementedError("Photon adapter is not built yet")

    def send(self, group_id: str, message: AgentMessage) -> str:
        raise NotImplementedError("Photon adapter is not built yet")
