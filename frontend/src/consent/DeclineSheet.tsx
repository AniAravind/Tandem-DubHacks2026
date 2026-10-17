import { useEffect, useState } from "react";

const REASONS = [
  { id: "too_expensive", label: "Too expensive" },
  { id: "dates", label: "Dates don't work" },
  { id: "other", label: "Other" },
  { id: "prefer_not_to_say", label: "Prefer not to say" },
] as const;

export default function DeclineSheet({
  onConfirm, onClose,
}: {
  onConfirm: (reason: string) => void; onClose: () => void;
}) {
  const [reason, setReason] = useState("prefer_not_to_say");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="decline-title" onClick={onClose}>
      <div className="sheet-card" onClick={(e) => e.stopPropagation()}>
        <div className="grabber" />
        <h2 className="sheet-title" id="decline-title">Not for you this time?</h2>
        <p className="muted small">Optional and private. The group only sees that a spot opened up.</p>
        <fieldset className="reasons" style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="kicker" style={{ marginBottom: 6 }}>Reason (private)</legend>
          {REASONS.map((r) => (
            <label key={r.id} className="reason">
              <input type="radio" name="reason" value={r.id} checked={reason === r.id} onChange={() => setReason(r.id)} />
              {r.label}
            </label>
          ))}
        </fieldset>
        <div className="actions-row" style={{ marginTop: 6 }}>
          <button className="btn" type="button" onClick={onClose}>Go back</button>
          <button className="btn btn-primary" type="button" onClick={() => onConfirm(reason)}>I'm out</button>
        </div>
      </div>
    </div>
  );
}
