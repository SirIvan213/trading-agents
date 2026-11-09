"""Regenerate the synthetic scenario fixtures (clean-buy, red-flag, extended-chase). Offline,
deterministic.

    .venv/Scripts/python tests/fixtures/scenarios/build.py

Each scenario folder gets bars.json, snapshot.json, news.json, submissions.json and
companyfacts.json in the real API shapes, for a run "as of" 2026-11-03 11:00 ET (EST, UTC-5).
scenario.json (symbol, files, expected outcome) is hand-written and not touched here.
The company facts reuse the TSLA fixture's quarterly structure with scaled dollar values.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
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


# The shared chart (prices relative to a base). Minute 0 = 09:30 ET, minute 89 = 10:59 ET.
# A steady trend up from the open, a pullback that holds VWAP, then a push back up to close
# near the session high. No spike-and-fade.
SHAPE = [(0, 0.00), (14, 0.20), (40, 0.55), (50, 0.62), (64, 0.43), (70, 0.46), (89, 0.72)]
WOBBLE = [0.00, 0.03, -0.02, 0.02, -0.03, 0.02, -0.01, 0.01]
# extended-chase: the same chart until the pullback ends (minute 70), then a vertical rip from
# minute 74 that closes at the high about 4 five-minute ATRs above VWAP (threshold 2.0).
CHASE_SHAPE = [p for p in SHAPE if p[0] < 74] + [(74, 0.50), (89, 1.40)]


def _volume_and_wick(m: int) -> tuple[int, float]:
    if m < 15:
        return 30000 - 600 * m, 0.06
    if m < 50:
        return 16000 + 60 * (m - 15), 0.05
    if m < 68:
        return 11000, 0.04  # the pullback comes on lighter volume
    return 15000 + 400 * (m - 68), 0.05


def bars(base: float, *, gap: range | None = None,
         shape: list[tuple[int, float]] | None = None) -> dict:
    closes = _path(SHAPE if shape is None else shape)
    out = []
    # three pre-market bars (dropped by the regular-session filter)
    for i in range(3):
        ts = UTC_OPEN - timedelta(minutes=3 - i)
        out.append(_bar(ts, base - 0.10, base - 0.08, 0.03, 1500 + 200 * i))
    prev = base - 0.05
    for m, rel in enumerate(closes):
        if gap is not None and m in gap:
            continue  # trading halted: no bars
        c = base + rel + (WOBBLE[m % len(WOBBLE)] if 3 <= m < 86 else 0.0)
        v, wick = _volume_and_wick(m)
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


def company_facts(cik: int, name: str, scale: float, *, filed_before: str | None = None) -> dict:
    """The TSLA facts with dollar AND share values scaled (so EPS stays consistent). With
    `filed_before`, every fact filed on or after that date is removed (a quarter not yet
    reported)."""
    facts = copy.deepcopy(json.loads((FIXTURES / "sec_companyfacts_tsla.json").read_text(
        encoding="utf-8")))
    facts["cik"], facts["entityName"] = cik, f"{name} (fixture)"
    for concepts in facts["facts"].values():
        for concept in concepts.values():
            for unit, rows in concept.get("units", {}).items():
                if filed_before is not None:
                    rows[:] = [r for r in rows if r.get("filed", "") < filed_before]
                if unit in ("USD", "shares"):
                    for r in rows:
                        r["val"] = int(r["val"] * scale)
    return facts


def fmp_row(symbol: str, day: str, time: str | None, confirmed: bool, period: str = "Q3",
            fiscal_year: int = 2026, period_ending: str = "2026-09-30") -> dict:
    """One FMP /stable/earnings-calendar row as returned with includeReportTimes=true (shape
    from a contract fixture recorded against the live API; see tools/earnings_calendar.py)."""
    return {"symbol": symbol, "date": day, "epsActual": None, "epsEstimated": 1.12,
            "revenueActual": None, "revenueEstimated": 412000000, "time": time,
            "periodEnding": period_ending, "fiscalPeriod": period, "fiscalYear": fiscal_year,
            "confirmed": confirmed, "lastUpdated": "2026-11-02"}


# Other companies reporting in the fixture window [2026-11-03, 2026-11-17]. None of the
# scenario symbols appears unless the scenario has an earnings event.
OTHER_REPORTS = [
    fmp_row("AXLE", "2026-11-03", "bmo", True),
    fmp_row("CORV", "2026-11-03", "amc", True),
    fmp_row("DUNE", "2026-11-04", None, False),
    fmp_row("EMBR", "2026-11-05", "amc", True),
    fmp_row("FJRD", "2026-11-10", "bmo", False),
    fmp_row("GLYF", "2026-11-12", "amc", True),
]


AV_HEADER = "symbol,name,reportDate,fiscalDateEnding,estimate,currency"
AV_FIRST, AV_LAST = date(2026, 11, 3), date(2027, 1, 29)  # a 3-month reply from the run day
AV_PER_WEEKDAY = 12  # 5 weekdays x 12 = 60 reports in [2026-11-03, 2026-11-10]


def av_calendar(extra: list[tuple[str, str, str]] = ()) -> str:
    """A SYNTHETIC Alpha Vantage EARNINGS_CALENDAR CSV for a run on 2026-11-03 (#167): the
    column set a third-party guide shows (Alpha Vantage does not document it), other
    companies reporting AV_PER_WEEKDAY times every weekday through 2027-01-29, plus `extra`
    (symbol, name, reportDate). None of the background symbols is a scenario symbol. Never
    a copy of real data."""
    lines = [AV_HEADER]
    for sym, name, day in extra:
        lines.append(f'{sym},"{name}",{day},2026-09-30,1.12,USD')
    day, n = AV_FIRST, 0
    while day <= AV_LAST:
        if day.weekday() < 5:
            for _ in range(AV_PER_WEEKDAY):
                n += 1
                lines.append(f"QAV{n:04d},Background Co {n},{day.isoformat()},2026-09-30,,USD")
        day += timedelta(days=1)
    return "\n".join(lines) + "\n"


def halt_item(symbol: str, name: str, market: str, day: str, time: str, code: str,
              resumed: tuple[str, str, str] = ("", "", "")) -> str:
    """One <item> of the Nasdaq Trader trade-halt RSS feed (SYNTHETIC data in the feed's
    documented shape; never a copy of the real feed: its terms restrict storing copies)."""
    rdate, rquote, rtrade = resumed
    return (
        f"<item><title>{symbol}</title><pubDate>Tue, 03 Nov 2026 05:00:00 GMT</pubDate>"
        f"<ndaq:HaltDate>{day}</ndaq:HaltDate><ndaq:HaltTime>{time}</ndaq:HaltTime>"
        f"<ndaq:IssueSymbol>{symbol}</ndaq:IssueSymbol><ndaq:IssueName>{name}</ndaq:IssueName>"
        f"<ndaq:Market>{market}</ndaq:Market><ndaq:ReasonCode>{code}</ndaq:ReasonCode>"
        f"<ndaq:PauseThresholdPrice />"
        + (f"<ndaq:ResumptionDate>{rdate}</ndaq:ResumptionDate>" if rdate
           else "<ndaq:ResumptionDate />")
        + (f"<ndaq:ResumptionQuoteTime>{rquote}</ndaq:ResumptionQuoteTime>" if rquote
           else "<ndaq:ResumptionQuoteTime />")
        + (f"<ndaq:ResumptionTradeTime>{rtrade}</ndaq:ResumptionTradeTime>" if rtrade
           else "<ndaq:ResumptionTradeTime />")
        + "<description><![CDATA[synthetic fixture]]></description></item>")


# Other issues' halts in every scenario's feed: a long-standing SEC suspension, a news halt
# yesterday that resumed, and a LULD pause today in another symbol.
OTHER_HALTS = [
    halt_item("QZXA", "Quazex Holdings", "NASDAQ", "08/14/2025", "09:30:00", "H10"),
    halt_item("WYLD", "Wyldcrest Inc.", "NYSE", "11/02/2026", "14:02:11.512", "T1",
              ("11/02/2026", "14:55:00", "15:00:00")),
    halt_item("PLMX", "Palmex Corp.", "NASDAQ", "11/03/2026", "10:21:04.118", "LUDP",
              ("11/03/2026", "10:26:04", "10:26:04")),
]


def halt_feed(items: list[str], pub: str = "Tue, 03 Nov 2026 15:59:00 GMT") -> str:
    """The trade-halt RSS document (synthetic). pubDate 10:59 ET, a minute before the
    scenarios' 11:00 run."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>'
        "<title>NASDAQTrader.com</title><link>http://www.nasdaqtrader.com</link>"
        "<description>NASDAQ Trade Halts</description>"
        "<copyright>Copyright 2026. All rights reserved.</copyright>"
        f"<pubDate>{pub}</pubDate><ttl>1</ttl><ndaq:numItems>{len(items)}</ndaq:numItems>"
        + "".join(items) + "</channel></rss>\n")


def write_text(folder: str, name: str, text: str) -> None:
    path = HERE / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write(folder: str, name: str, obj: dict | list) -> None:
    path = HERE / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")


def build() -> None:
    # ---- clean-buy: BRKO, steady trend up, a pullback that holds VWAP, close near the high
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
        ("2026-10-29T20:05:00.000Z", "10-Q", "", "brko-20260930.htm"),
        # Q3 results (item 2.02): proves BRKO reports quarterly, so the calendar's silence
        # about BRKO means "no earnings in the window" (#112)
        ("2026-10-29T20:01:00.000Z", "8-K", "2.02,9.01", "brko-8k_20261029.htm"),
    ]))
    write("clean-buy", "earnings_calendar.json", OTHER_REPORTS)
    write_text("clean-buy", "av_earnings_calendar.csv", av_calendar())
    write_text("clean-buy", "halts.xml", halt_feed(OTHER_HALTS))
    write("clean-buy", "companyfacts.json", company_facts(9900001, "Brookfield Optics", 0.05))

    # ---- red-flag: VNTR, the same chart shifted, a five-minute halt in the bars, a halt
    # headline and third-quarter earnings scheduled inside the trade window. Q3 is NOT yet
    # reported: no Q3 10-Q, and the company facts stop at the quarter ended 2026-06-30.
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
        ("2026-07-23T20:05:00.000Z", "10-Q", "", "vntr-20260630.htm"),
        ("2026-07-23T20:01:00.000Z", "8-K", "2.02,9.01", "vntr-8k_20260723.htm"),
    ]))
    write("red-flag", "earnings_calendar.json",
          [fmp_row("VNTR", "2026-11-03", None, True), *OTHER_REPORTS])
    write_text("red-flag", "av_earnings_calendar.csv",
               av_calendar([("VNTR", "Ventara Inc.", "2026-11-03")]))
    # The 10:05-10:09 gap in the bars: a LULD volatility pause, resumed at 10:10
    write_text("red-flag", "halts.xml", halt_feed([
        halt_item("VNTR", "Ventara Inc.", "NASDAQ", "11/03/2026", "10:05:00.214", "LUDP",
                  ("11/03/2026", "10:10:00", "10:10:00")),
        *OTHER_HALTS]))
    write("red-flag", "companyfacts.json",
          company_facts(9900002, "Ventara", 0.08, filed_before="2026-10-01"))

    # ---- extended-chase: CHSW, the clean-buy chart and news, but the last 15 minutes rip
    # straight up to close at the high far above VWAP. Every market entry is extended.
    b = bars(80.0, shape=CHASE_SHAPE)
    write("extended-chase", "bars.json", b)
    write("extended-chase", "snapshot.json", snapshot("CHSW", b, prev_close=78.20))
    write("extended-chase", "news.json", news("CHSW", [
        ("2026-11-03T15:20:00Z", "Analyst upgrade: broker raises Chasewater Labs to buy",
         "The broker cited order growth and a cleaner balance sheet."),
        ("2026-11-03T13:10:00Z", "Chasewater Labs wins multi-year supply contract",
         "The company signed a five-year optical module supply agreement."),
        ("2026-11-02T21:30:00Z", "Chasewater Labs raises full-year guidance",
         "Management lifted its revenue outlook on stronger bookings."),
    ]))
    write("extended-chase", "submissions.json", submissions(
        "CHSW", "9900003", "Chasewater Labs", [
            ("2026-11-02T21:05:00.000Z", "8-K", "1.01,9.01", "chsw-8k_20261102.htm"),
            ("2026-10-29T20:05:00.000Z", "10-Q", "", "chsw-20260930.htm"),
            ("2026-10-29T20:01:00.000Z", "8-K", "2.02,9.01", "chsw-8k_20261029.htm"),
        ]))
    write("extended-chase", "earnings_calendar.json", OTHER_REPORTS)
    write_text("extended-chase", "av_earnings_calendar.csv", av_calendar())
    write_text("extended-chase", "halts.xml", halt_feed(OTHER_HALTS))
    (FIXTURES / "nasdaq_halts_tsla.xml").write_text(halt_feed(OTHER_HALTS), encoding="utf-8")
    # The TSLA baseline's calendar for the same window (TSLA reported on 2026-10-22).
    (FIXTURES / "fmp_earnings_calendar_tsla.json").write_text(
        json.dumps(OTHER_REPORTS, indent=1) + "\n", encoding="utf-8")
    (FIXTURES / "av_earnings_calendar_tsla.csv").write_text(av_calendar(), encoding="utf-8")
    write("extended-chase", "companyfacts.json",
          company_facts(9900003, "Chasewater Labs", 0.05))


if __name__ == "__main__":
    build()
