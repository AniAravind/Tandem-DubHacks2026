import { Clock, XCircle } from "./Icons";

export default function ExpiredLink({ forbidden, message }: { forbidden?: boolean; message?: string }) {
  return (
    <section className="card state">
      <div className={forbidden ? "state-icon bad" : "state-icon neutral"}>
        {forbidden ? <XCircle size={34} /> : <Clock size={34} />}
      </div>
      <h1 className="state-title">{forbidden ? "This link isn't for you" : "This link expired"}</h1>
      <p className="muted">
        {forbidden ? "Links are personal." : message || "Ask Tandem in your chat for a new one."}
      </p>
      <p className="muted small" style={{ marginTop: 14 }}>No card form is shown, and nothing was held.</p>
    </section>
  );
}
