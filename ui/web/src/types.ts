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
