const TZ = "America/Los_Angeles";

// "Sat 7:00 PM" in the group's timezone.
export function when(iso: string, tz = TZ): string {
  return new Date(iso).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: tz });
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: TZ });
}

// "03:42:10 left" (or "Deadline passed").
export function countdown(iso: string, now: number): string {
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return "Deadline passed";
  const s = Math.floor(ms / 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)} left`;
}

export function initials(name: string): string {
  return name.slice(0, 1).toUpperCase();
}
