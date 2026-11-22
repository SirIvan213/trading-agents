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
