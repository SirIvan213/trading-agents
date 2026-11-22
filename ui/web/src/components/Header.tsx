import { useEffect, useState } from "react";

import { getJson } from "../api";
import type { Route } from "../router";

const NAV = [
  { href: "#/live", label: "Live run", pages: ["live"] },
  { href: "#/history", label: "History", pages: ["history", "decision"] },
  { href: "#/", label: "How it works", pages: ["about"] },
] as const;

// From GET /api/health: the limits config as recorded (null: it could not be read).
interface Mode {
  paper: boolean | null;
  trading_enabled: boolean | null;
}

type ModeState = { status: "loading" } | { status: "error" } | { status: "ready"; mode: Mode };

/** The PAPER badge says only what the config says: never "PAPER" when it is false or unknown. */
function useMode(): ModeState {
  const [state, setState] = useState<ModeState>({ status: "loading" });
  useEffect(() => {
    let live = true;
    getJson<Mode>("/api/health")
      .then((mode) => live && setState({ status: "ready", mode }))
      .catch(() => live && setState({ status: "error" }));
    return () => { live = false; };
  }, []);
  return state;
}

function ModeBadges({ state }: { state: ModeState }) {
  if (state.status === "loading") return <span className="badge badge-muted">checking config…</span>;
  if (state.status === "error") {
    return (
      <span className="badge badge-warn" role="status">
        Mode unknown: the server did not answer or returned an error
      </span>
    );
  }
  const { paper, trading_enabled } = state.mode;
  return (
    <>
      {paper === true && <span className="paper-badge">PAPER · practice money</span>}
      {paper === false && (
        <span className="badge badge-warn" role="status">NOT PAPER: the config says paper: false</span>
      )}
      {paper !== true && paper !== false && (
        <span className="badge badge-warn" role="status">Mode unknown: the limits config can't be read</span>
      )}
      {trading_enabled === false && (
        <span className="badge badge-warn" role="status">Kill switch on: no new orders</span>
      )}
    </>
  );
}

export function Header({ route }: { route: Route }) {
  const mode = useMode();
  return (
    <header className="app-header">
      <a className="brand" href="#/">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3 17l5-6 4 4 8-9" />
          <path d="M14 6h6v6" />
        </svg>
        <span className="brand-name">trading-agents</span>
      </a>
      <ModeBadges state={mode} />
      <nav aria-label="Main" className="main-nav">
        {NAV.map((item) => {
          const current = (item.pages as readonly string[]).includes(route.page);
          return (
            <a key={item.href} href={item.href} aria-current={current ? "page" : undefined}
              className={current ? "nav-link nav-current" : "nav-link"}>
              {item.label}
            </a>
          );
        })}
      </nav>
    </header>
  );
}
