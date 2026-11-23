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
  seq: number; // position among the journal's parsed entries
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

// GET /api/decisions/{run_id} (ui/api/detail.py). Golden copies:
// ui/fixtures/api/decision-*.json. Record text in `details` is untrusted model text.
export type StepState =
  | "done" | "blocked" | "skipped" | "not_reached" | "failed" | "not_recorded"
  | "declined" // a human answer other than yes: neutral, not a refusal by the code
  | "unanswered"; // no answer before the plan expired: neutral

export interface Step {
  n: number;
  key: string;
  title: string;
  actor: Actor;
  state: StepState;
  line: string;
  details: Record<string, unknown> | null;
}

export interface AnalystView {
  agent: string; name: string; ok: boolean | null; // null: not in the record
  reason_code: string | null;
  stance: string | null; confidence: number | null;
  evidence: { point: string | null; source: string | null }[];
}

export interface PlanView {
  plan_id: string | null; setup: string | null; entry: string | null; stop: string | null;
  target: string | null; reward_to_risk: string | null; chosen: boolean;
}

export interface TurnView {
  round: number | null; side: "bull" | "bear" | null; ok: boolean; argument: string | null;
  points: (string | null)[]; fatal: boolean | null;
}

export interface OrderEvent {
  status: string; at: string | null; fields: Record<string, string | null>;
}

export interface DecisionDetail {
  run_id: string;
  symbol: string | null;
  at: string | null;
  day: string | null;
  outcome: Outcome | null;
  why: { code: string; text: string; detail: string | null } | null;
  order_status: { code: string; text: string } | null;
  in_short: string;
  numbers: {
    shares: number | null; entry: string | null; limit_price: string | null;
    stop: string | null; target: string | null; max_loss: string | null;
  } | null;
  steps: Step[];
  order_events: OrderEvent[];
  record_available: boolean;
  problems: Problem[];
}

// GET /api/live (ui/api/live.py). Golden copies: ui/fixtures/api/live-*.json.
export type LiveKind = "normal" | "order_waiting" | "blocked" | "idle" | "not_running";
export type LiveOutcome = Outcome | "DECIDING" | "NO_OUTCOME";

export interface LiveLimit {
  key: string;
  label: string;
  unit: "usd" | "count" | "tokens";
  used: string | null; // as recorded; null when not known
  limit: string | null;
  reached: boolean;
  pct: number | null; // the bar's width, from the recorded numbers (display only)
  note: string | null;
}

export interface LiveRow {
  symbol: string | null;
  outcome: LiveOutcome;
  why: string;
  order_status: { code: string; text: string } | null;
  run_id: string | null;
  at: string | null;
}

export interface LivePosition {
  symbol: string;
  run_id: string;
  client_order_id: string;
  approved_at: string;
  qty: number;
  filled_qty: number;
  avg_price: string | null;
  stop: string | null;
  target: string | null;
  max_loss: string;
  status: { code: string; text: string };
  unrealized: null; // no live quotes in the UI
}

export interface Live {
  as_of: string;
  et_date: string;
  market: { state: "open" | "closed" | "unknown"; text: string };
  status: { kind: LiveKind; sentence: string; reasons: { code: string; text: string }[] };
  key_limits: LiveLimit[];
  caps: LiveLimit[];
  ticket: null; // the approval card needs #212
  today: LiveRow[];
  current: null | {
    symbol: string | null; step: null; line: string; analysts: null; live: boolean;
  };
  positions: LivePosition[] | null; // null: the order history does not replay
  sources: {
    ok: boolean; summary: string; expanded: boolean;
    items: { name: string; ok: boolean | null; detail: string }[];
  };
  run_day: {
    heartbeat_at: string | null; age_s: number | null; stale: boolean; future: boolean;
    phase: string | null; pid: number | null; started_at: string | null;
    last_scan_at: string | null; last_end: { reason: string | null; at: string | null } | null;
  };
  problems: Problem[];
}
