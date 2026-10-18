import { formatUsd } from "./money";

export default function ReconfirmSheet({
  fromCents, toCents, capCents, onConfirm, onLeave, onClose,
}: {
  fromCents: number; toCents: number; capCents: number;
  onConfirm: () => void; onLeave: () => void; onClose: () => void;
}) {
  const over = toCents > capCents;
  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="reconfirm-title">
      <div className="sheet-card">
        <h2 id="reconfirm-title">{over ? "New limit needed" : "Confirm your new share"}</h2>
        {over ? (
          <p>New share {formatUsd(toCents)} is above your {formatUsd(capCents)} limit. Approve a new limit or leave.</p>
        ) : (
          <p>
            Your share changed {formatUsd(fromCents)} to {formatUsd(toCents)} (within your {formatUsd(capCents)} limit). Confirm?
          </p>
        )}
        {!over && <button className="btn btn-primary" type="button" onClick={onConfirm}>Confirm changed share (demo code)</button>}
        <button className="btn" type="button" onClick={onLeave}>Leave</button>
        <button className="btn" type="button" onClick={onClose}>Not now</button>
      </div>
    </div>
  );
}
