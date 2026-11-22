// Pages not built yet: a plain "coming in UI-n" note, so nothing looks live that isn't.

const PLANNED = {
  live: { title: "Live run", pr: "UI-5", issue: 218 },
  history: { title: "History", pr: "UI-3", issue: 216 },
  decision: { title: "Decision detail", pr: "UI-4", issue: 217 },
} as const;

export function Placeholder({ page, id }: { page: keyof typeof PLANNED; id?: string }) {
  const p = PLANNED[page];
  return (
    <main className="page placeholder">
      <h1>{p.title}</h1>
      {id !== undefined && <p className="muted">Decision <code>{id}</code></p>}
      <p className="planned-box">
        This page is coming in {p.pr} (#{p.issue}). Nothing here reads live data yet.
      </p>
      <p><a href="#/">Back to How it works</a></p>
    </main>
  );
}

export function NotFound({ hash }: { hash: string }) {
  return (
    <main className="page placeholder">
      <h1>Page not found</h1>
      <p className="muted">There is no page at <code>{hash}</code>.</p>
      <p><a href="#/">Go to How it works</a></p>
    </main>
  );
}
