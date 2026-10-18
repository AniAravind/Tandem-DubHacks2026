import type { Assertion } from "./api";

/** Simulated passkey tap. Resolves null when the browser has no passkey support or the member cancels. */
export async function requestPasskey(displayName: string, signal?: AbortSignal): Promise<Assertion | null> {
  if (!window.PublicKeyCredential || !navigator.credentials) return null;
  try {
    const cred = await navigator.credentials.create({
      signal,
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rp: { name: "Tandem", id: window.location.hostname },
        user: { id: crypto.getRandomValues(new Uint8Array(16)), name: "tandem", displayName: displayName || "member" },
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
        timeout: 30000,
      },
    });
    if (!cred) return null;
    return { method: "passkey", credential_id: cred.id, client_data_json: "demo", signature: "demo-passkey" };
  } catch {
    return null;
  }
}
