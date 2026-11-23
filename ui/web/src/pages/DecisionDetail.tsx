// Decision detail (UI-4, #217): one decision from start to finish. "In short" and the key
// numbers come first; then the 8 steps in the code's order, one line each, with the details
// one click away. Record text (analyst evidence, debate turns, the Trader's reasoning) is
// untrusted model text: rendered as plain text only, already length-capped by the API, and
// never parsed for numbers. Numbers are the recorded strings; the limit price is never computed.
import { useEffect, useState } from "react";

import { ApiError, getJson } from "../api";
import { MISSING, showMoney, showWhen } from "../format";
import type {
  AnalystView, DecisionDetail as Detail, OrderEvent, PlanView, Step, StepState, TurnView,
} from "../types";

const STATE_LABEL: Record<StepState, string> = {
  done: "done", blocked: "blocked", skipped: "skipped", not_reached: "not reached",
  failed: "failed", not_recorded: "not recorded",
};
const OUTCOME_LABEL = { BUY: "BUY", PASS: "PASS", SKIPPED: "Skipped", FAILED: "Failed" } as const;

type State =
  | { status: "loading" }
  | { status: "missing" }
  | { status: "error"; message: string }
  | { status: "ready"; data: Detail };

export function DecisionDetail({ runId }: { runId: string }) {
  const [state, setState] = useState<State>({ status: "loading" });
  useEffect(() => {
    let live = true;
    setState({ status: "loading" });
    getJson<Detail>(`/api/decisions/${encodeURIComponent(runId)}`)
      .then((data) => live && setState({ status: "ready", data }))
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof ApiError && (e.status === 404 || e.status === 422)) {
          setState({ status: "missing" });
        }
        else setState({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
    return () => { live = false; };
  }, [runId]);

  return (
    <main className="page detail">
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <a href="#/history">← History</a>
        <span aria-hidden="true">/</span>
        <span>{state.status === "ready" ? `${state.data.symbol ?? MISSING} decision` : "Decision"}</span>
      </nav>
      {state.status === "loading" && <p className="muted">Loading…</p>}
      {state.status === "missing" && (
        <div role="alert" className="problems">
          <strong>No such decision.</strong> Neither the journal nor a decision record knows
          {" "}<code>{runId}</code>.
        </div>
      )}
      {state.status === "error" && (
        <div role="alert" className="problems">
          <strong>Can't load this decision.</strong> {state.message}
        </div>
      )}
      {state.status === "ready" && <Ready d={state.data} />}
    </main>
  );
}

function Ready({ d }: { d: Detail }) {
  const n = d.numbers;
  const keys = n ? [
    { label: "Shares", value: n.shares === null ? MISSING : String(n.shares), cls: "" },
    { label: "Buy at", value: n.entry ?? MISSING, cls: "" },
    { label: "Sell for a profit at", value: n.target ?? MISSING, cls: "teal" },
    { label: "Most it can lose", value: showMoney(n.max_loss), cls: "orange" },
  ] : [];
  return (
    <>
      <section aria-label="Summary" className="stack">
        <div className="detail-head">
          <h1>{d.symbol ?? MISSING}</h1>
          {d.outcome && (
            <span className={`outcome big outcome-${d.outcome.toLowerCase()}`}>
              {OUTCOME_LABEL[d.outcome]}
            </span>
          )}
          <span className="faint detail-when">{d.at ? `${showWhen(d.at)} ET` : MISSING}</span>
        </div>

        {d.problems.length > 0 && (
          <div role="alert" className="problems">
            <strong>Problems</strong>
            <ul>{d.problems.map((p, i) => <li key={i}>{p.text}</li>)}</ul>
          </div>
        )}

        <div className="in-short">
          <div className="stack-tight in-short-text">
            <span className="eyebrow">In short</span>
            <p>{d.in_short}</p>
          </div>
          {n ? (
            <dl className="key-numbers">
              {keys.map((k) => (
                <div key={k.label}>
                  <dt>{k.label}</dt>
                  <dd className={`mono ${k.cls}`}>{k.value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="muted small">No order was sized, so there are no numbers.</p>
          )}
        </div>

        <nav aria-label="Jump to step" className="jumps">
          <span className="faint">How it got there:</span>
          {d.steps.map((s) => (
            <button key={s.key} type="button" className={`jump jump-${s.state}`}
              onClick={() => document.getElementById(`step-${s.key}`)?.scrollIntoView()}>
              {s.n} {s.title}
            </button>
          ))}
        </nav>
      </section>

      {d.steps.map((s) => <StepCard key={s.key} s={s} d={d} />)}
    </>
  );
}

function StepCard({ s, d }: { s: Step; d: Detail }) {
  return (
    <section id={`step-${s.key}`} aria-labelledby={`h-${s.key}`} className="card step-card">
      <div className="step-title">
        <h2 id={`h-${s.key}`} className="h4">{s.n} · {s.title}</h2>
        <span className={`state state-${s.state}`}>{STATE_LABEL[s.state]}</span>
        <span className={`chip chip-${s.actor} tiny`}>{s.actor === "ai" ? "AI" : s.actor === "you" ? "You" : "Code"}</span>
      </div>
      <p className="text2">{s.line}</p>
      {s.details && <Details s={s} d={d} />}
    </section>
  );
}

function Toggle({ label, openLabel, children }: {
  label: string; openLabel: string; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="link-button" aria-expanded={open}
        onClick={() => setOpen(!open)}>{open ? `${openLabel} ▴` : `${label} ▾`}</button>
      {open && children}
    </>
  );
}

function Details({ s, d }: { s: Step; d: Detail }) {
  const x = s.details as Record<string, unknown>;
  switch (s.key) {
    case "analysts": return <Analysts list={(x.analysts as AnalystView[]) ?? []} />;
    case "plans": return <Plans list={(x.plans as PlanView[]) ?? []} />;
    case "debate": return <Debate x={x} />;
    case "trader":
      return x.reasoning ? <p className="muted plain">{String(x.reasoning)}</p> : null;
    case "order_ticket": return <Ticket x={x} d={d} />;
    case "approve_and_log": return <Events events={(x.events as OrderEvent[]) ?? []} x={x} />;
    default: return null;
  }
}

function Analysts({ list }: { list: AnalystView[] }) {
  return (
    <>
      <div className="cards">
        {list.map((a) => (
          <div key={a.agent} className="card analyst">
            <div className="analyst-head">
              <span className="strong">{a.name}</span>
              {a.ok === true && (
                <span className={`stance stance-${a.stance}`}>{a.stance}</span>
              )}
              {a.ok === false && (
                <span className="state state-failed">failed ({a.reason_code ?? "unknown"})</span>
              )}
              {a.ok === null && <span className="state state-not_recorded">not recorded</span>}
              {a.ok === true && <span className="mono tiny muted">{a.confidence}</span>}
            </div>
          </div>
        ))}
      </div>
      {list.some((a) => a.evidence.length > 0) && (
        <Toggle label="Show each analyst's evidence" openLabel="Hide evidence">
          <div className="cards">
            {list.filter((a) => a.evidence.length > 0).map((a) => (
              <div key={a.agent} className="card analyst">
                <span className="strong">{a.name}</span>
                <ul className="evidence">
                  {a.evidence.map((e, i) => (
                    <li key={i} className="plain">{e.point} <span className="mono tiny faint">{e.source}</span></li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </Toggle>
      )}
    </>
  );
}

function PlanRow({ p }: { p: PlanView }) {
  return (
    <tr className={p.chosen ? "chosen" : ""}>
      <td>{p.chosen ? "✓ " : ""}{p.plan_id} · {p.setup}</td>
      <td>{p.entry ?? MISSING}</td><td>{p.stop ?? MISSING}</td>
      <td>{p.target ?? MISSING}</td><td>{p.reward_to_risk ?? MISSING}</td>
    </tr>
  );
}

function Plans({ list }: { list: PlanView[] }) {
  if (list.length === 0) return null;
  const chosen = list.filter((p) => p.chosen);
  const shown = chosen.length > 0 ? chosen : list.slice(0, 1);
  const others = list.filter((p) => !shown.includes(p));
  const head = (
    <thead><tr><th>Plan</th><th>Buy at</th><th>Stop-loss</th><th>Target</th><th>Reward : risk</th></tr></thead>
  );
  return (
    <div className="table-scroll">
      <table className="plans mono">
        {head}
        <tbody>{shown.map((p) => <PlanRow key={p.plan_id} p={p} />)}</tbody>
      </table>
      {others.length > 0 && (
        <Toggle label={`${others.length} other plan${others.length > 1 ? "s" : ""} considered`}
          openLabel="Hide the other plans">
          <table className="plans mono">{head}<tbody>{others.map((p) => <PlanRow key={p.plan_id} p={p} />)}</tbody></table>
        </Toggle>
      )}
    </div>
  );
}

function Debate({ x }: { x: Record<string, unknown> }) {
  const turns = (x.turns as TurnView[]) ?? [];
  const rounds = [...new Set(turns.map((t) => t.round))];
  return (
    <>
      <div className="cards">
        {x.bull ? <div className="card side"><span className="side-bull">BULL</span><span className="plain text2">{String(x.bull)}</span></div> : null}
        {x.bear ? <div className="card side"><span className="side-bear">BEAR</span><span className="plain text2">{String(x.bear)}</span></div> : null}
      </div>
      {turns.length > 0 && (
        <Toggle label={`Read the full debate (${rounds.length} round${rounds.length > 1 ? "s" : ""})`}
          openLabel="Hide the rounds">
          {rounds.map((r) => (
            <div key={String(r)} className="cards">
              {turns.filter((t) => t.round === r).map((t, i) => (
                <article key={i} className="card side">
                  <span className={t.side === "bull" ? "side-bull" : "side-bear"}>
                    {t.side?.toUpperCase()} · round {r}{t.fatal ? " · fatal" : ""}
                    {t.ok === false ? " · call failed" : ""}
                  </span>
                  {t.argument && <p className="plain text2">{t.argument}</p>}
                  <ul className="evidence">{t.points.map((p, j) => <li key={j} className="plain">{p}</li>)}</ul>
                </article>
              ))}
            </div>
          ))}
        </Toggle>
      )}
    </>
  );
}

function Ticket({ x, d }: { x: Record<string, unknown>; d: Detail }) {
  const gate = x.gate as { status: string; reason: string | null } | undefined;
  const reasons = (x.risk_reasons as string[]) ?? [];
  const n = d.numbers;
  return (
    <>
      {n && (
        <dl className="minor">
          <div><dt className="mono">{n.limit_price ?? MISSING}</dt><dd>limit price (as recorded)</dd></div>
          <div><dt className="mono">{n.stop ?? MISSING}</dt><dd>stop-loss</dd></div>
          <div><dt className="mono">{x.decided_by ? String(x.decided_by) : MISSING}</dt><dd>size set by</dd></div>
        </dl>
      )}
      {reasons.length > 0 && (
        <ul className="evidence">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      )}
      {gate && (
        <Toggle label="Show the earnings and halt checks" openLabel="Hide the earnings and halt checks">
          <p className="text2">
            <span className={gate.status === "CLEAR" ? "gate-clear mono" : "gate-warn mono"}>{gate.status}</span>
            {gate.reason ? ` · ${gate.reason}` : ""}
          </p>
        </Toggle>
      )}
    </>
  );
}

function Events({ events, x }: { events: OrderEvent[]; x: Record<string, unknown> }) {
  const items = [
    ...(x.approved_at ? [{ status: "approved", at: String(x.approved_at), fields: {} }] : []),
    ...events,
  ];
  if (items.length === 0) return null;
  return (
    <ol className="events">
      {items.map((e, i) => (
        <li key={i} className="event">
          <span className="strong small">{e.status.replace(/_/g, " ")}</span>
          <span className="mono tiny muted">{showWhen(e.at)}</span>
          {Object.entries(e.fields).map(([k, v]) => (
            <span key={k} className="mono tiny faint">{k.replace(/_/g, " ")}: {v}</span>
          ))}
        </li>
      ))}
    </ol>
  );
}
