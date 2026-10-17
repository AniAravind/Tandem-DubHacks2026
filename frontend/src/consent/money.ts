export function formatUsd(cents: number, withCents = true): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const dollars = Math.floor(abs / 100).toLocaleString("en-US");
  if (!withCents) return `${sign}$${dollars}`;
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}$${dollars}.${frac}`;
}

export function offerCents(offers: { percent_back?: number }[] | undefined, shareCents: number): number {
  const pct = offers?.[0]?.percent_back ?? 0;
  return Math.round((shareCents * pct) / 100);
}

export function deadlineLabel(iso: string): string {
  const at = new Date(iso);
  const abs = at.toLocaleString("en-US", {
    weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/Los_Angeles",
  });
  const mins = Math.round((at.getTime() - Date.now()) / 60000);
  if (mins <= 0) return abs;
  const rel = mins < 60 ? `${mins}m` : mins < 48 * 60 ? `${Math.round(mins / 60)}h` : `${Math.round(mins / 1440)}d`;
  return `${abs} (in ${rel})`;
}
