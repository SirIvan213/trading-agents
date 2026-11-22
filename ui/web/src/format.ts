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

/** A recorded money string as dollars: "-31.65" -> "-$31.65". The digits are never changed. */
export function showMoney(value: string | null): string {
  if (value === null) return MISSING;
  return value.startsWith("-") ? `-$${value.slice(1)}` : `$${value}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
  "Dec"];

/** A recorded New York ISO time ("2026-11-23T10:05:00-05:00") as "Nov 23 10:05", read from
 * the text itself: the browser's own time zone is never applied. */
export function showWhen(iso: string | null): string {
  if (iso === null) return MISSING;
  const m = /^\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${MONTHS[Number(m[1]) - 1]} ${Number(m[2])} ${m[3]}:${m[4]}`;
}
