import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";

export default function ShareSummary({ data }: { data: ConsentPayload }) {
  const split = data.purchase.split_count;
  const details = [data.purchase.dates, data.purchase.merchant_name].filter(Boolean).join(" · ");
  return (
    <section className="share-summary" aria-label="Your share">
      <p className="kicker">Your share · {data.purchase.title}</p>
      <p className="share-amount money">{formatUsd(data.share_cents)}</p>
      <p className="share-meta">{formatUsd(data.purchase.total_cents)} split {split} {split === 1 ? "way" : "ways"}{details ? ` · ${details}` : ""}</p>
      <p className="hold-note">Held, not charged, until everyone&apos;s in.</p>
    </section>
  );
}
