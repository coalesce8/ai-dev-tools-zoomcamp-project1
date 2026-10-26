// Formats a duration in seconds as "2m 5s", "1h 2m", "1d 1h": the two
// largest non-zero units, seconds floored. Negative gives "0s", NaN gives "—".
export function formatSeconds(s: number): string {
  if (Number.isNaN(s)) {
    return "—";
  }
  const total = Math.max(0, Math.floor(s));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const parts: [number, string][] = [
    [days, "d"],
    [hours, "h"],
    [minutes, "m"],
    [seconds, "s"],
  ];
  const first = parts.findIndex(([n]) => n > 0);
  if (first === -1) {
    return "0s";
  }
  return parts
    .slice(first, first + 2)
    .filter(([n]) => n > 0)
    .map(([n, unit]) => `${n}${unit}`)
    .join(" ");
}

// Formats a 0..1 rate as a percentage with at most one decimal; null gives "—".
export function formatRate(rate: number | null): string {
  if (rate === null || Number.isNaN(rate)) {
    return "—";
  }
  return `${Number((rate * 100).toFixed(1))}%`;
}

// Seconds from an ISO timestamp to nowMs, clamped at 0. NaN if unparseable.
export function ageSeconds(isoTimestamp: string, nowMs: number): number {
  const then = Date.parse(isoTimestamp);
  if (Number.isNaN(then)) {
    return NaN;
  }
  return Math.max(0, (nowMs - then) / 1000);
}
