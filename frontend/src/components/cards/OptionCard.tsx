import { useState } from "react";
import { usd } from "../../api/client";
import type { OptionCardData } from "../../types/contracts";
import { CatalogTag, type CardProps } from "./common";

const FEATURES: Record<string, string> = { hot_tub: "Hot tub", kitchen: "Kitchen", separate_beds: "Separate beds", parking: "Parking" };
const label = (f: string) => FEATURES[f] ?? f.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

function FitMeter({ score }: { score: number }) {
  const pct = Math.min(100, Math.max(0, score));
  return (
    <div className="fit-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label={`Group fit ${pct} out of 100`}>
      <span className="fit-label">Group fit</span>
      <span className="fit-track" aria-hidden="true"><span style={{ width: `${pct}%` }} /></span>
      <span className="fit-value money">{pct}</span>
    </div>
  );
}

function OptionPhoto({ data }: { data: OptionCardData }) {
  const [failed, setFailed] = useState(false);
  if (data.photo_url && !failed) {
    return <img className="option-photo" src={data.photo_url} alt="" loading="lazy" onError={() => setFailed(true)} />;
  }
  return <div className="option-photo option-photo-fallback" aria-hidden="true"><span>{data.title.slice(0, 1)}</span></div>;
}

export function OptionCard({ data, onReply }: CardProps<OptionCardData>) {
  return (
    <article className={`card option-card${data.rank === 1 ? " top-pick" : ""}`} aria-label={`Option ${data.rank}: ${data.title}`}>
      <div className="option-media">
        <OptionPhoto data={data} />
        <span className="rank">{data.rank === 1 ? "Top pick" : `Option ${data.rank}`}</span>
      </div>
      <h3 className="card-title">{data.title}</h3>
      <p className="option-merchant">{data.merchant_name} <CatalogTag /></p>
      <p className="per-person money">
        <strong>{usd(data.per_person_cents)}</strong> each <span className="muted">· {usd(data.total_cents)} total · up to {data.capacity}</span>
      </p>
      {data.features.length > 0 && (
        <ul className="feature-chips" aria-label="Features">
          {data.features.map((f) => <li key={f}>{label(f)}</li>)}
        </ul>
      )}
      {data.why.length || data.tradeoff ? (
        <div className="why">
          <h4>Why this pick</h4>
          <ul>
            {data.why.map((w) => (
              <li key={w}><span aria-hidden="true">✓ </span>{w}</li>
            ))}
            {data.tradeoff ? <li className="tradeoff"><span aria-hidden="true">↔ </span>Trade-off: {data.tradeoff}</li> : null}
          </ul>
        </div>
      ) : null}
      {data.fit_score > 0 && <FitMeter score={data.fit_score} />}
      <div className="card-actions">
        <button className="btn primary" aria-label={`Choose ${data.title}`} onClick={() => onReply(String(data.rank))}>Choose this</button>
      </div>
      {data.cancellation ? <p className="fine">{data.cancellation}</p> : null}
    </article>
  );
}
