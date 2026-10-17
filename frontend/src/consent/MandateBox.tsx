import { Clock, Shield, Store } from "./Icons";
import LowerLimit from "./LowerLimit";
import { deadlineLabel, formatUsd } from "./money";
import type { ConsentPayload } from "./api";

export default function MandateBox({
  data, cap, onCap,
}: {
  data: ConsentPayload; cap: number; onCap: (cents: number) => void;
}) {
  return (
    <section className="card" aria-labelledby="mandate-label">
      <p className="kicker" id="mandate-label">Your spending limit (mandate)</p>
      <div className="rows">
        <div className="row">
          <span className="row-icon"><Shield /></span>
          <div><p className="row-label">Maximum</p><p className="row-value money">{formatUsd(cap)}</p></div>
        </div>
        <div className="row">
          <span className="row-icon"><Store /></span>
          <div><p className="row-label">Merchant lock</p><p className="row-value">{data.purchase.merchant_name} only</p></div>
        </div>
        <div className="row">
          <span className="row-icon"><Clock /></span>
          <div><p className="row-label">Expires at the deadline</p><p className="row-value">{deadlineLabel(data.deadline_at)}</p></div>
        </div>
      </div>
      <LowerLimit shareCents={data.share_cents} defaultCap={data.default_cap_cents} cap={cap} onChange={onCap} />
    </section>
  );
}
