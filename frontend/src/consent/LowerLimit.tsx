import { formatUsd } from "./money";

export default function LowerLimit({
  shareCents, defaultCap, cap, onChange,
}: {
  shareCents: number; defaultCap: number; cap: number; onChange: (cents: number) => void;
}) {
  if (defaultCap <= shareCents) return null;
  return (
    <details className="limit">
      <summary>Lower my limit</summary>
      <label htmlFor="cap" className="small muted">
        Max Tandem may hold: <strong className="money" style={{ color: "var(--ink-900)" }}>{formatUsd(cap)}</strong>
      </label>
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
      <div className="range-scale money"><span>{formatUsd(shareCents)}</span><span>{formatUsd(defaultCap)}</span></div>
    </details>
  );
}
