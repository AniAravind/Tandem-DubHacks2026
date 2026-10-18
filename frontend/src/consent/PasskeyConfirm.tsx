import { useId, useState } from "react";
import { DEMO_OTP, type Assertion } from "./api";

export default function PasskeyConfirm({
  busy, submitLabel, onAssert, onCancel,
}: {
  busy: boolean; submitLabel: string; onAssert: (assertion: Assertion) => void; onCancel: () => void;
}) {
  const id = useId();
  const [otp, setOtp] = useState("");
  return (
    <form
      className="otp-confirm"
      onSubmit={(e) => { e.preventDefault(); if (otp.length === 6) onAssert({ method: "otp", code: otp }); }}
    >
      <label htmlFor={id}>Confirmation code</label>
      <p className="otp-help" id={`${id}-help`}>
        Passkey wasn&apos;t available, so we sent a code instead. Simulated SMS: <strong className="money">{DEMO_OTP}</strong>
      </p>
      <input
        id={id}
        className="field otp-field money"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        aria-describedby={`${id}-help`}
        autoFocus
        value={otp}
        onChange={(e) => setOtp(e.target.value.replace(/\D/g, ""))}
      />
      <div className="otp-actions">
        <button className="btn consent-choice" type="submit" disabled={busy || otp.length !== 6}>{busy ? "Placing hold…" : submitLabel}</button>
        <button className="btn btn-quiet" type="button" disabled={busy} onClick={onCancel}>Back</button>
      </div>
    </form>
  );
}
