// The page's only network access: GET requests to its own origin's /api (CLAUDE.md, UI
// track; the server's Content-Security-Policy allows connect-src 'self' only).

export class ApiError extends Error {
  /** The HTTP status the server answered with, or null when it did not answer. */
  readonly status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.status = status;
  }
}

export async function getJson<T>(path: `/api/${string}`): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { method: "GET", headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("The local UI server did not answer. Is python -m ui.api running?");
  }
  if (!response.ok) {
    throw new ApiError(`The local UI server answered ${response.status} for ${path}.`,
      response.status);
  }
  return (await response.json()) as T;
}

export type Polled<T> = { changed: true; data: T; etag: string | null } | { changed: false };

/** A GET that sends If-None-Match: a 304 means nothing changed since `etag`. Sending the
 * header makes the browser skip its own cache (Fetch standard), so a 304 reaches here. */
export async function pollJson<T>(path: `/api/${string}`, etag: string | null): Promise<Polled<T>> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (etag) headers["If-None-Match"] = etag;
  let response: Response;
  try {
    response = await fetch(path, { method: "GET", headers, cache: "no-store" });
  } catch {
    throw new ApiError("The local UI server did not answer. Is python -m ui.api running?");
  }
  if (response.status === 304) return { changed: false };
  if (!response.ok) {
    throw new ApiError(`The local UI server answered ${response.status} for ${path}.`,
      response.status);
  }
  return { changed: true, data: (await response.json()) as T, etag: response.headers.get("ETag") };
}
