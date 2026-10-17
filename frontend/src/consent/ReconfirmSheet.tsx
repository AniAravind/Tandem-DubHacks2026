import { formatUsd } from "./money";

export default function ReconfirmSheet({
  fromCents, toCents, capCents, busy, onConfirm, onLeave, onClose,
}: {
  fromCents: number; toCents: number; capCents: number; busy: boolean;
  onConfirm: () => void; onLeave: () => void; onClose: () => void;
}) {
  const over = toCents > capCents;
  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="reconfirm-title">
      <div className="sheet-card">
        <div className="grabber" />
        <h2 className="sheet-title" id="reconfirm-title">{over ? "Above your limit" : "Your share changed"}</h2>
        <p className="money">
          {over
            ? `New share ${formatUsd(toCents)} is above your ${formatUsd(capCents)} limit. Approve a new limit or leave.`
            : `Your share changed ${formatUsd(fromCents)} to ${formatUsd(toCents)} (within your ${formatUsd(capCents)} limit). Confirm?`}
        </p>
        <div className="actions-row">
          <button className="btn" type="button" disabled={busy} onClick={onLeave}>Leave</button>
          {over
            ? <button className="btn btn-primary" type="button" disabled={busy} onClick={onClose}>Review new limit</button>
            : <button className="btn btn-primary" type="button" disabled={busy} onClick={onConfirm}>Confirm</button>}
        </div>
      </div>
    </div>
  );
}
