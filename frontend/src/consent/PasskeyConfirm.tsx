import { useState } from "react";
import type { Assertion } from "./api";

export async function requestPasskey(displayName: string): Promise<Assertion> {
  const cred = await navigator.credentials.create({
    publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rp: { name: "Tandem", id: window.location.hostname },
      user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "tandem-demo", displayName },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      timeout: 15000,
    },
  });
  if (!cred) throw new Error("cancelled");
  return { method: "passkey", credential_id: cred.id, client_data_json: "demo", signature: "demo-passkey" };
}

export default function OtpConfirm({
  onAssert, busy,
}: {
  onAssert: (assertion: Assertion) => void; busy: boolean;
}) {
  const [code, setCode] = useState("");
  return (
    <div className="otp">
      <label htmlFor="otp">Passkey unavailable. Enter the 6-digit code we sent (demo: <strong>000000</strong>).</label>
      <input
        id="otp"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={code}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
      />
      <button className="btn btn-primary btn-block" type="button" disabled={busy || code.length !== 6}
        onClick={() => onAssert({ method: "otp", code })}>
        Confirm code
      </button>
    </div>
  );
}
