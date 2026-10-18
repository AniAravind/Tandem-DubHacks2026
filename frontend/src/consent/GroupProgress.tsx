import type { ConsentPayload } from "./api";

// "3 of 6 are in" with a ring and one circle per seat. Circles are anonymous on purpose:
// Tandem never shows who has or hasn't paid, only counts, plus which circle is you.
export default function GroupProgress({ data }: { data: ConsentPayload }) {
  const total = Math.max(1, data.group_counts?.active ?? data.purchase.split_count);
  const meHeld = data.share_status === "held";
  const held = Math.min(total, data.group_counts?.held ?? 0);
  const others = Math.max(0, held - (meHeld ? 1 : 0));
  const r = 26, c = 2 * Math.PI * r, filled = (held / total) * c;
  const invitedBy = data.invited_by && data.invited_by !== data.member.display_name ? data.invited_by : null;
  return (
    <section className="group-progress" aria-labelledby="consent-title">
      <p className="gp-from">{invitedBy ? `${invitedBy} invited you to ` : "You’re organizing "}<strong>{data.group_name}</strong></p>
      <div className="gp-head">
        <svg className="gp-ring" viewBox="0 0 64 64" role="img" aria-label={`${held} of ${total} shares held`}>
          <circle cx="32" cy="32" r={r} className="gp-track" />
          <circle cx="32" cy="32" r={r} className="gp-fill" strokeDasharray={`${filled} ${c}`} transform="rotate(-90 32 32)" />
          <text x="32" y="37" textAnchor="middle">{held}/{total}</text>
        </svg>
        <div>
          <h1 id="consent-title">{held === 0 ? "Be the first one in" : `${held} of ${total} ${held === 1 ? "is" : "are"} in`}</h1>
          <p>{meHeld ? "You’re in. Nobody’s charged until everyone is." : held + 1 === total ? "You’re the last one. You’d complete the group." : `You’d make it ${held + 1}.`}</p>
        </div>
      </div>
      <ol className="gp-seats" aria-hidden="true">
        {Array.from({ length: Math.min(total, 12) }, (_, i) => {
          const isMe = meHeld ? i === others : i === held;
          const cls = i < others || (meHeld && isMe) ? "in" : isMe ? "me" : "";
          return <li key={i} className={cls}>{isMe ? "You" : cls === "in" ? "✓" : ""}</li>;
        })}
      </ol>
    </section>
  );
}
