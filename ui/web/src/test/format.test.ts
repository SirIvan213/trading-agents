import { describe, expect, it } from "vitest";

import { showCount, showLimit, showTime } from "../format";

describe("format", () => {
  it("shows money exactly as recorded", () => {
    expect(showLimit({ key: "k", label: "", unit: "usd", value: "50.00" })).toBe("$50.00");
    expect(showLimit({ key: "k", label: "", unit: "usd", value: "0.10" })).toBe("$0.10");
  });
  it("shows a missing value as a dash, never a default", () => {
    expect(showLimit({ key: "k", label: "", unit: "usd", value: null })).toBe("—");
    expect(showCount(null)).toBe("—");
  });
  it("shows New York times on a 12-hour clock", () => {
    expect(showTime("15:55")).toBe("3:55 PM ET");
    expect(showTime("09:45")).toBe("9:45 AM ET");
    expect(showTime("12:00")).toBe("12:00 PM ET");
    expect(showTime("00:05")).toBe("12:05 AM ET");
    expect(showTime("odd")).toBe("odd");
  });
  it("groups big counts", () => {
    expect(showCount(600000)).toBe("600,000");
  });
});
