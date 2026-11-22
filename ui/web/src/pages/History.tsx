// History (UI-3, #216): every decision the journal records, newest first, with the outcome
// filter, 4 tiles, the table and extra columns behind a toggle. Everything shown is recorded:
// "why" is a fixed string the API mapped from a recorded code, money is the recorded string,
// and the event check is the recorded gate, "check failed" or "not reached". All text renders
// as plain text.
import { useCallback, useEffect, useRef, useState } from "react";

import { getJson } from "../api";
import { MISSING, showMoney, showWhen } from "../format";
import type { DecisionRow, Decisions, Outcome, OutcomeFilter } from "../types";

const FILTERS: { key: OutcomeFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "buy", label: "BUY" },
  { key: "pass", label: "PASS" },
  { key: "skipped", label: "Skipped" },
  { key: "failed", label: "Failed" },
];
const OUTCOME_LABEL: Record<Outcome, string> = {
  BUY: "BUY", PASS: "PASS", SKIPPED: "Skipped", FAILED: "Failed",
};
const PAGE = 50;

type State =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: Decisions; loadingMore: boolean; refreshing?: boolean };

function url(filter: OutcomeFilter, cursor: string | null): `/api/${string}` {
  const q = new URLSearchParams({ outcome: filter, limit: String(PAGE) });
  if (cursor) q.set("cursor", cursor);
  return `/api/decisions?${q.toString()}`;
}

export function History() {
  const [filter, setFilter] = useState<OutcomeFilter>("all");
  const [extra, setExtra] = useState(false);
  const [state, setState] = useState<State>({ status: "loading" });
  // every request takes a number; only the latest request's reply may change the table, so a
  // page that returns after the filter changed (even changed and changed back) is dropped
  const latest = useRef(0);

  useEffect(() => {
    const mine = ++latest.current;
    let live = true;
    // keep the current table (and the filters) on screen while the next filter loads
    setState((prev) => (prev.status === "ready" ? { ...prev, refreshing: true }
      : { status: "loading" }));
    getJson<Decisions>(url(filter, null))
      .then((data) => live && latest.current === mine
        && setState({ status: "ready", data, loadingMore: false }))
      .catch((e: unknown) => live && latest.current === mine && setState({
        status: "error", message: e instanceof Error ? e.message : String(e),
      }));
    return () => { live = false; };
  }, [filter]);

  const loadMore = useCallback(() => {
    if (state.status !== "ready" || !state.data.next_cursor) return;
    const before = state.data;
    setState({ status: "ready", data: before, loadingMore: true });
    const mine = ++latest.current;
    getJson<Decisions>(url(filter, before.next_cursor))
      .then((more) => {
        if (latest.current !== mine) return; // a newer request started: drop this page
        // the list grows during the day, so an offset page can repeat rows: never twice
        const seen = new Set(before.items.map((i) => i.run_id));
        setState({
          status: "ready", loadingMore: false,
          data: { ...more, items: [...before.items, ...more.items.filter((i) => !seen.has(i.run_id))] },
        });
      })
      .catch((e: unknown) => {
        if (latest.current !== mine) return;
        setState({ status: "error", message: e instanceof Error ? e.message : String(e) });
      });
  }, [state, filter]);

  return (
    <main className="page history">
      <div className="stack-tight">
        <h1>History</h1>
        <p className="muted">
          Every stock the system looked at and what it decided, newest first. Open a stock to
          see the full decision. <a href="#/">How does it decide?</a>
        </p>
      </div>

      {state.status === "loading" && <p className="muted">Loading…</p>}
      {state.status === "error" && (
        <div role="alert" className="problems">
          <strong>Can't load the history.</strong> {state.message}
        </div>
      )}
      {state.status === "ready" && (
        <Ready data={state.data} filter={filter} setFilter={setFilter} extra={extra}
          setExtra={setExtra} loadMore={loadMore} loadingMore={state.loadingMore}
          refreshing={state.refreshing === true} />
      )}
    </main>
  );
}

function Ready({ data, filter, setFilter, extra, setExtra, loadMore, loadingMore, refreshing }: {
  data: Decisions; filter: OutcomeFilter; setFilter: (f: OutcomeFilter) => void;
  extra: boolean; setExtra: (b: boolean) => void; loadMore: () => void; loadingMore: boolean;
  refreshing: boolean;
}) {
  const t = data.tiles;
  const tiles = [
    { label: "Stocks the AI reviewed", value: t.reviewed, note: "ended in BUY or PASS" },
    { label: "Trades proposed (BUY)", value: t.buys, note: "the Trader chose a plan" },
    { label: "Passed", value: t.passed, note: "decided not to trade" },
    { label: "Skipped early", value: t.skipped, note: "stopped before any AI ran" },
  ];
  return (
    <>
      {data.problems.length > 0 && (
        <div role="alert" className="problems">
          <strong>Problems reading the journal</strong>
          <ul>{data.problems.map((p, i) => <li key={i}>{p.text}</li>)}</ul>
        </div>
      )}

      <section aria-label="Totals" className="cards tiles">
        {tiles.map((x) => (
          <div key={x.label} className="card tile">
            <span className="tiny muted">{x.label}</span>
            <span className="tile-value mono">{x.value}</span>
            <span className="tiny faint">{x.note}</span>
          </div>
        ))}
      </section>

      <section aria-labelledby="tbl-h" className="card table-card" aria-busy={refreshing}>
        <div className="table-head">
          <h2 id="tbl-h" className="h4">Decisions</h2>
          <div role="group" aria-label="Filter by outcome" className="filters">
            {FILTERS.map((f) => (
              <button key={f.key} type="button" aria-pressed={filter === f.key}
                className={filter === f.key ? "filter on" : "filter"}
                onClick={() => setFilter(f.key)}>
                {f.label} <span className="faint">{data.counts[f.key]}</span>
              </button>
            ))}
          </div>
        </div>

        {data.items.length === 0 ? (
          <p className="muted">No decisions here yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="decisions">
              <thead>
                <tr>
                  <th scope="col">When (ET)</th>
                  <th scope="col">Stock</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Why</th>
                  {extra && <th scope="col">Event check</th>}
                  {extra && <th scope="col">Run id</th>}
                  {extra && <th scope="col">Plan id</th>}
                  {extra && <th scope="col">Size set by</th>}
                  <th scope="col">Order</th>
                  <th scope="col">Profit / loss</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => <Row key={r.run_id} r={r} extra={extra} />)}
              </tbody>
            </table>
          </div>
        )}

        <div className="table-foot">
          <button type="button" className="link-button" aria-expanded={extra}
            onClick={() => setExtra(!extra)}>
            {extra ? "Hide extra columns ▴" : "Show more columns ▾"}
          </button>
          <span className="tiny faint">
            Showing {data.items.length} of {data.total}.
          </span>
          {data.next_cursor && (
            <button type="button" className="button small-button" onClick={loadMore}
              disabled={loadingMore}>
              {loadingMore ? "Loading…" : "Show more"}
            </button>
          )}
          <span className="tiny faint pnl-total">
            Realized profit / loss:{" "}
            <span className="mono">
              {t.realized_pnl === null ? `${MISSING} until a trade closes` : showMoney(t.realized_pnl)}
            </span>
          </span>
        </div>
      </section>
    </>
  );
}

function Row({ r, extra }: { r: DecisionRow; extra: boolean }) {
  const outcomeClass = `outcome outcome-${r.outcome.toLowerCase()}`;
  const fatal = r.forced_pass === "fatal_bear";
  const gateClass = r.gate.status === "CLEAR" ? "gate-clear"
    : r.gate.status === "not reached" ? "faint" : "gate-warn";
  return (
    <tr>
      <td className="mono muted nowrap">{showWhen(r.at)}</td>
      <td className="strong">
        <a href={`#/decision/${encodeURIComponent(r.run_id)}`}>{r.symbol ?? MISSING}</a>
      </td>
      <td><span className={outcomeClass}>{OUTCOME_LABEL[r.outcome]}{fatal ? " · fatal" : ""}</span></td>
      <td className="text2">
        {r.why.text}
        {r.why.detail && <div className="tiny faint">{r.why.detail}</div>}
      </td>
      {extra && (
        <td className={`mono tiny ${gateClass}`} title={r.gate.reason ?? undefined}>
          {r.gate.status}
        </td>
      )}
      {extra && <td className="mono tiny muted">{r.run_id}</td>}
      {extra && <td className="mono tiny muted">{r.plan_id ?? MISSING}</td>}
      {extra && <td className="mono tiny muted">{r.decided_by ?? MISSING}</td>}
      <td className="muted">{r.order_status?.text ?? MISSING}</td>
      <td className="mono faint">{showMoney(r.realized_pnl)}</td>
    </tr>
  );
}
