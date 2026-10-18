import LowerLimit from "./LowerLimit";
import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";

export default function MandateBox({
  data, cap, onCap,
}: {
  data: ConsentPayload; cap: number; onCap: (cents: number) => void;
}) {
  const label = new Date(data.deadline_at).toLocaleString("en-US", {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/Los_Angeles",
  });
  return (
    <section className="consent-box mandate-box" aria-labelledby="mandate-title">
      <p className="kicker" id="mandate-title">Your spending limit (mandate)</p>
      <dl className="mandate-rows">
        <div><dt>Most it can hold</dt><dd className="money">{formatUsd(cap)}</dd></div>
        <div><dt>Only at</dt><dd>{data.purchase.merchant_name}</dd></div>
        <div><dt>Only for</dt><dd>{data.purchase.title}</dd></div>
        <div><dt>Expires</dt><dd>{label}</dd></div>
      </dl>
      {data.above_budget && (
        <p className="notice pending" role="note">
          This share is above the private budget you set. Approving sets a new {formatUsd(cap)} limit for this purchase only.
        </p>
      )}
      <LowerLimit shareCents={data.share_cents} defaultCap={data.default_cap_cents} cap={cap} onChange={onCap} />
    </section>
  );
}
