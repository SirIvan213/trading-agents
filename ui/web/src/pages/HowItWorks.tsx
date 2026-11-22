// How it works (UI-2, #215): what the project is, the 8 steps of a decision, the safety
// limits read from the config, the screens, the glossary and the phase progress.
// Every string from the API is rendered as plain text (React escapes it); never as HTML.
import { useEffect, useState } from "react";

import { getJson } from "../api";
import { MISSING, showCount, showLimit } from "../format";
import type { Actor, Cap, Explainer, Limit, PhaseState } from "../types";

const ACTOR_LABEL: Record<Actor, string> = { code: "Code", ai: "AI", you: "You" };
const PHASE_LABEL: Record<PhaseState, string> = {
  done: "done", in_progress: "in progress", next: "next",
};
// The rail of six large cards, as in the sketch: five limits and the decision cap.
const RAIL_LIMITS = ["max_loss_per_trade", "daily_loss_limit", "max_trades_per_day",
  "max_open_positions", "flat_by"];

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: Explainer };

export function HowItWorks() {
  const [state, setState] = useState<State>({ status: "loading" });
  useEffect(() => {
    let live = true;
    getJson<Explainer>("/api/explainer")
      .then((data) => live && setState({ status: "ready", data }))
      .catch((e: unknown) => live && setState({
        status: "error", message: e instanceof Error ? e.message : String(e),
      }));
    return () => { live = false; };
  }, []);

  if (state.status === "loading") {
    return <main className="page"><p className="muted">Loading…</p></main>;
  }
  if (state.status === "error") {
    return (
      <main className="page">
        <div role="alert" className="problems">
          <strong>Can't load this page.</strong> {state.message}
        </div>
      </main>
    );
  }
  return <Page data={state.data} />;
}

function Page({ data }: { data: Explainer }) {
  const byKey = new Map(data.limits.map((l) => [l.key, l]));
  const rail = RAIL_LIMITS.map((k) => byKey.get(k)).filter((l): l is Limit => l !== undefined);
  const otherLimits = data.limits.filter((l) => !RAIL_LIMITS.includes(l.key));
  const decisions = data.caps.find((c) => c.key === "max_decisions");
  const otherCaps = data.caps.filter((c) => c.key !== "max_decisions");

  return (
    <main className="page about">
      {data.problems.length > 0 && (
        <div role="alert" className="problems">
          <strong>Problems with the config</strong>
          <ul>{data.problems.map((p, i) => <li key={i}>{p.text}</li>)}</ul>
        </div>
      )}

      <section aria-labelledby="hero-h" className="hero">
        <h1 id="hero-h">{data.hero.title}</h1>
        <p className="lead">{data.hero.text}</p>
        <div className="hero-actions">
          <a className="button primary" href="#/live">Open the live run</a>
          <button type="button" className="button"
            onClick={() => document.getElementById("steps")?.scrollIntoView()}>
            See how a decision works
          </button>
        </div>
      </section>

      <section aria-label="Key facts" className="cards facts">
        {data.facts.map((f) => (
          <div key={f.title} className="card">
            <span className="card-title">{f.title}</span>
            <span className="muted small">{f.text}</span>
          </div>
        ))}
      </section>

      <section id="steps" aria-labelledby="steps-h" className="stack">
        <div className="section-head">
          <h2 id="steps-h">How one decision works</h2>
          <span className="muted small">The same 8 steps are numbered on every decision page.</span>
        </div>
        <div className="legend">
          <span className="chip chip-code">Code: rules, no AI</span>
          <span className="chip chip-ai">AI: reads and argues</span>
          <span className="chip chip-you">You: approve</span>
        </div>
        <ol className="steps">
          {data.steps.map((s) => (
            <li key={s.key} className="card step">
              <div className="step-head">
                <span className="step-n" aria-hidden="true">{s.n}</span>
                <span className="card-title">{s.title}</span>
                <span className={`chip chip-${s.actor} step-actor`}>{ACTOR_LABEL[s.actor]}</span>
              </div>
              <span className="text2 small">{s.text}</span>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="rails-h" className="stack">
        <h2 id="rails-h">Safety limits</h2>
        <p className="muted narrow">
          Code enforces these limits on every order. They can't be loosened to let a trade
          through, and the AI can't change them.
        </p>
        <div className="cards rails">
          {rail.map((l) => (
            <div key={l.key} className="card rail">
              <span className="rail-value mono">{showLimit(l)}</span>
              <span className="muted small">{l.label}</span>
            </div>
          ))}
          {decisions && <CapCard cap={decisions} />}
        </div>
        <dl className="minor">
          {otherLimits.map((l) => (
            <div key={l.key}><dt className="mono">{showLimit(l)}</dt><dd>{l.label}</dd></div>
          ))}
          {otherCaps.map((c) => (
            <div key={c.key}>
              <dt className="mono">{showCount(c.value)}</dt>
              <dd>{c.label}{capCeiling(c)}</dd>
            </div>
          ))}
        </dl>
        <p className="faint small">
          Limits from <code>config/{data.limits_source.source ?? MISSING}</code>; daily caps
          from <code>config/{data.caps_source.source ?? "run_day.json"}</code>, never above
          their hard ceilings.
        </p>
      </section>

      <section aria-labelledby="screens-h" className="stack">
        <h2 id="screens-h">The screens</h2>
        <div className="cards screens">
          {data.screens.map((p) => (
            <a key={p.key} href={p.route} className={p.built ? "card screen" : "card screen planned"}>
              <span className="card-title screen-title">
                {p.title} <span className="accent" aria-hidden="true">→</span>
              </span>
              <span className="muted small">{p.text}</span>
              {!p.built && <span className="faint small">Planned, not built yet.</span>}
            </a>
          ))}
        </div>
        <p className="faint small">
          Anything with a <span className="planned-sample">dashed outline</span> is planned but
          not built yet.
        </p>
      </section>

      <section aria-labelledby="gl-h" className="stack">
        <h2 id="gl-h">Words you'll see</h2>
        <dl className="glossary">
          {data.glossary.map((g) => (
            <div key={g.term}>
              <dt>{g.term}</dt>
              <dd className="muted small">{g.text}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section aria-labelledby="st-h" className="card phases-box">
        <h2 id="st-h" className="h3">Where the project is</h2>
        <ol className="phases">
          {data.phases.map((ph) => (
            <li key={ph.n} className={`phase phase-${ph.state}`}>
              <span className="muted tiny">Phase {ph.n} · {PHASE_LABEL[ph.state]}</span>
              <span className="phase-title">{ph.title}</span>
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}

function capCeiling(c: Cap): string {
  return c.value !== null && c.value < c.ceiling ? ` (hard ceiling ${showCount(c.ceiling)})` : "";
}

function CapCard({ cap }: { cap: Cap }) {
  return (
    <div className="card rail">
      <span className="rail-value mono">{showCount(cap.value)}</span>
      <span className="muted small">{cap.label}{capCeiling(cap)}</span>
    </div>
  );
}
