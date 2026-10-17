// Shared API client (Aryan owns; Ani and Rishab import it). Same origin as Django, so no CORS and no base URL config.

const BASE = "/api/v1";
const MEMBER_KEY = "tandem.demoMember";

export class ApiError extends Error {
  code: string;
  status: number;
  requestId?: string;

  constructor(status: number, body: { error?: { code?: string; message?: string; request_id?: string } } | null) {
    const err = body?.error ?? {};
    super(err.message ?? `Request failed (${status})`);
    this.code = err.code ?? "http_error";
    this.status = status;
    this.requestId = err.request_id;
  }
}

// Phase 0 demo auth: every request names the member it acts as (X-Tandem-Member). Phase 1 swaps in a session.
let demoMember: string | null = typeof localStorage !== "undefined" ? localStorage.getItem(MEMBER_KEY) : null;

export function setDemoMember(memberId: string | null) {
  demoMember = memberId;
  if (typeof localStorage === "undefined") return;
  if (memberId) localStorage.setItem(MEMBER_KEY, memberId);
  else localStorage.removeItem(MEMBER_KEY);
}

export function getDemoMember(): string | null {
  return demoMember;
}

async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(path.startsWith("/api/") ? path : BASE + path, {
    method,
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(demoMember ? { "X-Tandem-Member": demoMember } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

// Paths are relative to /api/v1 (e.g. "/groups/123"); full "/api/..." paths are passed through.
export const api = {
  get: <T>(path: string, headers?: Record<string, string>) => request<T>("GET", path, undefined, headers),
  post: <T>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>("POST", path, body ?? {}, headers),
  put: <T>(path: string, body?: unknown, headers?: Record<string, string>) => request<T>("PUT", path, body ?? {}, headers),
};

// Money is integer cents everywhere. Chat style drops ".00"; receipts/consent use exact().
export function usd(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return abs % 100 === 0
    ? `${sign}$${(abs / 100).toLocaleString("en-US")}`
    : `${sign}$${(abs / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function usdExact(cents: number): string {
  return `${cents < 0 ? "-" : ""}$${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
