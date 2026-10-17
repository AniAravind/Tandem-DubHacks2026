import { formatUsd, offerCents } from "./money";
import type { ConsentPayload } from "./api";

export default function ShareSummary({ data }: { data: ConsentPayload }) {
  const offer = offerCents(data.offers, data.share_cents);
  return (
    <section className="consent-card">
      <p className="kicker">Your share</p>
      <p className="share-amount money">{formatUsd(data.share_cents)}</p>
      <p className="muted">(of {formatUsd(data.purchase.total_cents, false)})</p>
      <p>
        Split {data.purchase.split_count} ways · Offer: {offer
          ? `−${formatUsd(offer)} (simulated)`
          : "−$0 (none)"}
      </p>
    </section>
  );
}
