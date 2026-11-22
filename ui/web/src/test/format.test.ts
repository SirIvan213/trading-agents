import { describe, expect, it } from "vitest";

import { showCount, showLimit, showMoney, showTime, showWhen } from "../format";

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
  it("shows recorded money with a dollar sign, digits unchanged", () => {
    expect(showMoney("13.55")).toBe("$13.55");
    expect(showMoney("-31.65")).toBe("-$31.65");
    expect(showMoney("-0.00")).toBe("-$0.00");
    expect(showMoney("45.2")).toBe("$45.2");
    expect(showMoney(null)).toBe("—");
  });
  it("shows the recorded New York time as written, in any browser time zone", () => {
    expect(showWhen("2026-11-23T10:05:00-05:00")).toBe("Nov 23 10:05");
    expect(showWhen("2026-07-01T09:45:12-04:00")).toBe("Jul 1 09:45");
    expect(showWhen("odd")).toBe("odd");
    expect(showWhen(null)).toBe("—");
  });
  it("groups big counts", () => {
    expect(showCount(600000)).toBe("600,000");
  });
});
