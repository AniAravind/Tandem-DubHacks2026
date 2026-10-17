import { useId } from "react";
import { Lock } from "./Icons";

export default function SecureCardField({
  pan, onPan, errorId,
}: {
  pan: string; onPan: (value: string) => void; errorId?: string;
}) {
  const id = useId();
  const digits = pan.replace(/\D/g, "").slice(0, 16);
  const pretty = digits.replace(/(\d{4})(?=\d)/g, "$1 ");
  return (
    <section className="card">
      <label htmlFor={id} className="kicker">Card</label>
      <div className="secure">
        <div className="secure-top">
          <span><Lock size={13} />secure field (tokenized)</span>
          <span className="visa">VISA</span>
        </div>
        <div className="secure-input">
          <input
            id={id}
            inputMode="numeric"
            autoComplete="off"
            placeholder="4111 1111 1111 1111"
            value={pretty}
            aria-describedby={errorId ? `${id}-help ${errorId}` : `${id}-help`}
            aria-invalid={Boolean(errorId)}
            onChange={(e) => onPan(e.target.value)}
          />
          <span className="secure-exp" aria-label="Expiry 12/28">12/28</span>
        </div>
      </div>
      <p id={`${id}-help`} className="hint">
        Test cards only: <code>4111 1111 1111 1111</code> approves, ending <code>0002</code> declines.
        Your card number stays on this device; only a token is sent.
      </p>
    </section>
  );
}

export function last4FromPan(pan: string): string {
  return pan.replace(/\D/g, "").slice(-4).padStart(4, "0");
}
