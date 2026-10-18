import { useId } from "react";

export default function SecureCardField({
  pan, onPan, errorId, disabled,
}: {
  pan: string; onPan: (value: string) => void; errorId?: string; disabled?: boolean;
}) {
  const id = useId();
  const digits = pan.replace(/\D/g, "").slice(0, 16);
  const pretty = digits.replace(/(\d{4})(?=\d)/g, "$1 ").trim();
  return (
    <section className="consent-box card-box">
      <label className="kicker" htmlFor={id}>Pay with</label>
      <div className="secure-field">
        <div className="secure-label">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>
          Payment card
          <span className="card-brand" aria-hidden="true">VISA</span>
        </div>
        <input
          id={id}
          className="field card-field money"
          inputMode="numeric"
          autoComplete="off"
          placeholder="4111 1111 1111 1111"
          value={pretty}
          disabled={disabled}
          aria-invalid={errorId ? true : undefined}
          aria-describedby={errorId || undefined}
          onChange={(e) => onPan(e.target.value)}
        />
      </div>
    </section>
  );
}
