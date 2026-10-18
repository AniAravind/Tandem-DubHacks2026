import { useState } from "react";
import { Link } from "react-router-dom";
import LeaveSheet from "./LeaveSheet";
import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";
import type { NextLink } from "./StatusScreen";

type Step = { label: string; detail: string; state: "done" | "current" | "todo" };

function steps(data: ConsentPayload): Step[] {
  const state = data.purchase_state ?? "collecting";
  const counts = data.group_counts;
  const funded = ["funded", "booked", "completed"].includes(state);
  const booked = ["booked", "completed"].includes(state);
  const charged = data.share_status === "captured";
  return [
    { label: "Your share is held", detail: "Reserved on your card, not charged.", state: "done" },
    {
      label: "Group funded",
      detail: funded ? "Everyone is in." : counts ? `${counts.held} of ${counts.active} shares held so far.` : "Waiting on the others.",
      state: funded ? "done" : "current",
    },
    { label: "Booked", detail: booked ? "Signed agent checkout confirmed." : "Tandem books only once everyone is in.", state: booked ? "done" : funded ? "current" : "todo" },
    { label: "Charged", detail: charged ? "Your exact share was captured." : "Only your share, only after booking.", state: charged ? "done" : booked ? "current" : "todo" },
  ];
}

export default function HeldSuccess({
  data, last4, links, onLeave,
}: {
  data: ConsentPayload; last4: string; links: NextLink[]; onLeave: () => Promise<void>;
}) {
  const [leaving, setLeaving] = useState(false);
  const canLeave = (data.purchase_state ?? "collecting") === "collecting";
  return (
    <section className="status-screen tone-funded" aria-labelledby="held-title">
      <div className="status-icon lock-pop" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="m9 15.5 2 2 4-4"/></svg>
      </div>
      <p className="eyebrow">YOU&apos;RE IN</p>
      <h1 id="held-title">Held, not charged.</h1>
      <p className="status-lede">
        <strong className="money">{formatUsd(data.share_cents)}</strong> is on hold on {last4 ? `Visa •••• ${last4}` : "your card"}. You&apos;ll be charged only when all {data.purchase.split_count} are in.
      </p>
      <ol className="timeline" aria-label="What happens next">
        {steps(data).map((s) => (
          <li key={s.label} className={`timeline-step ${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
            <span className="timeline-dot" aria-hidden="true">{s.state === "done" ? "✓" : ""}</span>
            <span><strong>{s.label}</strong><small>{s.detail}</small></span>
          </li>
        ))}
      </ol>
      <div className="status-actions">
        {links.map((l, i) => <Link key={l.to} className={`btn${i === 0 ? " btn-primary" : ""}`} to={l.to}>{l.label}</Link>)}
      </div>
      {canLeave ? (
        <button className="btn btn-quiet leave-link" type="button" onClick={() => setLeaving(true)}>Leave and release my hold</button>
      ) : (
        <p className="fine">The group is fully funded, so your hold is locked in for booking.</p>
      )}

      {leaving && <LeaveSheet shareCents={data.share_cents} onLeave={onLeave} onClose={() => setLeaving(false)} />}
    </section>
  );
}
