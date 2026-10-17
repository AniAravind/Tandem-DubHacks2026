import { Link } from "react-router-dom";
import { Check, LockCheck } from "./Icons";
import { formatUsd } from "./money";

const STEPS = [
  { label: "Held", note: "Your share is reserved, not charged" },
  { label: "Group funded", note: "Everyone has held their share" },
  { label: "Booked", note: "Tandem books with a signed checkout" },
  { label: "Charged", note: "Only now does money move" },
];

export default function HeldSuccess({
  shareCents, last4, splitCount, groupId,
}: {
  shareCents: number; last4: string; splitCount: number; groupId: string;
}) {
  return (
    <section className="card state">
      <div className="state-icon ok pop"><LockCheck size={36} /></div>
      <h1 className="state-title">Held, not charged.</h1>
      <p className="muted">
        <span className="money">{formatUsd(shareCents)}</span> is on hold on Visa •••• {last4}.
        You&apos;ll be charged only when all {splitCount} are in.
      </p>
      <ol className="steps">
        {STEPS.map((s, i) => (
          <li key={s.label} className={i === 0 ? "step done" : "step"}>
            <span className="step-dot">{i === 0 ? <Check size={14} /> : i + 1}</span>
            <span className="step-text">{s.label}<small>{s.note}</small></span>
          </li>
        ))}
      </ol>
      <div className="state-actions">
        <Link className="btn btn-primary" to={`/chat/${groupId}`}>Back to chat</Link>
        <Link className="btn" to={`/g/${groupId}`}>View trip</Link>
      </div>
    </section>
  );
}
