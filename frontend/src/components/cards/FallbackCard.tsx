import { usd } from "../../api/client";
import type { FallbackCardData } from "../../types/contracts";
import type { CardProps } from "./common";

export function FallbackCard({ data, onReply }: CardProps<FallbackCardData>) {
  return (
    <article className="card fallback-card" aria-label="Ways to keep the plan on">
      <h3 className="card-title">3 ways to keep it on</h3>
      <ol className="fallback-list">
        {data.options.map((o, i) => (
          <li key={o.key}>
            <button className="fallback-choice" onClick={() => onReply(String(i + 1))}>
              <span className="vote-key">{i + 1}</span>
              <span>
                <span className="vote-label">{o.label}</span>
                <span className="money">
                  {" "}{usd(o.per_person_cents)} each{o.delta_cents ? ` (${o.delta_cents > 0 ? "+" : ""}${usd(o.delta_cents)})` : ""}
                </span>
                <span className="fine block">{o.cap_impact}</span>
              </span>
            </button>
          </li>
        ))}
      </ol>
      <p className="fine">Reply 1, 2, or 3. Nobody has been charged.</p>
    </article>
  );
}
