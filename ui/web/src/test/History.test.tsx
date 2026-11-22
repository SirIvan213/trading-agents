// The History page rendered from the golden API response (ui/fixtures/api/decisions.json),
// which ui/tests/test_history.py checks against the real endpoint over a journal holding
// every outcome kind.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import golden from "../../../fixtures/api/decisions.json";
import health from "../../../fixtures/api/health.json";
import { App } from "../App";
import type { Decisions, OutcomeFilter } from "../types";

const DECISIONS = golden as Decisions;
const OUTCOME: Record<string, string | null> = {
  all: null, buy: "BUY", pass: "PASS", skipped: "SKIPPED", failed: "FAILED",
};

afterEach(() => {
  window.location.hash = "";
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
}

/** Serves /api/decisions like the server: the outcome filter, then a page of `limit`. */
function serve(base: Decisions = DECISIONS, opts: { failStatus?: number } = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/health") return json(health);
    if (opts.failStatus) return json({ detail: "x" }, opts.failStatus);
    const q = new URL(url, "http://127.0.0.1").searchParams;
    const wanted = OUTCOME[(q.get("outcome") ?? "all") as OutcomeFilter];
    const all = base.items.filter((i) => wanted === null || i.outcome === wanted);
    const cursor = Number(q.get("cursor") ?? "0");
    const limit = Number(q.get("limit") ?? "50");
    const page = all.slice(cursor, cursor + limit);
    return json({
      ...base, items: page, total: all.length,
      next_cursor: cursor + limit < all.length ? String(cursor + limit) : null,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function open(base?: Decisions, opts?: { failStatus?: number }) {
  const fetchMock = serve(base, opts);
  window.location.hash = "#/history";
  render(<App />);
  return fetchMock;
}

function rows(): HTMLTableRowElement[] {
  return [...document.querySelectorAll<HTMLTableRowElement>("table.decisions tbody tr")];
}

describe("History", () => {
  it("fetches only its own API", async () => {
    const fetchMock = await open();
    await screen.findByRole("table");
    const urls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(urls.every((u) => u.startsWith("/api/"))).toBe(true);
    expect(urls).toContain("/api/decisions?outcome=all&limit=50");
  });

  it("shows the 4 tiles from the recorded totals", async () => {
    await open();
    const tiles = within(await screen.findByRole("region", { name: "Totals" }));
    expect(tiles.getByText("Stocks the AI reviewed").parentElement).toHaveTextContent("16");
    expect(tiles.getByText("Trades proposed (BUY)").parentElement).toHaveTextContent("12");
    expect(tiles.getByText("Passed").parentElement).toHaveTextContent("4");
    expect(tiles.getByText("Skipped early").parentElement).toHaveTextContent("1");
  });

  it("lists every decision, newest first, each linked to its decision page", async () => {
    await open();
    await screen.findByRole("table");
    expect(rows()).toHaveLength(22);
    const first = rows()[0];
    expect(first).toHaveTextContent("NFLX");
    expect(within(first).getByRole("link", { name: "NFLX" }))
      .toHaveAttribute("href", "#/decision/r-lost");
    expect(rows()[21]).toHaveTextContent("BAC");
  });

  it("shows failed and unknown decisions, never hiding them", async () => {
    await open();
    await screen.findByRole("table");
    const failed = rows().filter((r) => r.textContent!.includes("Failed"));
    expect(failed.map((r) => r.querySelector("a")!.textContent).sort())
      .toEqual(["CVX", "DIS", "HD", "NFLX", "XOM"]);
    const odd = rows().find((r) => r.textContent!.includes("DIS"))!;
    expect(odd).toHaveTextContent("mystery_status"); // an unknown code is shown as itself
  });

  it("shows why from the recorded code, with the recorded detail", async () => {
    await open();
    await screen.findByRole("table");
    const nvda = rows().find((r) => r.textContent!.includes("NVDA"))!;
    expect(nvda).toHaveTextContent("The risk check blocked the order");
    expect(nvda).toHaveTextContent("event gate: EVENT (earnings 2026-11-24 before the open)");
    const vntr = rows().find((r) => r.textContent!.includes("VNTR"))!;
    expect(vntr).toHaveTextContent("PASS · fatal");
  });

  it("shows money as recorded and a dash until a trade closes", async () => {
    await open();
    await screen.findByRole("table");
    const tsla = rows().find((r) => r.textContent!.includes("TSLA"))!;
    expect(tsla).toHaveTextContent("-$31.65");
    expect(tsla).toHaveTextContent("closed");
    const jpm = rows().find((r) => r.textContent!.includes("JPM"))!;
    expect(jpm).toHaveTextContent("$45.20");
    const amzn = rows().find((r) => r.textContent!.includes("AMZN"))!;
    expect(amzn.lastElementChild).toHaveTextContent("—");
    expect(screen.getByText("Realized profit / loss:", { exact: false })).toHaveTextContent("$13.55");
  });

  it("says the total is a dash until a trade closes", async () => {
    const body = structuredClone(DECISIONS);
    body.tiles.realized_pnl = null;
    await open(body);
    await screen.findByRole("table");
    expect(screen.getByText("Realized profit / loss:", { exact: false }))
      .toHaveTextContent("— until a trade closes");
  });

  it("filters by outcome", async () => {
    await open();
    await screen.findByRole("table");
    const group = within(screen.getByRole("group", { name: "Filter by outcome" }));
    for (const [label, n] of [["BUY", 12], ["PASS", 4], ["Skipped", 1], ["Failed", 5], ["All", 22]] as const) {
      fireEvent.click(group.getByRole("button", { name: new RegExp(`^${label}`) }));
      await screen.findByRole("table");
      await vi.waitFor(() => expect(rows()).toHaveLength(n));
      expect(group.getByRole("button", { name: new RegExp(`^${label}`) }))
        .toHaveAttribute("aria-pressed", "true");
    }
  });

  it("hides the extra columns until asked, and shows not reached for the event check", async () => {
    await open();
    await screen.findByRole("table");
    expect(screen.queryByRole("columnheader", { name: "Event check" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Show more columns/ }));
    expect(screen.getByRole("columnheader", { name: "Event check" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Run id" })).toBeInTheDocument();
    const sap = rows().find((r) => r.textContent!.includes("SAP"))!;
    expect(sap).toHaveTextContent("not reached");
    expect(sap).not.toHaveTextContent("CLEAR");
    const nvda = rows().find((r) => r.textContent!.includes("NVDA"))!;
    expect(nvda).toHaveTextContent("EVENT");
    expect(nvda).toHaveTextContent("p-risk");
  });

  it("loads more rows with the cursor", async () => {
    const big = structuredClone(DECISIONS);
    big.items = Array.from({ length: 60 }, (_, n) => ({
      ...DECISIONS.items[0], run_id: `r-${n}`, symbol: `S${n}`,
    }));
    await open(big);
    await screen.findByRole("table");
    expect(rows()).toHaveLength(50);
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await vi.waitFor(() => expect(rows()).toHaveLength(60));
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("shows journal problems", async () => {
    const body = structuredClone(DECISIONS);
    body.problems = [
      { level: "error", text: "journal line 31 is not JSON: day totals cannot be trusted" },
      { level: "warn", text: "the journal's last line is incomplete" },
    ];
    await open(body);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("journal line 31 is not JSON");
    expect(alert).toHaveTextContent("last line is incomplete");
    expect(rows()).toHaveLength(22); // the rows read before it still show
  });

  it("keeps the table on screen, marked busy, while a filter loads", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => { release = r; });
    const base = serve();
    vi.stubGlobal("fetch", vi.fn(async (u: string) => {
      if (u.includes("outcome=buy")) await gate;
      return base(u);
    }));
    window.location.hash = "#/history";
    render(<App />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: /^BUY/ }));
    const section = screen.getByRole("region", { name: "Decisions" });
    await vi.waitFor(() => expect(section).toHaveAttribute("aria-busy", "true"));
    expect(rows()).toHaveLength(22); // the old rows stay until the new ones arrive
    release();
    await vi.waitFor(() => expect(rows()).toHaveLength(12));
    expect(section).toHaveAttribute("aria-busy", "false");
  });

  it("drops a Show more page that returns after the filter changed", async () => {
    const big = structuredClone(DECISIONS);
    big.items = Array.from({ length: 60 }, (_, n) => ({
      ...DECISIONS.items[0], run_id: `r-${n}`, symbol: `S${n}`, outcome: "PASS" as const,
    }));
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => { release = r; });
    const base = serve(big);
    vi.stubGlobal("fetch", vi.fn(async (u: string) => {
      if (u.includes("cursor=50")) await gate;
      return base(u);
    }));
    window.location.hash = "#/history";
    render(<App />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("button", { name: /^BUY/ }));
    await vi.waitFor(() => expect(screen.getByText("No decisions here yet.")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /^All/ })); // and back again
    await vi.waitFor(() => expect(rows()).toHaveLength(50));
    release();
    await new Promise((r) => setTimeout(r, 50));
    expect(rows()).toHaveLength(50); // the stale page was dropped, not appended
  });

  it("never shows a row twice when a page repeats one", async () => {
    const big = structuredClone(DECISIONS);
    big.items = Array.from({ length: 60 }, (_, n) => ({
      ...DECISIONS.items[0], run_id: `r-${n}`, symbol: `S${n}`,
    }));
    const base = serve(big);
    vi.stubGlobal("fetch", vi.fn(async (u: string) => {
      if (u.includes("cursor=50")) return base(u.replace("cursor=50", "cursor=49"));
      return base(u);
    }));
    window.location.hash = "#/history";
    render(<App />);
    await screen.findByRole("table");
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    await vi.waitFor(() => expect(rows()).toHaveLength(60));
  });

  it("says so when the API fails", async () => {
    await open(undefined, { failStatus: 500 });
    expect(await screen.findByRole("alert")).toHaveTextContent("answered 500");
  });

  it("renders recorded text as plain text", async () => {
    const body = structuredClone(DECISIONS);
    body.items = [{
      ...DECISIONS.items[0], symbol: "<img src=x onerror=alert(1)>",
      why: { code: "x", text: "<b>bold</b>", detail: "**md**" },
    }];
    await open(body);
    await screen.findByRole("table");
    expect(document.querySelector("table img")).toBeNull();
    expect(document.querySelector("table b")).toBeNull();
    expect(screen.getByText("<b>bold</b>")).toBeInTheDocument();
  });
});
