// The response of GET /api/explainer (ui/api/explainer.py). Golden copy:
// ui/fixtures/api/explainer.json, checked against the real endpoint by ui/tests.

export type Actor = "code" | "ai" | "you";
export type PhaseState = "done" | "in_progress" | "next";

export interface Problem {
  level: "error" | "warn";
  text: string;
}

export interface Source {
  source: string | null;
  ok: boolean;
  error: string | null;
}

export interface Limit {
  key: string;
  label: string;
  unit: "usd" | "count" | "time" | "seconds";
  value: string | null; // as recorded by tools.config ("50.00", "3", "15:55"); null if unknown
}

export interface Cap {
  key: string;
  label: string;
  value: number | null; // from config/run_day.json; null when run_day would refuse it
  ceiling: number; // the hard ceiling (CLAUDE.md rule 6)
}

export interface Explainer {
  hero: { title: string; text: string };
  facts: { title: string; text: string }[];
  steps: { n: number; key: string; title: string; actor: Actor; text: string }[];
  screens: { key: string; title: string; route: string; built: boolean; text: string }[];
  glossary: { term: string; text: string }[];
  phases: { n: number; title: string; state: PhaseState }[];
  limits: Limit[];
  limits_source: Source;
  paper: boolean | null;
  trading_enabled: boolean | null;
  caps: Cap[];
  caps_source: Source;
  problems: Problem[];
}

// GET /api/decisions (ui/api/history.py). Golden copy: ui/fixtures/api/decisions.json.
export type Outcome = "BUY" | "PASS" | "SKIPPED" | "FAILED";
export type OutcomeFilter = "all" | "buy" | "pass" | "skipped" | "failed";

export interface DecisionRow {
  run_id: string;
  line: number;
  at: string | null; // New York ISO time as recorded; null when it can't be read
  day: string | null;
  symbol: string | null;
  outcome: Outcome;
  why: { code: string; text: string; detail: string | null };
  forced_pass: string | null;
  order_status: { code: string; text: string } | null;
  qty: number | null;
  entry: string | null; // money as recorded
  max_loss: string | null;
  realized_pnl: string | null;
  plan_id: string | null;
  decided_by: string | null;
  gate: { status: string; reason: string | null };
  notes: string[];
}

export interface Decisions {
  items: DecisionRow[];
  tiles: {
    reviewed: number; buys: number; passed: number; skipped: number; failed: number;
    realized_pnl: string | null; // Decimal sum of recorded realized_pnl; null until a close
  };
  counts: Record<OutcomeFilter, number>;
  total: number;
  next_cursor: string | null;
  problems: Problem[];
}
