const REASONS = [
  { id: "too_expensive", label: "Too expensive" },
  { id: "dates", label: "Dates don't work" },
  { id: "other", label: "Other" },
  { id: "prefer_not_to_say", label: "Prefer not to say" },
] as const;

export default function DeclineSheet({
  onPick, onClose,
}: {
  onPick: (reason: string) => void; onClose: () => void;
}) {
  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="decline-title">
      <div className="sheet-card">
        <h2 id="decline-title">Not for me</h2>
        <p className="muted">This reason stays private. The group only sees that a spot opened up.</p>
        {REASONS.map((r) => (
          <button key={r.id} className="btn reason" type="button" onClick={() => onPick(r.id)}>{r.label}</button>
        ))}
        <button className="btn" type="button" onClick={onClose}>Keep this hold option</button>
      </div>
    </div>
  );
}
