// The page's only network access: GET requests to its own origin's /api (CLAUDE.md, UI
// track; the server's Content-Security-Policy allows connect-src 'self' only).

export class ApiError extends Error {}

export async function getJson<T>(path: `/api/${string}`): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { method: "GET", headers: { Accept: "application/json" } });
  } catch {
    throw new ApiError("The local UI server did not answer. Is python -m ui.api running?");
  }
  if (!response.ok) {
    throw new ApiError(`The local UI server answered ${response.status} for ${path}.`);
  }
  return (await response.json()) as T;
}
