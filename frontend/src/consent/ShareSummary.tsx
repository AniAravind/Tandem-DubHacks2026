import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";

export default function ShareSummary({ data }: { data: ConsentPayload }) {
  const split = data.purchase.split_count;
  return (
    <section className="share-summary" aria-label="Your share">
      <p className="kicker">Your share</p>
      <p className="share-amount money">{formatUsd(data.share_cents)}</p>
      <p className="share-meta">of {formatUsd(data.purchase.total_cents)} · split {split} {split === 1 ? "way" : "ways"}</p>
      <div className="seat-bar" aria-hidden="true">
        {Array.from({ length: Math.min(split, 12) }, (_, i) => <span key={i} className={i === 0 ? "mine" : ""} />)}
      </div>
      <p className="hold-note">A hold today. A charge only when everyone&apos;s in.</p>
    </section>
  );
}
