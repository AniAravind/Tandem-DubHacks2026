export default function CanCanNever({ can, canNever }: { can: string[]; canNever: string[] }) {
  return (
    <div className="promise-grid">
      <section className="promise can" aria-labelledby="can-title">
        <p className="kicker" id="can-title">Tandem can</p>
        <ul>
          {can.map((line) => <li key={line}><span className="promise-mark" aria-hidden="true">✓</span>{line}</li>)}
        </ul>
      </section>
      <section className="promise never" aria-labelledby="never-title">
        <p className="kicker" id="never-title">Tandem can never</p>
        <ul>
          {canNever.map((line) => <li key={line}><span className="promise-mark" aria-hidden="true">✕</span>{line}</li>)}
        </ul>
      </section>
    </div>
  );
}
