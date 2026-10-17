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
