import { useId } from "react";

export default function SecureCardField({
  pan, onPan, errorId,
}: {
  pan: string; onPan: (value: string) => void; errorId?: string;
}) {
  const id = useId();
  const digits = pan.replace(/\D/g, "").slice(0, 16);
  const pretty = digits.replace(/(\d{4})(?=\d)/g, "$1 ").trim();
  return (
    <section className="consent-card">
      <label htmlFor={id}>Card</label>
      <div className="secure-field">
        <div className="secure-label">Simulated secure field · tokenized</div>
        <input
          id={id}
          className="field"
          inputMode="numeric"
          autoComplete="off"
          placeholder="4111 1111 1111 1111"
          value={pretty}
          aria-describedby={errorId ? `${id}-help ${errorId}` : `${id}-help`}
          onChange={(e) => onPan(e.target.value)}
        />
      </div>
      <p id={`${id}-help`} className="muted">
        Test cards only in demo: 4111 1111 1111 1111 approves, ending 0002 declines.
      </p>
    </section>
  );
}
