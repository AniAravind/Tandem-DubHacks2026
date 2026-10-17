import { Link } from "react-router-dom";
import { formatUsd } from "./money";

export default function HeldSuccess({
  shareCents, last4, splitCount, groupId,
}: {
  shareCents: number; last4: string; splitCount: number; groupId: string;
}) {
  return (
    <section className="consent-card">
      <div className="success-icon lock-pop" aria-hidden>🔒✓</div>
      <h1>Held, not charged.</h1>
      <p>
        {formatUsd(shareCents)} is on hold on Visa •••• {last4}. You&apos;ll be charged only when all {splitCount} are in.
      </p>
      <ol className="timeline">
        <li>Held ✓</li>
        <li>Group funded</li>
        <li>Booked</li>
        <li>Charged</li>
      </ol>
      <p className="muted">Simulated payment: no real money moves in this demo.</p>
      <div style={{ display: "grid", gap: 8 }}>
        <Link className="btn btn-primary" to={`/chat/${groupId}`} style={{ textAlign: "center" }}>Back to chat</Link>
        <Link className="btn" to={`/g/${groupId}`} style={{ textAlign: "center" }}>View trip</Link>
      </div>
    </section>
  );
}
