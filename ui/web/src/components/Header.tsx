import type { Route } from "../router";

const NAV = [
  { href: "#/live", label: "Live run", pages: ["live"] },
  { href: "#/history", label: "History", pages: ["history", "decision"] },
  { href: "#/", label: "How it works", pages: ["about"] },
] as const;

export function Header({ route }: { route: Route }) {
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
      <span className="paper-badge">PAPER · practice money</span>
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
