// Display formatting only. Values arrive as the tools recorded them; nothing here computes a
// money value (CLAUDE.md, UI track): "50.00" is shown as "$50.00", never rounded or summed.
import type { Limit } from "./types";

export const MISSING = "—";

export function showLimit(limit: Limit): string {
  if (limit.value === null) return MISSING;
  switch (limit.unit) {
    case "usd":
      return `$${limit.value}`;
    case "time":
      return showTime(limit.value);
    case "seconds":
      return `${limit.value} s`;
    default:
      return limit.value;
  }
}

/** "15:55" (New York) -> "3:55 PM ET". Anything else is shown as given. */
export function showTime(hhmm: string): string {
  const m = /^(\d{2}):(\d{2})$/.exec(hhmm);
  if (!m) return hhmm;
  const h = Number(m[1]);
  const suffix = h < 12 ? "AM" : "PM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${m[2]} ${suffix} ET`;
}

export function showCount(n: number | null): string {
  return n === null ? MISSING : n.toLocaleString("en-US");
}
