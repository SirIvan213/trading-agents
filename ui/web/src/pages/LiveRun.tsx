// Live run (UI-5, #218): what run_day is doing today, polled from GET /api/live every 2 s
// with an ETag. Everything shown is recorded: the status is a fixed sentence the API built
// from recorded state, money is the recorded string, and the heartbeat's phase is plain text
// that is never read for numbers. Problems are never hidden: a failed poll keeps the last
// answer on screen under a warning that says how old it is.
import { useEffect, useRef, useState } from "react";

import { pollJson } from "../api";
import { MISSING, showMoney, showWhen } from "../format";
import type { Live, LiveLimit, LiveOutcome, LivePosition, LiveRow } from "../types";

export const POLL_MS = 2000;
const INTRO_KEY = "live-intro-dismissed";

const OUTCOME: Record<LiveOutcome, { label: string; cls: string }> = {
  BUY: { label: "BUY", cls: "outcome-buy" },
  PASS: { label: "PASS", cls: "outcome-pass" },
  SKIPPED: { label: "Skipped", cls: "outcome-skipped" },
  FAILED: { label: "Failed", cls: "outcome-failed" },
  DECIDING: { label: "Deciding", cls: "outcome-deciding" },
  NO_OUTCOME: { label: "No outcome", cls: "outcome-failed" },
};
const KIND_CLASS: Record<Live["status"]["kind"], string> = {
  normal: "dot-ok", order_waiting: "dot-warn", blocked: "dot-warn", idle: "dot-idle",
  not_running: "dot-idle",
};

/** Not running while the market is open is worth a look (run_day should be running); outside
 * market hours it is the calm, ordinary state. */
function dotClass(kind: Live["status"]["kind"], market: Live["market"]["state"]): string {
  if (kind === "not_running" && market === "open") return "dot-warn";
  return KIND_CLASS[kind];
}

type State = {
  data: Live | null;
  error: string | null; // the last poll failed: the data below is from `asOf`
  asOf: Date | null; // when the shown data was last confirmed by the server
};

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(INTRO_KEY) === "1";
  } catch {
    return false;
  }
}

/** The fields the page reads, checked before anything renders: an answer of another shape
 * (an older server, a proxy page) is an error on screen, never a crash. */
export function isLive(x: unknown): x is Live {
  const d = x as Live | null;
  const list = (v: unknown) => Array.isArray(v);
  return typeof d === "object" && d !== null
    && typeof d.status === "object" && d.status !== null
    && typeof d.status.sentence === "string" && list(d.status.reasons)
    && typeof d.market === "object" && d.market !== null
    && list(d.key_limits) && list(d.caps) && list(d.today) && list(d.problems)
    && (d.positions === null || list(d.positions))
    && typeof d.sources === "object" && d.sources !== null && list(d.sources.items)
    && typeof d.run_day === "object" && d.run_day !== null;
}

function useLive(): State {
  const [state, setState] = useState<State>({ data: null, error: null, asOf: null });
  const etag = useRef<string | null>(null);
  const busy = useRef(false);

  useEffect(() => {
    let live = true;
    const tick = () => {
      if (busy.current) return; // never two polls at once
      busy.current = true;
      pollJson<Live>("/api/live", etag.current)
        .then((r) => {
          if (!live) return;
          if (r.changed && !isLive(r.data)) {
            etag.current = null;
            setState((s) => ({ ...s, error: "The local UI server's answer has an unexpected "
              + "shape (is the server the same version as this page?)." }));
          } else if (r.changed) {
            etag.current = r.etag;
            setState({ data: r.data, error: null, asOf: new Date() });
          } else {
            setState((s) => ({ ...s, error: null, asOf: new Date() }));
          }
        })
        .catch((e: unknown) => {
          if (!live) return;
          setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e) }));
        })
        .finally(() => { busy.current = false; });
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { live = false; window.clearInterval(id); };
  }, []);
  return state;
}

export function LiveRun() {
  const { data, error, asOf } = useLive();
  const [intro, setIntro] = useState(() => !readDismissed());

  const dismiss = () => {
    setIntro(false);
    try {
      window.localStorage.setItem(INTRO_KEY, "1");
    } catch {
      // a per-viewer convenience only: the banner just comes back next time
    }
  };

  return (
    <main className="page live">
      <h1 className="visually-hidden">Live run</h1>
      {intro && (
        <section aria-label="What this is" className="intro">
          <span className="intro-text">
            AI agents research stocks, argue for and against buying, and either propose one
            trade or pass. Code enforces every limit, and a person approves each order.
            Practice money only.
          </span>
          <a href="#/" className="strong nowrap">How it works →</a>
          <button type="button" className="dismiss" aria-label="Dismiss" onClick={dismiss}>×</button>
        </section>
      )}

      {error && (
        <div role="alert" className="problems">
          <strong>Live data is not updating.</strong> {error}{" "}
          {data && asOf
            ? `What's below was last confirmed at ${asOf.toLocaleTimeString("en-US")} and may be out of date.`
            : "Nothing to show yet."}
        </div>
      )}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && <Ready data={data} />}
    </main>
  );
}

function Ready({ data }: { data: Live }) {
  const [allLimits, setAllLimits] = useState(false);
  const st = data.status;
  const limits = allLimits ? [...data.key_limits, ...data.caps] : data.key_limits;
  return (
    <>
      {data.problems.length > 0 && (
        <div role="alert" className="problems">
          <strong>Problems</strong>
          <ul>{data.problems.map((p, i) => <li key={i}>{p.text}</li>)}</ul>
        </div>
      )}

      <section aria-label="Status" className={`card status-card status-${st.kind}`}>
        <div className="status-line">
          <span className={`dot big-dot ${dotClass(st.kind, data.market.state)}`}
            aria-hidden="true" />
          <span className="status-sentence" data-kind={st.kind}>{st.sentence}</span>
          <span className="small muted">{data.market.text}</span>
        </div>
        {st.reasons.length > 1 && (
          <ul className="reasons small">
            {st.reasons.map((r) => <li key={r.code + r.text}>{r.text}</li>)}
          </ul>
        )}
        <div className="limits">
          {limits.map((l) => <LimitView key={l.key} l={l} />)}
        </div>
        <button type="button" className="link-button" aria-expanded={allLimits}
          onClick={() => setAllLimits(!allLimits)}>
          {allLimits ? "Show fewer limits ▴"
            : `Show all ${data.key_limits.length + data.caps.length} daily limits ▾`}
        </button>
      </section>

      <div className="live-columns">
        <Today rows={data.today} />
        <Now data={data} />
        <aside className="live-side">
          <Positions positions={data.positions} />
          <Sources sources={data.sources} />
        </aside>
      </div>
    </>
  );
}

function showUsed(l: LiveLimit, v: string | null): string {
  if (v === null) return MISSING;
  if (l.unit === "usd") return showMoney(v);
  if (l.unit === "tokens" && /^\d+$/.test(v)) return Number(v).toLocaleString("en-US");
  return v;
}

function LimitView({ l }: { l: LiveLimit }) {
  return (
    <div className="limit" data-key={l.key}>
      <span className="tiny muted">{l.label}</span>
      <span className="mono limit-value">
        {showUsed(l, l.used)} <span className="faint">of {showUsed(l, l.limit)}</span>
        {l.reached && <span className="tiny reached"> · reached</span>}
      </span>
      <span className="bar" aria-hidden="true">
        <span className={l.reached ? "bar-fill bar-reached" : "bar-fill"}
          style={{ width: `${l.pct ?? 0}%` }} />
      </span>
      {l.note && <span className="tiny faint">{l.note}</span>}
    </div>
  );
}

/** "2026-11-23T10:05:00-05:00" -> "10:05", read from the text (never the browser's zone). */
function clock(iso: string | null): string {
  if (iso === null) return "";
  const m = /T(\d{2}):(\d{2})/.exec(iso);
  return m ? `${m[1]}:${m[2]}` : iso;
}

/** ":48" from "...T10:11:48-05:00": the seconds showWhen leaves out. */
function seconds(iso: string): string {
  const m = /T\d{2}:\d{2}:(\d{2})/.exec(iso);
  return m ? `:${m[1]}` : "";
}

function Today({ rows }: { rows: LiveRow[] }) {
  return (
    <section aria-labelledby="today-h" className="card list-card today">
      <div className="list-head">
        <h2 id="today-h" className="h4">Today's stocks</h2>
        <span className="tiny muted">{rows.length} so far</span>
      </div>
      {rows.length === 0 ? (
        <p className="muted list-empty">No stock has been looked at today.</p>
      ) : (
        <ul className="today-list">
          {rows.map((r, i) => {
            const o = OUTCOME[r.outcome] ?? { label: r.outcome, cls: "outcome-failed" };
            const body = (
              <>
                <span className="mono tiny faint today-time">{clock(r.at)}</span>
                <span className="today-main">
                  <span className="strong">{r.symbol ?? MISSING}</span>
                  <span className="tiny muted">
                    {r.why}{r.order_status ? ` · ${r.order_status.text}` : ""}
                  </span>
                </span>
                <span className={`outcome ${o.cls}`}>{o.label}</span>
              </>
            );
            return (
              <li key={r.run_id ?? `${r.symbol}-${i}`}>
                {r.run_id ? <a className="today-row" href={`#/decision/${r.run_id}`}>{body}</a>
                  : <span className="today-row">{body}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Now({ data }: { data: Live }) {
  const cur = data.current;
  const hb = data.run_day;
  return (
    <section aria-labelledby="now-h" className="card now">
      <div className="now-head">
        <h2 id="now-h" className="eyebrow">{cur?.live ? "Now" : "Last known step"}</h2>
        <span className="now-symbol">{cur?.symbol ?? MISSING}</span>
      </div>
      <div className="now-line">
        <span className="tiny strong muted nowrap">RUN_DAY</span>
        <span className="plain">{cur ? cur.line : "Nothing is being decided."}</span>
      </div>
      <p className="tiny muted">
        {hb.heartbeat_at === null ? "No heartbeat from run_day."
          : `Last heartbeat: ${showWhen(hb.heartbeat_at)}${seconds(hb.heartbeat_at)} ET`
            + (hb.future ? " · dated in the future" : hb.stale ? " · stale" : "")}
        {hb.last_end && ` · run_day ended${hb.last_end.at ? ` at ${clock(hb.last_end.at)}` : ""}`}
      </p>
      <p className="tiny faint">
        The analysts' views and the debate show up here once each decision records its steps;
        for now, open a stock in the list for its full decision.
      </p>
    </section>
  );
}

function Positions({ positions }: { positions: LivePosition[] | null }) {
  return (
    <section aria-labelledby="pos-h" className="card side-card">
      <h2 id="pos-h" className="h4">Open position</h2>
      {positions === null && (
        <p className="orange">Not shown: the journal's order history does not replay.</p>
      )}
      {positions !== null && positions.length === 0 && (
        <p className="muted">None. The system holds at most one stock at a time.</p>
      )}
      {positions?.map((p) => (
        <div key={p.client_order_id} className="position" data-symbol={p.symbol}>
          <div className="position-head">
            <span className="position-symbol">{p.symbol}</span>
            <span className="muted">{p.qty} shares</span>
          </div>
          <span className="small">{p.status.text}</span>
          <span className="small text2">
            {p.avg_price === null ? "Fill price not recorded" : `Filled ${p.filled_qty} at ${p.avg_price}`}.
            {" "}Stop <span className="mono">{p.stop ?? MISSING}</span>, target{" "}
            <span className="mono">{p.target ?? MISSING}</span>; the most it can lose is{" "}
            <span className="mono">{showMoney(p.max_loss)}</span>.
          </span>
          <span className="tiny faint">
            Unrealized profit / loss: {MISSING} (the UI has no live quotes)
          </span>
          <a className="small" href={`#/decision/${p.run_id}`}>Open the decision →</a>
        </div>
      ))}
    </section>
  );
}

function Sources({ sources }: { sources: Live["sources"] }) {
  const [open, setOpen] = useState(false);
  const shown = sources.expanded || open;
  return (
    <section aria-labelledby="src-h"
      className={sources.ok ? "card side-card" : "card side-card card-warn"}>
      <h2 id="src-h" className="h4">Data sources</h2>
      <div className="source-line">
        <span className={`dot ${sources.ok ? "dot-ok" : "dot-warn"}`} aria-hidden="true" />
        <span className={sources.ok ? "text2" : "orange strong"}>{sources.summary}</span>
      </div>
      {shown && (
        <ul className="source-list">
          {sources.items.map((s) => (
            <li key={s.name}>
              <span className={`dot ${s.ok === true ? "dot-ok" : s.ok === false ? "dot-warn" : "dot-idle"}`}
                aria-hidden="true" />
              <span className="source-name small">{s.name}</span>
              <span className={s.ok === false ? "tiny orange strong" : "tiny muted"}>{s.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {!sources.expanded && (
        <button type="button" className="link-button" aria-expanded={open}
          onClick={() => setOpen(!open)}>
          {open ? "Hide details ▴" : "Show details ▾"}
        </button>
      )}
    </section>
  );
}
