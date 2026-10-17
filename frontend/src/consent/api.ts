const DEMO_OTP = "000000";

export type ConsentPayload = {
  member: { id: string; display_name: string };
  invited_by: string;
  group_name: string;
  purchase: {
    id: string;
    version: number;
    title: string;
    merchant_name: string;
    merchant_slug: string;
    dates: string;
    total_cents: number;
    split_count: number;
    photo_url?: string;
  };
  share_cents: number;
  default_cap_cents: number;
  deadline_at: string;
  can: string[];
  can_never: string[];
  consent_text_version: string;
  consent_text_sha256: string;
  simulated: true;
  offers: { offer_id: string; merchant_id: string; description: string; percent_back: number }[];
  share_id: string;
  group_id: string;
  share_status: string;
};

export type ApiError = { error: { code: string; message: string; decline_code?: string } };

export type Assertion = {
  method: "otp" | "passkey";
  code?: string;
  credential_id?: string;
  client_data_json?: string;
  signature?: string;
};

async function req(path: string, init: RequestInit = {}, memberId?: string) {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (memberId) headers.set("X-Tandem-Member", memberId);
  const res = await fetch(path, { ...init, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = data as ApiError;
    throw Object.assign(new Error(err.error?.message || res.statusText), {
      status: res.status,
      code: err.error?.code,
      decline_code: err.error?.decline_code,
    });
  }
  return data;
}

export function getConsent(token: string) {
  return req(`/api/v1/consent/${token}`) as Promise<ConsentPayload>;
}

export function tokenize(token: string, last4: string) {
  const rand = Math.random().toString(36).slice(2, 6);
  return req(`/api/v1/consent/${token}/tokenize`, {
    method: "POST",
    body: JSON.stringify({ transient_card_token: `tt_sim_${last4}_${rand}` }),
  }) as Promise<{ agent_token_id: string; card: { brand: string; last4: string }; simulated: true }>;
}

export function approve(token: string, body: {
  agent_token_id: string;
  cap_cents: number;
  consent_text_sha256: string;
  assertion: Assertion;
}) {
  return req(`/api/v1/consent/${token}/approve`, { method: "POST", body: JSON.stringify(body) });
}

export function declineConsent(token: string, reason: string) {
  return req(`/api/v1/consent/${token}/decline`, { method: "POST", body: JSON.stringify({ reason }) });
}

export function leaveShare(shareId: string, memberId: string) {
  return req(`/api/v1/shares/${shareId}/leave`, { method: "POST" }, memberId);
}

export function reconfirmShare(shareId: string, memberId: string, assertion: Assertion) {
  return req(`/api/v1/shares/${shareId}/reconfirm`, {
    method: "POST",
    body: JSON.stringify({ assertion }),
  }, memberId);
}

export { DEMO_OTP };
