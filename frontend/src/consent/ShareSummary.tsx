import { Tag } from "./Icons";
import { formatUsd, offerCents } from "./money";
import type { ConsentPayload } from "./api";

export default function ShareSummary({ data }: { data: ConsentPayload }) {
  const offer = offerCents(data.offers, data.share_cents);
  const n = data.purchase.split_count;
  return (
    <section className="card" aria-labelledby="share-label">
      <p className="kicker" id="share-label">Your share</p>
      <p className="share-amount money">{formatUsd(data.share_cents)}</p>
      <p className="share-sub money">of {formatUsd(data.purchase.total_cents)} total · split {n} ways</p>
      <div className="seats" aria-hidden="true">
        {Array.from({ length: n }, (_, i) => <span key={i} className={i === 0 ? "seat you" : "seat"} />)}
      </div>
      <div className="seats-legend" aria-hidden="true"><span>You</span><span>{n - 1} others</span></div>
      {offer > 0 && (
        <p className="offer"><Tag size={16} />Offer applied: −{formatUsd(offer)} (simulated)</p>
      )}
    </section>
  );
}
