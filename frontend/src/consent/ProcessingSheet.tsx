import { formatUsd } from "./money";

// Shown while a hold is being placed: three short steps with checks, like a card payment going through.
export default function ProcessingSheet({ step, shareCents }: { step: number; shareCents: number }) {
  const steps = ["Verifying your card", `Placing a ${formatUsd(shareCents)} hold`, "Letting the group know"];
  return (
    <div className="processing-backdrop" role="status" aria-live="polite">
      <div className="processing-sheet">
        <div className="processing-spinner" aria-hidden="true" />
        <ol>
          {steps.map((label, i) => (
            <li key={label} className={i < step ? "done" : i === step ? "current" : ""}>
              <span aria-hidden="true">{i < step ? "✓" : ""}</span>{label}{i === step ? "…" : ""}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
