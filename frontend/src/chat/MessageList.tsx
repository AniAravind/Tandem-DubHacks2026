import { Fragment, type ReactNode } from "react";
import { CardView } from "../components/cards";
import type { ChatMessage } from "./types";

// Turn "/c/abc" and "https://..." into links; everything else stays plain text.
function linkify(text: string): ReactNode[] {
  return text.split(/(\s+)/).map((part, i) =>
    /^(https?:\/\/\S+|\/(c|g|p|chat)\/\S+)$/.test(part)
      ? <a key={i} href={part}>{part.startsWith("/c/") ? "Open my private link →" : part}</a>
      : <Fragment key={i}>{part}</Fragment>);
}

// Vote and funding cards are re-posted as they change; only the newest copy of each is shown.
function cardKey(m: ChatMessage): string | null {
  const d = m.card?.data as Record<string, string> | undefined;
  if (!m.card || !d) return null;
  if (m.card.type === "VoteCard") return `vote:${d.vote_id}`;
  if (m.card.type === "FundingCard") return `funding:${d.purchase_id}`;
  return null;
}

export function MessageList({ messages, meId, onReply, consentUrl, onDemoShare }: {
  messages: ChatMessage[]; meId: string; onReply: (t: string) => void; consentUrl?: string;
  onDemoShare?: (action: "hold" | "decline") => void;
}) {
  const latest = new Map<string, number>();
  for (const m of messages) {
    const k = cardKey(m);
    if (k) latest.set(k, m.seq);
  }
  return (
    <ol className="messages" role="log" aria-live="polite" aria-label="Group chat">
      {messages.map((m) => {
        const k = cardKey(m);
        const showCard = m.card && (!k || latest.get(k) === m.seq);
        if (!m.text && !showCard) return null;
        const mine = m.direction === "in" && m.member?.id === meId;
        const who = m.direction === "out" ? "Tandem" : m.member?.display_name ?? "Someone";
        return (
          <li key={m.id} className={`msg ${m.direction === "out" ? "agent" : mine ? "mine" : "member"}${m.private ? " private" : ""}`}>
            {m.text ? (
              <div className="bubble">
                <span className="who">{who}{m.private ? " · only you see this" : ""}</span>
                <p>{linkify(m.text)}</p>
              </div>
            ) : null}
            {showCard && m.card ? <CardView card={m.card} onReply={onReply} consentUrl={consentUrl} meId={meId} onDemoShare={onDemoShare} /> : null}
          </li>
        );
      })}
    </ol>
  );
}
