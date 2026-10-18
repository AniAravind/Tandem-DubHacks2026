import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { usd } from "../../api/client";
import type { FundingCardData } from "../../types/contracts";
import { countdown, when } from "../../chat/format";
import { type CardProps } from "./common";
import { SHARE_STATUS } from "./status";

export function FundingCard({ data, consentUrl, meId, onDemoShare }: CardProps<FundingCardData>) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const active = data.members.filter((m) => !["declined", "released"].includes(m.status));
  const held = active.filter((m) => m.status === "held" || m.status === "captured").length;
  const tally = (s: string[]) => active.filter((m) => s.includes(m.status)).length;
  const mine = data.members.find((m) => m.id === meId);
  const myTurn = mine && ["invited", "viewing", "card_failed"].includes(mine.status);
  const lowTime = new Date(data.deadline_at).getTime() - now < 2 * 3600 * 1000;
  return (
    <article className="card funding-card" aria-label={`Funding for ${data.title}`}>
      <div className="card-row">
        <h3 className="card-title">{data.title} · <span className="money">{usd(data.total_cents)}</span></h3>

      </div>
      <div className="progress" role="progressbar" aria-valuenow={held} aria-valuemin={0} aria-valuemax={active.length}
           aria-label={`${held} of ${active.length} shares held`}>
        <span style={{ width: `${active.length ? (held / active.length) * 100 : 0}%` }} />
      </div>
      <p><strong>{held} of {active.length} held</strong> <span className="money muted">({usd(data.held_cents)} of {usd(data.total_cents)})</span></p>
      <ul className="avatars">
        {data.members.map((m) => {
          const s = SHARE_STATUS[m.status];
          return (
            <li key={m.id} className={`avatar tone-${s.tone}`} title={`${m.display_name}: ${s.label}`}>
              <span className="avatar-initial" aria-hidden="true">{m.initials}</span>
              <span className="avatar-glyph" aria-hidden="true">{s.glyph}</span>
              <span className="sr-only">{m.display_name}: {s.label}</span>
            </li>
          );
        })}
      </ul>
      <p className="muted">
        <span aria-hidden="true">🔒</span> Held {tally(["held", "captured"])} · <span aria-hidden="true">⏱</span> Deciding{" "}
        {tally(["viewing", "card_failed"])} · <span aria-hidden="true">◌</span> Invited {tally(["invited"])}
      </p>
      <p className={lowTime ? "deadline low" : "deadline"} aria-live="off">
        Deadline {when(data.deadline_at)} · <span className="money">{countdown(data.deadline_at, now)}</span>
      </p>
      <p>Nobody is charged unless all {active.length} are in.</p>
      {myTurn && consentUrl?.startsWith("/c/demo-") && onDemoShare ? (
        <div className="card-actions">
          <button className="btn primary" onClick={() => onDemoShare("hold")}>Hold my share</button>
          <button className="btn" onClick={() => onDemoShare("decline")}>Not for me</button>
        </div>
      ) : myTurn && consentUrl ? (
        <Link className="btn primary" to={consentUrl}>{mine?.status === "card_failed" ? "Try another card →" : "Review & hold my share →"}</Link>
      ) : null}
    </article>
  );
}
