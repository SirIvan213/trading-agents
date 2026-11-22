// The Decision detail page rendered from the golden API responses
// (ui/fixtures/api/decision-*.json), which ui/tests/test_detail.py checks against the real
// endpoint over golden decision records from the real decision path.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import buy from "../../../fixtures/api/decision-buy-approved.json";
import missing from "../../../fixtures/api/decision-missing-record.json";
import failed from "../../../fixtures/api/decision-pass-analyst-failed.json";
import fatal from "../../../fixtures/api/decision-pass-fatal-bear.json";
import skipped from "../../../fixtures/api/decision-skipped.json";
import health from "../../../fixtures/api/health.json";
import { App } from "../App";
import type { DecisionDetail } from "../types";

afterEach(() => {
  window.location.hash = "";
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
}

function serve(body: unknown, status = 200) {
  const fetchMock = vi.fn(async (url: string) =>
    url === "/api/health" ? json(health) : json(body, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function open(body: unknown, runId = "RUN_ID", status = 200) {
  const fetchMock = serve(body, status);
  window.location.hash = `#/decision/${runId}`;
  render(<App />);
  return fetchMock;
}

function step(key: string): HTMLElement {
  return document.getElementById(`step-${key}`)!;
}

describe("Decision detail", () => {
  it("fetches only its own API, for this decision", async () => {
    const fetchMock = await open(buy);
    await screen.findByText("In short");
    const urls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(urls).toContain("/api/decisions/RUN_ID");
    expect(urls.every((u) => u.startsWith("/api/"))).toBe(true);
  });

  it("shows In short and the 4 key numbers exactly as recorded", async () => {
    await open(buy);
    expect(await screen.findByText(buy.in_short)).toBeInTheDocument();
    const nums = within(screen.getByText("Shares").closest("dl")!);
    expect(nums.getByText("13")).toBeInTheDocument();
    expect(nums.getByText("103.10")).toBeInTheDocument();
    expect(nums.getByText("106.59")).toBeInTheDocument();
    expect(nums.getByText("$46.80")).toBeInTheDocument();
  });

  it("shows the 8 steps in code order, each with its state and one line", async () => {
    await open(buy);
    await screen.findByText("In short");
    const ids = [...document.querySelectorAll("section[id^=step-]")].map((s) => s.id);
    expect(ids).toEqual(["step-scan", "step-event_check", "step-analysts", "step-plans",
      "step-debate", "step-trader", "step-order_ticket", "step-approve_and_log"]);
    for (const s of (buy as DecisionDetail).steps) {
      expect(step(s.key)).toHaveTextContent(s.line);
    }
    expect(step("event_check")).toHaveTextContent("8-K item 2.02");
  });

  it("links back to History and jumps to each step", async () => {
    await open(buy);
    await screen.findByText("In short");
    expect(screen.getByRole("link", { name: "← History" })).toHaveAttribute("href", "#/history");
    const jumps = within(screen.getByRole("navigation", { name: "Jump to step" }));
    expect(jumps.getAllByRole("button")).toHaveLength(8);
  });

  it("puts analyst evidence, the full debate and the earnings and halt checks behind toggles", async () => {
    await open(buy);
    await screen.findByText("In short");
    expect(screen.queryByText("breakout above the opening range")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Show each analyst's evidence/ }));
    expect(screen.getByText("breakout above the opening range")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Read the full debate/ }));
    expect(within(step("debate")).getByText(/BEAR · round 1/)).toBeInTheDocument();

    expect(within(step("order_ticket")).queryByText(/no earnings through/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Show the earnings and halt checks/ }));
    expect(within(step("order_ticket")).getByText(/no earnings through 2026-11-10/)).toBeInTheDocument();
    expect(within(step("order_ticket")).getByText("103.21")).toBeInTheDocument();
  });

  it("shows the other plans behind a toggle", async () => {
    const body = structuredClone(buy) as DecisionDetail;
    const plans = body.steps[3].details!.plans as { plan_id: string; chosen: boolean }[];
    plans.push({ ...plans[0], plan_id: "plan-2", chosen: false });
    await open(body);
    await screen.findByText("In short");
    expect(within(step("plans")).queryByText(/plan-2/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /1 other plan considered/ }));
    expect(within(step("plans")).getByText(/plan-2/)).toBeInTheDocument();
  });

  it("a fatal Bear PASS: blocked debate and Trader, no numbers, ticket not reached", async () => {
    await open(fatal);
    await screen.findByText(fatal.in_short);
    expect(within(step("debate")).getByText("blocked")).toBeInTheDocument();
    expect(within(step("trader")).getByText("blocked")).toBeInTheDocument();
    expect(within(step("order_ticket")).getByText("not reached")).toBeInTheDocument();
    expect(screen.getByText("No order was sized, so there are no numbers.")).toBeInTheDocument();
  });

  it("an analyst failure shows which analyst failed, by its code", async () => {
    await open(failed);
    await screen.findByText("In short");
    expect(within(step("analysts")).getByText("failed (invalid_output)")).toBeInTheDocument();
    expect(within(step("debate")).getByText("skipped")).toBeInTheDocument();
  });

  it("a skip stops at the event check", async () => {
    await open(skipped);
    await screen.findByText("In short");
    expect(within(step("event_check")).getByText("blocked")).toBeInTheDocument();
    for (const key of ["analysts", "plans", "debate", "trader", "order_ticket", "approve_and_log"]) {
      expect(within(step(key)).getByText("not reached")).toBeInTheDocument();
    }
  });

  it("a missing record is a visible problem and the record steps say not recorded", async () => {
    await open(missing);
    expect(await screen.findByRole("alert")).toHaveTextContent("no decision record");
    for (const key of ["analysts", "plans", "debate"]) {
      expect(within(step(key)).getByText("not recorded")).toBeInTheDocument();
      expect(step(key)).toHaveTextContent("Not recorded for this decision.");
    }
    expect(within(step("order_ticket")).getByText("done")).toBeInTheDocument();
    // the limit price is never computed: a dash when it isn't recorded
    expect(step("order_ticket")).toHaveTextContent("limit of —");
  });

  it("an unknown decision says so", async () => {
    await open({ detail: "no such decision" }, "NOPE", 404);
    expect(await screen.findByRole("alert")).toHaveTextContent("No such decision.");
  });

  it("a run_id the API refuses (422) reads as no such decision", async () => {
    await open({ detail: "bad run_id" }, "RUN_ID", 422);
    expect(await screen.findByRole("alert")).toHaveTextContent("No such decision.");
  });

  it("a decision without a readable time shows a dash, not a dangling ET", async () => {
    const body = structuredClone(buy) as DecisionDetail;
    body.at = null;
    await open(body);
    await screen.findByText("In short");
    expect(document.querySelector(".detail-when")!.textContent).toBe("—");
  });

  it("a server error says so", async () => {
    await open({ detail: "x" }, "RUN_ID", 500);
    expect(await screen.findByRole("alert")).toHaveTextContent("answered 500");
  });

  it("renders record text as plain text, never as HTML or Markdown", async () => {
    const body = structuredClone(buy) as DecisionDetail;
    const trader = body.steps[5].details!;
    trader.reasoning = "<img src=x onerror=alert(1)> **bold** [link](/x)";
    const analysts = body.steps[2].details!.analysts as { evidence: { point: string }[] }[];
    analysts[0].evidence[0].point = "<script>alert(1)</script>";
    await open(body);
    await screen.findByText("In short");
    fireEvent.click(screen.getByRole("button", { name: /Show each analyst's evidence/ }));
    expect(document.querySelector("main img, main script, main strong.md")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)> **bold** [link](/x)")).toBeInTheDocument();
    expect(screen.getByText("<script>alert(1)</script>", { exact: false })).toBeInTheDocument();
  });
});
