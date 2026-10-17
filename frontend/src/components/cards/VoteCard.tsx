import { usd } from "../../api/client";
import type { VoteCardData } from "../../types/contracts";
import { clock } from "../../chat/format";
import type { CardProps } from "./common";

export function VoteCard({ data, onReply }: CardProps<VoteCardData>) {
  const max = Math.max(1, ...data.choices.map((c) => c.count));
  const closed = Boolean(data.winner_key);
  return (
    <article className="card vote-card" aria-label="Group vote">
      <div className="card-row">
        <h3 className="card-title"><span aria-hidden="true">🗳️ </span>{closed ? "Vote closed" : "Pick one"}</h3>
        {!closed ? <span className="muted">closes {clock(data.closes_at)} <span aria-hidden="true">⏱</span></span> : null}
      </div>
      <ul className="vote-list">
        {data.choices.map((c) => (
          <li key={c.key}>
            <button className={`vote-choice${data.your_choice === c.key ? " mine" : ""}`} disabled={closed}
                    onClick={() => onReply(c.key)} aria-label={`Vote ${c.key}: ${c.label}, ${usd(c.per_person_cents)} each, ${c.count} votes`}>
              <span className="vote-key">{c.key}</span>
              <span className="vote-label">{c.label}</span>
              <span className="money">{usd(c.per_person_cents)}</span>
              <span className="bar" aria-hidden="true"><span style={{ width: `${(c.count / max) * 100}%` }} /></span>
              <span className="vote-count">{c.count}</span>
              {data.winner_key === c.key ? <span className="badge funded">Winner</span> : null}
            </button>
          </li>
        ))}
      </ul>
      <p className="muted">
        {data.voted} voted · Quorum: {data.quorum}
        {data.your_choice ? <> · Your vote: {data.your_choice} <span aria-hidden="true">✓</span></> : null}
      </p>
      {!closed ? <p className="fine">Tap a choice or reply 1, 2, or 3. You can change your vote.</p> : null}
    </article>
  );
}
