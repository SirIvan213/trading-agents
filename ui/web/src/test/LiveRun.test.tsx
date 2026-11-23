// The Live run page rendered from the golden API responses (ui/fixtures/api/live-*.json),
// which ui/tests/test_live.py checks against the real endpoint.
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import calendarDown from "../../../fixtures/api/live-calendar-down.json";
import normal from "../../../fixtures/api/live-normal.json";
import notRunning from "../../../fixtures/api/live-not-running-closed.json";
import stale from "../../../fixtures/api/live-stale-heartbeat.json";
import health from "../../../fixtures/api/health.json";
import { App } from "../App";
import { POLL_MS } from "../pages/LiveRun";
import type { Live } from "../types";

const NORMAL = normal as unknown as Live;

function json(body: unknown, status = 200, etag = '"e1"'): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json", ETag: etag },
  });
}

type Reply = Live | "304" | "down" | number;

/** Serves /api/live from a queue of replies (the last one repeats). */
function serve(...replies: Reply[]) {
  const queue = [...replies];
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url === "/api/health") return json(health);
    const r = queue.length > 1 ? queue.shift()! : queue[0];
    if (r === "304") return new Response(null, { status: 304, headers: { ETag: '"e1"' } });
    if (r === "down") throw new TypeError("Failed to fetch");
    if (typeof r === "number") return json({ detail: "x" }, r);
    return json(r);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function open(...replies: Reply[]) {
  const fetchMock = serve(...replies);
  window.location.hash = "#/live";
  render(<App />);
  await screen.findByText((replies[0] as Live).status?.sentence ?? "Loading…");
  return fetchMock;
}

function liveCalls(fetchMock: ReturnType<typeof serve>) {
  return fetchMock.mock.calls.filter(([u]) => u === "/api/live");
}

beforeEach(() => {
  try { window.localStorage.clear(); } catch { /* ignore */ }
});
afterEach(() => {
  vi.useRealTimers();
  window.location.hash = "";
});

describe("Live run", () => {
  it("fetches only its own API", async () => {
    const fetchMock = await open(NORMAL);
    const urls = fetchMock.mock.calls.map(([u]) => u);
    expect(new Set(urls)).toEqual(new Set(["/api/health", "/api/live"]));
  });

  it("shows the status sentence, the market and the three key limits", async () => {
    await open(NORMAL);
    const status = screen.getByRole("region", { name: "Status" });
    expect(status).toHaveTextContent("All normal · nothing needs you");
    expect(status).toHaveTextContent("Market open");
    const limits = [...status.querySelectorAll(".limit")];
    expect(limits.map((l) => l.getAttribute("data-key"))).toEqual(
      ["decisions", "money_at_risk", "open_positions"]);
    expect(limits[0]).toHaveTextContent("3 of 10");
    expect(limits[1]).toHaveTextContent("$15.91 of $150.00"); // as recorded
    expect(limits[2]).toHaveTextContent("1 of 1 · reached");
  });

  it("shows all limits behind the toggle", async () => {
    await open(NORMAL);
    const toggle = screen.getByRole("button", { name: /Show all 7 daily limits/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    const keys = [...document.querySelectorAll(".limit")].map((l) => l.getAttribute("data-key"));
    expect(keys).toEqual(["decisions", "money_at_risk", "open_positions", "tokens",
      "av_calls", "fmp_requests", "trades"]);
    const tokens = document.querySelector('.limit[data-key="tokens"]')!;
    expect(tokens).toHaveTextContent("214,000 of 600,000");
    expect(tokens).toHaveTextContent("12000 more reserved for calls in progress");
    fireEvent.click(screen.getByRole("button", { name: /Show fewer limits/ }));
    expect(document.querySelectorAll(".limit")).toHaveLength(3);
  });

  it("lists today's stocks with the deciding one first, linking each decision", async () => {
    await open(NORMAL);
    const list = screen.getByRole("region", { name: "Today's stocks" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector(".outcome")!.textContent)).toEqual(
      ["Deciding", "Skipped", "PASS", "BUY"]);
    expect(rows[0].querySelector("a")).toBeNull(); // no run yet: nothing to open
    expect(rows[3].querySelector("a")).toHaveAttribute("href", "#/decision/r-nvda");
    expect(rows[3]).toHaveTextContent("sent, fill not recorded");
    expect(rows[2]).toHaveTextContent("10:00");
  });

  it("shows the heartbeat's phase as plain text in the decision area", async () => {
    await open(NORMAL);
    const now = screen.getByRole("region", { name: "Now" });
    expect(now).toHaveTextContent("AMD");
    expect(now).toHaveTextContent("deciding AMD");
    expect(now).toHaveTextContent("Last heartbeat: Nov 23 10:11:48 ET");
  });

  it("shows the open position with no fill recorded and no unrealized P&L", async () => {
    await open(NORMAL);
    const pos = screen.getByRole("region", { name: "Open position" });
    expect(pos).toHaveTextContent("NVDA");
    expect(pos).toHaveTextContent("6 shares");
    expect(pos).toHaveTextContent("sent, fill not recorded");
    expect(pos).toHaveTextContent("Fill price not recorded");
    expect(pos).toHaveTextContent("Stop 243.77, target 262.46");
    expect(pos).toHaveTextContent("the most it can lose is $15.91");
    expect(pos).toHaveTextContent("Unrealized profit / loss: —");
  });

  it("keeps the sources to one line until asked, when all are OK", async () => {
    await open(NORMAL);
    const src = screen.getByRole("region", { name: "Data sources" });
    expect(src).toHaveTextContent("All 3 sources OK");
    expect(within(src).queryAllByRole("listitem")).toHaveLength(0);
    fireEvent.click(within(src).getByRole("button", { name: /Show details/ }));
    expect(within(src).getAllByRole("listitem")).toHaveLength(3);
  });

  it("expands the sources by itself on a failure, and shows the block", async () => {
    const down = calendarDown as unknown as Live;
    await open(down);
    const src = screen.getByRole("region", { name: "Data sources" });
    expect(within(src).getAllByRole("listitem")).toHaveLength(3);
    expect(within(src).queryByRole("button")).toBeNull();
    expect(src).toHaveTextContent("no valid calendar today, and all 5 calls are used");
    const status = screen.getByRole("region", { name: "Status" });
    expect(status).toHaveTextContent("New trades are blocked:");
    expect(status.querySelector(".status-sentence")).toHaveAttribute("data-kind", "blocked");
  });

  it("shows a stale heartbeat as blocked and the counted symbol as having no outcome",
    async () => {
      await open(stale as unknown as Live);
      expect(screen.getByRole("region", { name: "Status" }))
        .toHaveTextContent("run_day's heartbeat is 95 s old");
      const rows = within(screen.getByRole("region", { name: "Today's stocks" }))
        .getAllByRole("listitem");
      expect(rows[0]).toHaveTextContent("No outcome");
      expect(screen.getByRole("region", { name: "Last known step" })).toHaveTextContent("stale");
    });

  it("is calm when not running outside market hours", async () => {
    await open(notRunning as unknown as Live);
    expect(screen.getByRole("region", { name: "Status" }))
      .toHaveTextContent("Not running today · market closed");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("No stock has been looked at today.")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Open position" })).toHaveTextContent("None.");
  });

  it("never shows an approval card: the ticket needs #212", async () => {
    await open(NORMAL);
    expect(screen.queryByText(/Waiting for your approval/)).toBeNull();
  });

  it("shows every problem", async () => {
    const body = { ...NORMAL, problems: [{ level: "error", text: "journal line 8 is not JSON" },
      { level: "warn", text: "the cache folder cannot be listed (PermissionError)" }] };
    await open(body as Live);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("journal line 8 is not JSON");
    expect(alert).toHaveTextContent("the cache folder cannot be listed");
  });

  it("renders recorded text as plain text", async () => {
    const body = structuredClone(NORMAL) as Live;
    body.current = { ...body.current!, line: "<img src=x onerror=alert(1)> **x**" };
    body.today[0] = { ...body.today[0], why: "<b>bold</b>" };
    await open(body);
    expect(document.querySelector("main img")).toBeNull();
    expect(document.querySelector("main b")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)> **x**")).toBeInTheDocument();
  });

  it("the intro banner can be dismissed and stays dismissed", async () => {
    await open(NORMAL);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("region", { name: "What this is" })).toBeNull();
    expect(window.localStorage.getItem("live-intro-dismissed")).toBe("1");
  });
});

describe("polling", () => {
  it("polls every 2 s with the ETag, and keeps the page on a 304", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    const fetchMock = serve(NORMAL, "304");
    window.location.hash = "#/live";
    render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("All normal · nothing needs you")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    const calls = liveCalls(fetchMock);
    expect(calls).toHaveLength(2);
    const headers = (calls[1][1]?.headers ?? {}) as Record<string, string>;
    expect(headers["If-None-Match"]).toBe('"e1"');
    expect(screen.getByText("All normal · nothing needs you")).toBeInTheDocument();
  });

  it("updates when the answer changes", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    serve(NORMAL, stale as unknown as Live);
    window.location.hash = "#/live";
    render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByRole("region", { name: "Status" }))
      .toHaveTextContent("New trades are blocked");
  });

  it("a failed poll keeps the last answer under a warning; it is never hidden", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    serve(NORMAL, "down", 500);
    window.location.hash = "#/live";
    render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Can't reach the local UI server.");
    expect(alert).toHaveTextContent("may be out of date");
    expect(screen.getByText("All normal · nothing needs you")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByRole("alert")).toHaveTextContent("answered 500");
  });

  it("an answer of another shape is an error on screen, never a crash", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    serve(NORMAL, { hello: "world" } as unknown as Live);
    window.location.hash = "#/live";
    render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
    expect(screen.getByRole("alert")).toHaveTextContent("unexpected shape");
    expect(screen.getByText("All normal · nothing needs you")).toBeInTheDocument();
  });

  it("an error on the first load says so", async () => {
    serve("down");
    window.location.hash = "#/live";
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Nothing to show yet.");
  });

  it("stops polling when the page is left", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
    const fetchMock = serve(NORMAL);
    window.location.hash = "#/live";
    const { unmount } = render(<App />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    unmount();
    const before = liveCalls(fetchMock).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 3); });
    expect(liveCalls(fetchMock)).toHaveLength(before);
  });
});
