import { usd } from "../../api/client";
import type { OptionCardData } from "../../types/contracts";
import { DemoTag, type CardProps } from "./common";

const FEATURES: Record<string, string> = { hot_tub: "Hot tub", kitchen: "Kitchen", separate_beds: "Separate beds", parking: "Parking" };

function FitDots({ score }: { score: number }) {
  const filled = Math.min(5, Math.max(0, Math.round(score / 20)));
  return (
    <span className="fit" aria-label={`Fit score ${score} out of 100`}>
      <span aria-hidden="true">{"●".repeat(filled) + "○".repeat(5 - filled)}</span> {score}
    </span>
  );
}

export function OptionCard({ data, onReply }: CardProps<OptionCardData>) {
  return (
    <article className="card option-card" aria-label={`Option ${data.rank}: ${data.title}`}>
      {data.photo_url ? <img className="option-photo" src={data.photo_url} alt="" loading="lazy" onError={e => { e.currentTarget.style.display = "none"; }} /> : null}
      <div className="card-row">
        <h3 className="card-title">{data.title}</h3>
        <span className="rank">#{data.rank}</span>
      </div>
      <p className="per-person money">
        <strong>{usd(data.per_person_cents)}</strong> /person <span className="muted">· {usd(data.total_cents)} total</span>
      </p>
      <p className="muted">
        Sleeps {data.capacity}
        {data.features.map((f) => ` · ${FEATURES[f] ?? f.replace(/_/g, " ")}`).join("")}
      </p>
      {data.why.length || data.tradeoff ? (
        <div className="why">
          <h4>Why this pick</h4>
          <ul>
            {data.why.map((w) => (
              <li key={w}><span aria-hidden="true">✓ </span>{w}</li>
            ))}
            {data.tradeoff ? <li><span aria-hidden="true">↔ </span>Trade-off: {data.tradeoff}</li> : null}
          </ul>
        </div>
      ) : null}
      <FitDots score={data.fit_score} />
      <div className="card-actions">
        <button className="btn primary" onClick={() => onReply(String(data.rank))}>Vote for this</button>
      </div>
      <p className="fine">
        {data.cancellation ? `${data.cancellation} · ` : ""}{data.merchant_name} <DemoTag />
      </p>
    </article>
  );
}
