// A hash that matches no page.

export function NotFound({ hash }: { hash: string }) {
  return (
    <main className="page placeholder">
      <h1>Page not found</h1>
      <p className="muted">There is no page at <code>{hash}</code>.</p>
      <p><a href="#/">Go to How it works</a></p>
    </main>
  );
}
