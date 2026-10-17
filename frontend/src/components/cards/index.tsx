import type { ComponentType } from "react";
import type { CardEnvelope } from "../../chat/types";
import type { CardProps } from "./common";
import { FallbackCard } from "./FallbackCard";
import { FundingCard } from "./FundingCard";
import { OptionCard } from "./OptionCard";
import { BookingConfirmed, ErrorCard, ExpenseCard, LedgerCard, PlanSummary, PreferenceRequest } from "./OtherCards";
import { VoteCard } from "./VoteCard";

const CARDS: Record<string, ComponentType<CardProps<any>>> = {
  OptionCard, VoteCard, FundingCard, FallbackCard, BookingConfirmed, ExpenseCard, LedgerCard, ErrorCard,
  PreferenceRequest, PlanSummary,
};

export function CardView({ card, ...rest }: { card: CardEnvelope } & Omit<CardProps<unknown>, "data">) {
  const Component = CARDS[card.type];
  if (!Component) return null;
  return <Component data={card.data} {...rest} />;
}
