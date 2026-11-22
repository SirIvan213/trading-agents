import { describe, expect, it } from "vitest";

import { parseHash } from "../router";

describe("parseHash", () => {
  it.each([
    ["", { page: "about" }],
    ["#", { page: "about" }],
    ["#/", { page: "about" }],
    ["#/live", { page: "live" }],
    ["#/history", { page: "history" }],
    ["#/decision/run-20261123-TSLA_1", { page: "decision", id: "run-20261123-TSLA_1" }],
  ])("%s", (hash, route) => {
    expect(parseHash(hash)).toEqual(route);
  });

  it.each(["#/decision/", "#/decision/a/b", "#/decision/<x>", "#/decision/a.b",
    `#/decision/${"x".repeat(65)}`, "#/nope", "#steps"])(
    "%s is not found",
    (hash) => {
      expect(parseHash(hash).page).toBe("not_found");
    },
  );
});
