import { Fragment, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { plainText } from "../../../shared/message-text.mjs";
import tandemMark from "../assets/tandem-mark-blue.png";
import { CardView } from "../components/cards";
import type { ChatMessage } from "./types";

const IN_APP = /^(?:https?:\/\/[^/\s]+)?(\/(c|g|p|chat)\/[^\s]+?)([.,!?)]*)$/;
const LINK_LABELS: Record<string, string> = {
  c: "Review & approve my share", p: "Open the shared plan", g: "Open group overview", chat: "Open group chat",
};

// In-app links ("/c/abc", "https://host/p/123") become action links; other URLs stay plain links.
function linkify(text: string): ReactNode[] {
  return text.split(/(\s+)/).map((part, i) => {
    const app = part.match(IN_APP);
    if (app && (part.startsWith("/") || part.includes(window.location.host))) {
      return <Fragment key={i}><Link className={`link-chip kind-${app[2]}`} to={app[1]} title={app[1]}>{LINK_LABELS[app[2]]} <span aria-hidden="true">→</span></Link>{app[3]}</Fragment>;
    }
    return /^https?:\/\/\S+$/.test(part)
      ? <a key={i} href={part} title={part} rel="noreferrer">Open link ↗</a>
      : <Fragment key={i}>{part}</Fragment>;
  });
}

function MessageBody({text}: {text:string}) {
  const blocks = plainText(text).split(/\n\n+/);
  return <>{blocks.map((block,index) => {
    const lines=block.split('\n');
    const title=index===0 && lines[0].length<80 && blocks.length>1 && !/^https?:/.test(lines[0]);
    return <p key={index}>{lines.map((line,i)=><Fragment key={i}>{i>0?'\n':null}{title&&i===0?<strong className="message-title">{linkify(line)}</strong>:linkify(line)}</Fragment>)}</p>;
  })}</>;
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
            <span className="message-initial" aria-hidden="true">{m.direction === "out" ? <img src={tandemMark} alt=""/> : who.slice(0,1)}</span>
            {m.text ? (
              <div className="bubble">
                <span className="who">{who}{m.private ? " · only you see this" : ""}</span>
                {m.direction === "out" ? <MessageBody text={m.text}/> : <p>{linkify(m.text)}</p>}
              </div>
            ) : null}
            {showCard && m.card ? <CardView card={m.card} onReply={onReply} consentUrl={consentUrl} meId={meId} onDemoShare={onDemoShare} /> : null}
          </li>
        );
      })}
    </ol>
  );
}
