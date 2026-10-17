export default function CanCanNever({ can, canNever }: { can: string[]; canNever: string[] }) {
  return (
    <>
      <section className="consent-card">
        <p className="kicker">Tandem CAN</p>
        <ul className="consent-list">
          {can.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </section>
      <section className="consent-card">
        <p className="kicker">Tandem can NEVER</p>
        <ul className="consent-list">
          {canNever.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </section>
    </>
  );
}
