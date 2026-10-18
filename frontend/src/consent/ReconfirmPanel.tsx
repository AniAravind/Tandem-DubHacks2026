import { formatUsd } from "./money";
import type { ConsentPayload } from "./api";

type Reconfirmation = NonNullable<ConsentPayload["reconfirmation"]>;

/** Shown when a fallback changed this member's share. Within the approved limit it is a one-tap confirm. */
export default function ReconfirmPanel({ change }: { change: Reconfirmation }) {
  const delta = change.to_cents - change.from_cents;
  const direction = delta > 0 ? "up" : delta < 0 ? "down" : "same";
  return (
    <section className={`reconfirm-panel ${change.within_cap ? "within" : "over"}`} aria-labelledby="reconfirm-title">
      <p className="eyebrow">THE PLAN CHANGED</p>
      <h2 id="reconfirm-title">{change.within_cap ? "Confirm your new share" : "Your new share needs a new limit"}</h2>
      <p className="reconfirm-amounts money">
        <s aria-label={`was ${formatUsd(change.from_cents)}`}>{formatUsd(change.from_cents)}</s>
        <span aria-hidden="true">→</span>
        <strong>{formatUsd(change.to_cents)}</strong>
        {direction !== "same" && <span className={`delta ${direction}`}>{delta > 0 ? "+" : "−"}{formatUsd(Math.abs(delta))}</span>}
      </p>
      {change.within_cap && change.cap_cents != null ? (
        <p>That&apos;s within the {formatUsd(change.cap_cents)} limit you already approved, so your card stays the same. Confirm below, or leave.</p>
      ) : change.cap_cents != null ? (
        <p>That&apos;s above the {formatUsd(change.cap_cents)} limit you approved. Review the new limit below and approve it again, or leave. Nothing changes until you do.</p>
      ) : (
        <p>Review the details below and approve your new share, or leave. Nothing is held until you do.</p>
      )}
    </section>
  );
}
