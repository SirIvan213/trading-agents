"""Regenerate the synthetic scenario fixtures (clean-buy, red-flag). Offline, deterministic.

    .venv/Scripts/python tests/fixtures/scenarios/build.py

Each scenario folder gets bars.json, snapshot.json, news.json, submissions.json and
companyfacts.json in the real API shapes, for a run "as of" 2026-11-03 11:00 ET (EST, UTC-5).
scenario.json (symbol, files, expected outcome) is hand-written and not touched here.
The company facts reuse the TSLA fixture's quarterly structure with scaled dollar values.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent
UTC_OPEN = datetime(2026, 11, 3, 14, 30)  # 09:30 ET on 2026-11-03 (EST)


def _bar(ts: datetime, o: float, c: float, wick: float, v: int) -> dict:
    o, c = round(o, 2), round(c, 2)
    return {
        "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "o": o, "h": round(max(o, c) + wick, 2), "l": round(min(o, c) - wick, 2), "c": c,
        "v": v, "n": max(1, v // 90), "vw": round((o + c) / 2, 2),
    }


def _path(points: list[tuple[int, float]]) -> list[float]:
    """Piecewise-linear closes by minute index (inclusive endpoints)."""
    out: list[float] = []
    for (m0, p0), (m1, p1) in zip(points, points[1:], strict=False):
        for m in range(m0, m1):
            out.append(p0 + (p1 - p0) * (m - m0) / (m1 - m0))
    out.append(points[-1][1])
    return out


# The shared chart (prices relative to a base): an opening spike that sets the session high,
# a fade into a quiet base around VWAP, then an orderly, rising-volume push off the base.
# Minute 0 = 09:30 ET, minute 89 = 10:59 ET.
SHAPE = [(0, 0.50), (2, 1.90), (4, 0.70), (14, -0.25), (30, 0.05), (45, -0.05),
         (60, 0.10), (75, 0.20), (89, 0.42)]
WOBBLE = [0.00, 0.04, -0.03, 0.02, -0.04, 0.03, -0.01, 0.02]


def bars(base: float, *, gap: range | None = None) -> dict:
    closes = _path(SHAPE)
    out = []
    # three pre-market bars (dropped by the regular-session filter)
    for i in range(3):
        ts = UTC_OPEN - timedelta(minutes=3 - i)
        out.append(_bar(ts, base + 0.30, base + 0.32, 0.03, 1500 + 200 * i))
    prev = base + 0.40
    for m, rel in enumerate(closes):
        if gap is not None and m in gap:
            continue  # trading halted: no bars
        c = base + rel + (WOBBLE[m % len(WOBBLE)] if 5 <= m < 85 else 0.0)
        if m < 5:
            v, wick = 60000 - 8000 * m, 0.12
        elif m < 15:
            v, wick = 22000, 0.07
        elif m < 60:
            v, wick = 9000 + 37 * (m % 7), 0.04
        else:
            v, wick = 14000 + 450 * (m - 60), 0.04
        out.append(_bar(UTC_OPEN + timedelta(minutes=m), prev, c, wick, v))
        prev = c
    return {"bars": out, "symbol": None, "next_page_token": None}


def snapshot(symbol: str, b: dict, prev_close: float) -> dict:
    regular = [x for x in b["bars"] if x["t"] >= "2026-11-03T14:30:00Z"]
    last = regular[-1]
    return {symbol: {
        "latestTrade": {"t": last["t"], "p": last["c"], "s": 100},
        "latestQuote": {"t": last["t"], "ap": round(last["c"] + 0.01, 2),
                        "bp": round(last["c"] - 0.01, 2), "as": 3, "bs": 4},
        "minuteBar": last,
        "dailyBar": {"t": "2026-11-03T05:00:00Z", "o": regular[0]["o"],
                     "h": max(x["h"] for x in regular), "l": min(x["l"] for x in regular),
                     "c": last["c"], "v": sum(x["v"] for x in regular)},
        "prevDailyBar": {"t": "2026-11-02T05:00:00Z", "o": round(prev_close - 0.6, 2),
                         "h": round(prev_close + 0.4, 2), "l": round(prev_close - 1.1, 2),
                         "c": prev_close, "v": 1200000},
    }}


def news(symbol: str, items: list[tuple[str, str, str]]) -> dict:
    return {"news": [
        {"id": 92000000 + i, "headline": h, "summary": s, "author": "fixture",
         "source": "fixture", "url": f"https://example.invalid/{symbol.lower()}/{i}",
         "symbols": [symbol], "created_at": t, "updated_at": t}
        for i, (t, h, s) in enumerate(items, 1)
    ], "next_page_token": None}


def submissions(symbol: str, cik: str, name: str, rows: list[tuple[str, str, str, str]]) -> dict:
    """rows: (acceptance UTC, form, items, document)."""
    return {
        "cik": cik, "name": f"{name} (fixture)", "tickers": [symbol],
        "filings": {"recent": {
            "accessionNumber": [f"0009999999-26-{900000 + i:06d}" for i in range(len(rows))],
            "filingDate": [r[0][:10] for r in rows],
            "acceptanceDateTime": [r[0] for r in rows],
            "form": [r[1] for r in rows],
            "items": [r[2] for r in rows],
            "primaryDocument": [r[3] for r in rows],
        }, "files": []},
    }


def company_facts(cik: int, name: str, scale: float) -> dict:
    facts = copy.deepcopy(json.loads((FIXTURES / "sec_companyfacts_tsla.json").read_text(
        encoding="utf-8")))
    facts["cik"], facts["entityName"] = cik, f"{name} (fixture)"
    for concepts in facts["facts"].values():
        for concept in concepts.values():
            for unit, rows in concept.get("units", {}).items():
                if unit == "USD":
                    for r in rows:
                        r["val"] = int(r["val"] * scale)
    return facts


def write(folder: str, name: str, obj: dict) -> None:
    path = HERE / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")


def build() -> None:
    # ---- clean-buy: BRKO, orderly push off a VWAP base, room below the opening spike high
    b = bars(80.0)
    write("clean-buy", "bars.json", b)
    write("clean-buy", "snapshot.json", snapshot("BRKO", b, prev_close=78.20))
    write("clean-buy", "news.json", news("BRKO", [
        ("2026-11-03T15:20:00Z", "Analyst upgrade: broker raises Brookfield Optics to buy",
         "The broker cited order growth and a cleaner balance sheet."),
        ("2026-11-03T13:10:00Z", "Brookfield Optics wins multi-year supply contract",
         "The company signed a five-year optical module supply agreement."),
        ("2026-11-02T21:30:00Z", "Brookfield Optics raises full-year guidance",
         "Management lifted its revenue outlook on stronger bookings."),
    ]))
    write("clean-buy", "submissions.json", submissions("BRKO", "9900001", "Brookfield Optics", [
        ("2026-11-02T21:05:00.000Z", "8-K", "1.01,9.01", "brko-8k_20261102.htm"),
        ("2026-10-28T20:05:00.000Z", "10-Q", "", "brko-20260930.htm"),
    ]))
    write("clean-buy", "companyfacts.json", company_facts(9900001, "Brookfield Optics", 0.05))

    # ---- red-flag: VNTR, the same chart shifted, a five-minute halt in the bars, a halt
    # headline and an earnings release scheduled inside the trade window
    b = bars(92.0, gap=range(35, 40))  # 10:05-10:09 ET: halted, no bars
    write("red-flag", "bars.json", b)
    write("red-flag", "snapshot.json", snapshot("VNTR", b, prev_close=90.10))
    write("red-flag", "news.json", news("VNTR", [
        ("2026-11-03T15:12:00Z", "Ventara shares halted for volatility, trading resumes",
         "Trading was halted for five minutes after a sharp move, then resumed."),
        ("2026-11-03T13:00:00Z",
         "Ventara to report third-quarter earnings at 1:00 p.m. ET today",
         "The company will release results and host a call during market hours."),
        ("2026-11-02T22:00:00Z", "Ventara shares rise on sector strength",
         "Peers rallied after an industry trade group report."),
    ]))
    write("red-flag", "submissions.json", submissions("VNTR", "9900002", "Ventara", [
        ("2026-11-02T21:40:00.000Z", "8-K", "8.01", "vntr-8k_20261102.htm"),
        ("2026-10-27T20:05:00.000Z", "10-Q", "", "vntr-20260930.htm"),
    ]))
    write("red-flag", "companyfacts.json", company_facts(9900002, "Ventara", 0.08))


if __name__ == "__main__":
    build()
