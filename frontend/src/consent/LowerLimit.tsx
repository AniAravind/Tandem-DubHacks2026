import { formatUsd } from "./money";

export default function LowerLimit({
  shareCents, defaultCap, cap, onChange,
}: {
  shareCents: number; defaultCap: number; cap: number; onChange: (cents: number) => void;
}) {
  return (
    <details>
      <summary>Lower my limit</summary>
      <label htmlFor="cap">Max I will hold</label>
      <input
        id="cap"
        className="field"
        type="range"
        min={shareCents}
        max={defaultCap}
        step={100}
        value={cap}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <p className="money">{formatUsd(cap)}</p>
    </details>
  );
}
