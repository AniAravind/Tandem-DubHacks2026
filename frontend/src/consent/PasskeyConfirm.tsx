import { useState } from "react";
import { DEMO_OTP, type Assertion } from "./api";

export default function PasskeyConfirm({
  onAssert, busy,
}: {
  onAssert: (assertion: Assertion) => void; busy: boolean;
}) {
  const [otp, setOtp] = useState("");
  const [useOtp, setUseOtp] = useState(false);

  async function withPasskey() {
    try {
      const cred = await navigator.credentials.create({
        publicKey: {
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          rp: { name: "Tandem", id: window.location.hostname },
          user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "tandem-demo", displayName: "Tandem demo" },
          pubKeyCredParams: [{ type: "public-key", alg: -7 }],
          timeout: 60000,
        },
      });
      if (!cred) throw new Error("cancelled");
      onAssert({
        method: "passkey",
        credential_id: cred.id,
        client_data_json: "demo",
        signature: "demo-passkey",
      });
    } catch {
      setUseOtp(true);
    }
  }

  if (useOtp) {
    return (
      <div>
        <label htmlFor="otp">Demo code (also in the server log)</label>
        <input id="otp" className="field" inputMode="numeric" maxLength={6} value={otp} onChange={(e) => setOtp(e.target.value)} />
        <button className="btn btn-primary" type="button" disabled={busy || otp.length !== 6}
          onClick={() => onAssert({ method: "otp", code: otp || DEMO_OTP })}>
          Confirm with code
        </button>
      </div>
    );
  }

  return (
    <button className="btn" type="button" disabled={busy} onClick={withPasskey}>
      Confirm with Face ID / passkey
    </button>
  );
}
