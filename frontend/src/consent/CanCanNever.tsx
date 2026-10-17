import { Check, X } from "./Icons";

export default function CanCanNever({ can, canNever }: { can: string[]; canNever: string[] }) {
  return (
    <section className="card">
      <h2 className="promise-head can"><Check />Tandem CAN</h2>
      <ul className="promise-list can">
        {can.map((line) => <li key={line}><span className="dot"><Check size={14} /></span>{line}</li>)}
      </ul>
      <div className="divider" />
      <h2 className="promise-head never"><X />Tandem can NEVER</h2>
      <ul className="promise-list never">
        {canNever.map((line) => <li key={line}><span className="dot"><X size={14} /></span>{line}</li>)}
      </ul>
    </section>
  );
}
