from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Protocol

CardType = Literal["PlanSummary", "PreferenceRequest", "OptionCard", "CompareCard", "VoteCard", "FundingCard",
                   "FallbackCard", "BookingConfirmed", "ExpenseCard", "LedgerCard", "ErrorCard", "LiveOffersCard"]


@dataclass
class Card:
    type: CardType
    data: dict                                   # shapes in frontend/src/types/contracts.ts


@dataclass
class InboundMessage:
    group_id: str
    member_id: str
    text: str
    channel: Literal["web", "imessage"] = "web"
    external_id: str = ""                        # provider message id (dedupe)


@dataclass
class AgentMessage:
    text: str
    card: Card | None = None
    quick_replies: list[str] = field(default_factory=list)
    link: str | None = None
    private_to_member_id: str | None = None      # DM-style (consent links, reminders); None = group


class ChatAdapter(Protocol):
    channel: str
    def parse_inbound(self, request) -> InboundMessage: ...     # verify + normalize provider payload
    def send(self, group_id: str, message: AgentMessage) -> str: ...   # returns stored message id
