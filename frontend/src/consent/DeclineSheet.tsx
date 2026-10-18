import { useState } from "react";
import { useDialog } from "../components/layout/useDialog";

const REASONS = [
  { id: "too_expensive", label: "Too expensive" },
  { id: "dates", label: "Dates don't work" },
  { id: "other", label: "Something else" },
  { id: "prefer_not_to_say", label: "Prefer not to say" },
] as const;

export default function DeclineSheet({
  onPick, onClose,
}: {
  onPick: (reason: string) => Promise<void>; onClose: () => void;
}) {
  const dialog = useDialog(onClose);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function pick(reason: string) {
    setBusy(true);
    setError("");
    try {
      await onPick(reason);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn’t update your choice. Try again.");
      setBusy(false);
    }
  }
  return (
    <div ref={dialog} className="sheet" role="dialog" aria-modal="true" aria-labelledby="decline-title">
      <div className="sheet-card">
        <h2 id="decline-title">Not for me</h2>
        <p>Pick a reason. It stays private. The group only sees that a spot opened up, and nothing is held.</p>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="reason-list">
          {REASONS.map((r) => (
            <button key={r.id} className="btn reason" type="button" disabled={busy} onClick={() => pick(r.id)}>{r.label}</button>
          ))}
        </div>
        <button className="btn btn-quiet" type="button" disabled={busy} onClick={onClose}>Go back</button>
      </div>
    </div>
  );
}
