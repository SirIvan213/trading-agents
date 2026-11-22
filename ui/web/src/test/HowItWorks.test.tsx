// The How it works page and the shared header, rendered from the golden API responses
// (ui/fixtures/api/explainer.json and health.json), which ui/tests/test_explainer.py checks
// against the real endpoints.
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import golden from "../../../fixtures/api/explainer.json";
import health from "../../../fixtures/api/health.json";
import { App } from "../App";
import type { Explainer } from "../types";

afterEach(() => {
  window.location.hash = "";
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
}

/** Serve the explainer (`body`, `status`) and the health response by URL. */
function serve(body: unknown, status = 200, healthBody: unknown = health) {
  const fetchMock = vi.fn(async (url: string) =>
    url === "/api/health" ? json(healthBody) : json(body, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function urls(fetchMock: ReturnType<typeof serve>): string[] {
  return fetchMock.mock.calls.map((c) => c[0]).sort();
}

function copy(): Explainer {
  return structuredClone(golden) as Explainer;
}

async function openAbout(body: unknown = golden) {
  const fetchMock = serve(body);
  window.location.hash = "#/";
  render(<App />);
  await screen.findByRole("heading", { level: 1 });
  return fetchMock;
}

describe("How it works", () => {
  it("fetches only its own API", async () => {
    const fetchMock = await openAbout();
    expect(urls(fetchMock)).toEqual(["/api/explainer", "/api/health"]);
  });

  it("shows the hero, the four facts and the header", async () => {
    await openAbout();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(golden.hero.title);
    for (const f of golden.facts) expect(screen.getByText(f.title)).toBeInTheDocument();
    expect(await screen.findByText("PAPER · practice money")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "How it works" }))
      .toHaveAttribute("aria-current", "page");
  });

  it("shows the 8 numbered steps with their Code / AI / You tags", async () => {
    await openAbout();
    const items = document.querySelectorAll("ol.steps > li");
    expect(items).toHaveLength(8);
    const tags = [...items].map((li) => li.querySelector(".step-actor")!.textContent);
    expect(tags).toEqual(["Code", "Code", "AI", "Code", "AI", "AI", "Code", "You"]);
    expect(items[7]).toHaveTextContent("Approve and log");
  });

  it("shows the safety limits as recorded, with their source", async () => {
    await openAbout();
    expect(screen.getByText("$50.00")).toBeInTheDocument();
    expect(screen.getByText("$150.00")).toBeInTheDocument();
    expect(screen.getByText("3:55 PM ET")).toBeInTheDocument();
    expect(screen.getByText("600,000")).toBeInTheDocument();
    const rails = document.querySelectorAll(".rails .rail");
    expect(rails).toHaveLength(6);
    expect(rails[5]).toHaveTextContent("10AI decisions per day, at most");
    expect(screen.getByText("config/limits.json")).toBeInTheDocument();
  });

  it("marks screens that are not built yet", async () => {
    await openAbout();
    const planned = document.querySelectorAll(".screen.planned");
    expect(planned).toHaveLength(golden.screens.filter((s) => !s.built).length);
  });

  it("shows the whole glossary and the phase progress", async () => {
    await openAbout();
    expect(document.querySelectorAll(".glossary dt")).toHaveLength(golden.glossary.length);
    const phases = document.querySelectorAll(".phases li");
    expect(phases).toHaveLength(6);
    expect(phases[4]).toHaveTextContent("Phase 5 · in progress");
    expect(phases[4]).toHaveClass("phase-in_progress");
  });

  it("shows config problems and a dash for every unknown value, never a default", async () => {
    const body = copy();
    body.limits = body.limits.map((l) => ({ ...l, value: null }));
    body.caps = body.caps.map((c) => ({ ...c, value: null }));
    body.problems = [{ level: "error", text: "limits config is invalid" }];
    await openAbout(body);
    expect(screen.getByRole("alert")).toHaveTextContent("limits config is invalid");
    expect(screen.queryByText("$50.00")).not.toBeInTheDocument();
    const rails = [...document.querySelectorAll(".rails .rail-value")].map((e) => e.textContent);
    expect(rails).toEqual(["—", "—", "—", "—", "—", "—"]);
  });

  it("says so when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    window.location.hash = "#/";
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("did not answer");
  });

  it("says so when the API answers with an error", async () => {
    serve({ detail: "x" }, 500);
    window.location.hash = "#/";
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("answered 500");
  });

  it("renders API text as plain text, never as HTML", async () => {
    const body = copy();
    body.glossary = [{ term: "<img src=x onerror=alert(1)>", text: "<b>bold</b> **md**" }];
    body.hero = { title: "<script>alert(1)</script>", text: "[link](/x)" };
    await openAbout(body);
    expect(document.querySelector("main img")).toBeNull();
    expect(document.querySelector("main script")).toBeNull();
    expect(document.querySelector("main b")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(screen.getByText("<b>bold</b> **md**")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 }))
      .toHaveTextContent("<script>alert(1)</script>");
  });
});

describe("pages not built yet", () => {
  it.each([
    ["#/live", "Live run", "UI-5"],
    ["#/history", "History", "UI-3"],
    ["#/decision/run-1", "Decision detail", "UI-4"],
  ])("%s says it is coming", (hash, title, pr) => {
    const fetchMock = serve(golden);
    window.location.hash = hash;
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(title);
    expect(screen.getByText(new RegExp(`coming in ${pr}`))).toBeInTheDocument();
    expect(urls(fetchMock)).toEqual(["/api/health"]); // only the header's mode
  });

  it("an unknown page is not found", () => {
    serve(golden);
    window.location.hash = "#/nope";
    render(<App />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Page not found");
  });
});

describe("the header's mode badges", () => {
  async function header(healthBody: unknown) {
    serve(golden, 200, healthBody);
    window.location.hash = "#/live";
    render(<App />);
  }

  it("says PAPER only when the config says paper: true", async () => {
    await header(health);
    expect(await screen.findByText("PAPER · practice money")).toBeInTheDocument();
    expect(screen.queryByText(/Kill switch/)).not.toBeInTheDocument();
  });

  it("warns when the config says paper: false", async () => {
    await header({ ...health, paper: false });
    expect(await screen.findByText(/NOT PAPER/)).toBeInTheDocument();
    expect(screen.queryByText("PAPER · practice money")).not.toBeInTheDocument();
  });

  it("warns when the limits can't be read", async () => {
    await header({ ...health, paper: null, trading_enabled: null });
    expect(await screen.findByText(/Mode unknown: the limits config/)).toBeInTheDocument();
    expect(screen.queryByText("PAPER · practice money")).not.toBeInTheDocument();
  });

  it("warns when the server does not answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    window.location.hash = "#/live";
    render(<App />);
    expect(await screen.findByText(/Mode unknown: the server/)).toBeInTheDocument();
  });

  it("shows the kill switch", async () => {
    await header({ ...health, trading_enabled: false });
    expect(await screen.findByText("Kill switch on: no new orders")).toBeInTheDocument();
  });
});
