import { useState } from "react";
import { useDialog } from "../components/layout/useDialog";
import { formatUsd } from "./money";

export default function LeaveSheet({
  shareCents, onLeave, onClose,
}: {
  shareCents: number; onLeave: () => Promise<void>; onClose: () => void;
}) {
  const dialog = useDialog(onClose);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function leave() {
    setBusy(true);
    setError("");
    try {
      await onLeave();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn’t release your hold. Try again.");
      setBusy(false);
    }
  }
  return (
    <div ref={dialog} className="sheet" role="dialog" aria-modal="true" aria-labelledby="leave-title">
      <div className="sheet-card">
        <h2 id="leave-title">Leave this purchase?</h2>
        <p>Your {formatUsd(shareCents)} hold is released right away. The group only sees that a spot opened up, and Tandem will suggest ways to keep the plan on.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="sheet-actions">
          <button className="btn" type="button" disabled={busy} onClick={leave}>{busy ? "Releasing…" : "Release my hold"}</button>
          <button className="btn" type="button" disabled={busy} onClick={onClose}>Stay in</button>
        </div>
      </div>
    </div>
  );
}
