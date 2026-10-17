import LowerLimit from "./LowerLimit";
import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";

export default function MandateBox({
  data, cap, onCap,
}: {
  data: ConsentPayload; cap: number; onCap: (cents: number) => void;
}) {
  const deadline = new Date(data.deadline_at);
  const label = deadline.toLocaleString("en-US", {
    weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/Los_Angeles",
  });
  return (
    <section className="consent-card">
      <p className="kicker">Your spending limit (mandate)</p>
      <p><strong>Max {formatUsd(cap)}</strong> · {data.purchase.merchant_name} only</p>
      <p className="muted">Expires {label} (deadline)</p>
      <LowerLimit shareCents={data.share_cents} defaultCap={data.default_cap_cents} cap={cap} onChange={onCap} />
    </section>
  );
}
