import { formatUsd } from "./money";

export default function LowerLimit({
  shareCents, defaultCap, cap, onChange,
}: {
  shareCents: number; defaultCap: number; cap: number; onChange: (cents: number) => void;
}) {
  if (defaultCap <= shareCents) return null;
  return (
    <details className="lower-limit">
      <summary>Lower my limit</summary>
      <label htmlFor="cap">Max I will hold</label>
      <input
        id="cap"
        className="range"
        type="range"
        min={shareCents}
        max={defaultCap}
        step={100}
        value={cap}
        aria-valuetext={formatUsd(cap)}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <p className="range-scale money"><span>{formatUsd(shareCents)}</span><strong>{formatUsd(cap)}</strong><span>{formatUsd(defaultCap)}</span></p>
    </details>
  );
}
