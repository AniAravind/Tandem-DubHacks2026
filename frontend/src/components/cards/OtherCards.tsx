import { usd, usdExact } from "../../api/client";
import type { BookingConfirmedData, ExpenseCardData } from "../../types/contracts";
import { type CardProps } from "./common";

export function BookingConfirmed({ data }: CardProps<BookingConfirmedData>) {
  return (
    <article className="card booking-card" aria-label="Plan confirmed">
      <div className="card-row">
        <h3 className="card-title"><span aria-hidden="true">🎉 </span>Plan confirmed: {data.title}</h3>

      </div>
      <p>{data.dates} · Plan reference <strong>{data.booking_ref}</strong></p>
      <p className="money">Approved share: {usd(data.per_person_cents)} each</p>
      <p className={data.signature_verified ? "ok" : "bad"}>
        <span aria-hidden="true">{data.signature_verified ? "✓ " : "✕ "}</span>
        {data.signature_verified ? "Signed agent checkout verified" : "Signature not verified"}
      </p>
      <a className="btn" href={data.dashboard_url}>Trip dashboard →</a>
    </article>
  );
}

export function ExpenseCard({ data }: CardProps<ExpenseCardData>) {
  return (
    <article className="card expense-card" aria-label={`Expense: ${data.description}`}>
      <h3 className="card-title">{data.description} <span className="money">{usdExact(data.amount_cents)}</span></h3>
      <p>Paid by {data.payer} · split {data.split_count} ways</p>
      <p className="money"><strong>{usdExact(data.per_person_cents)}</strong> each</p>
    </article>
  );
}

// Card types without a frozen contract shape yet; rendered defensively.
interface LedgerData { balances?: { member_id: string; display_name: string; net_cents: number }[]; net_total_cents?: number }
interface ErrorData { message?: string }
interface PreferenceData { url?: string }
interface PlanData { lines?: string[]; title?: string }

export function LedgerCard({ data }: CardProps<LedgerData>) {
  const rows = data.balances ?? [];
  return (
    <article className="card ledger-card" aria-label="Balances">
      <h3 className="card-title">Balances</h3>
      <table>
        <tbody>
          {rows.map((r) => (
            <tr key={r.member_id}>
              <th scope="row">{r.display_name}</th>
              <td className={`money ${r.net_cents < 0 ? "bad" : "ok"}`}>{r.net_cents < 0 ? "owes " : "gets "}{usdExact(Math.abs(r.net_cents))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="fine">Balances net to {usdExact(data.net_total_cents ?? 0)} {data.net_total_cents === 0 ? "✓" : ""}</p>
    </article>
  );
}

export function ErrorCard({ data }: CardProps<ErrorData>) {
  return (
    <article className="card error-card" role="alert">
      <p><span aria-hidden="true">⚠ </span>{data.message ?? "Something went wrong. Nothing was charged."}</p>
    </article>
  );
}

export function PreferenceRequest({ data }: CardProps<PreferenceData>) {
  return (
    <article className="card pref-card">
      <p>Quick private check so I don't suggest anything that doesn't work for you.</p>
      {data.url ? <a className="btn primary" href={data.url}>Set my budget & must-haves →</a> : null}
      <p className="fine">Only you see your answers.</p>
    </article>
  );
}

export function PlanSummary({ data }: CardProps<PlanData>) {
  return (
    <article className="card plan-card">
      {data.title ? <h3 className="card-title">{data.title}</h3> : null}
      <ul>{(data.lines ?? []).map((l) => <li key={l}>{l}</li>)}</ul>
    </article>
  );
}
