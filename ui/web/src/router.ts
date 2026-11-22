// Hash routes, no router library: #/ (How it works), #/live, #/history, #/decision/:id.
import { useEffect, useState } from "react";

export type Route =
  | { page: "about" }
  | { page: "live" }
  | { page: "history" }
  | { page: "decision"; id: string }
  | { page: "not_found"; hash: string };

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#/, "") || "/";
  if (path === "/") return { page: "about" };
  if (path === "/live") return { page: "live" };
  if (path === "/history") return { page: "history" };
  const decision = /^\/decision\/([A-Za-z0-9_.-]{1,128})$/.exec(path);
  if (decision) return { page: "decision", id: decision[1] };
  return { page: "not_found", hash };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
