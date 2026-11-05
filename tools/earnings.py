"""Earnings facts from SEC XBRL companyfacts, point-in-time (no look-ahead).

Network code is limited to fetch_company_facts (injectable opener). Everything
else takes already-fetched data so it can be tested offline. No LLM, no
estimates: only values that companies actually filed on or before `as_of`.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from tools.http import get_json
from tools.news import SEC_LIMITER, RateLimiter, _norm_cik, _sec_headers

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ALLOWED_FORMS = frozenset({"10-Q", "10-K", "10-Q/A", "10-K/A"})
MIN_QUARTER_DAYS = 80
MAX_QUARTER_DAYS = 100
YOY_MIN_DAYS = 350
YOY_MAX_DAYS = 380

# metric name -> (taxonomy, candidate concepts in priority order, unit)
DURATION_CONCEPTS: dict[str, tuple[tuple[str, ...], str]] = {
    "revenue": (
        ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
        "USD",
    ),
    "net_income": (("NetIncomeLoss",), "USD"),
    "eps_diluted": (("EarningsPerShareDiluted",), "USD/shares"),
}
CASH_CONCEPTS = ("CashAndCashEquivalentsAtCarryingValue",)
SHARES_CONCEPTS = ("EntityCommonStockSharesOutstanding",)
_PCT = Decimal("0.01")
RECENT_FILING_DAYS = 5


def fetch_company_facts(
    cik: str, opener: Callable | None = None, *, limiter: RateLimiter | None = None
) -> dict:
    """Fetch the SEC XBRL companyfacts JSON for `cik` (padded to 10 digits).

    Uses the same SEC_USER_AGENT rules and rate limiter as news.fetch_submissions.
    RuntimeError on a bad response; ValueError on an invalid CIK.
    """
    padded = _norm_cik(cik)
    headers = _sec_headers()
    (limiter or SEC_LIMITER).wait()
    body = get_json(
        COMPANYFACTS_URL.format(cik=padded), headers, opener=opener, expect_keys=["facts"]
    )
    if not isinstance(body, dict):
        raise RuntimeError("unexpected companyfacts response shape")
    return body


def cik_for_symbol(symbol: str, cik_map: dict[str, str]) -> str | None:
    """Look up `symbol` (stripped, upper-cased) in a map from news.fetch_cik_map.

    Returns the 10-digit CIK string, or None when the symbol is unknown or the
    inputs are not of the expected types.
    """
    if not isinstance(symbol, str) or not isinstance(cik_map, dict):
        return None
    value = cik_map.get(symbol.strip().upper())
    return value if isinstance(value, str) else None


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _dec(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _entries(facts: dict, taxonomy: str, concept: str, unit: str) -> list | None:
    tax = facts.get(taxonomy)
    node = tax.get(concept) if isinstance(tax, dict) else None
    units = node.get("units") if isinstance(node, dict) else None
    rows = units.get(unit) if isinstance(units, dict) else None
    return rows if isinstance(rows, list) and rows else None


def _parse(entry: object, as_of: date, *, duration: bool, stats: dict) -> dict | None:
    """Validate one entry; return a parsed dict or None (counted in `stats`)."""
    if not isinstance(entry, dict):
        stats["malformed"] += 1
        return None
    end, filed, val = _date(entry.get("end")), _date(entry.get("filed")), _dec(entry.get("val"))
    form = entry.get("form")
    start = _date(entry.get("start")) if entry.get("start") is not None else None
    if (
        end is None or filed is None or val is None or not isinstance(form, str)
        or (duration and start is None)
        or (entry.get("start") is not None and start is None)
    ):
        stats["malformed"] += 1
        return None
    if form not in ALLOWED_FORMS or filed > as_of:
        return None  # not an error: wrong form, or not yet public at as_of
    if duration:
        days = (end - start).days  # type: ignore[operator]
        if not MIN_QUARTER_DAYS <= days <= MAX_QUARTER_DAYS:
            stats["non_quarterly"] += 1
            return None
    elif start is not None:
        return None  # a duration value is not an instant
    fy, fp = entry.get("fy"), entry.get("fp")
    return {
        "start": start, "end": end, "filed": filed, "val": val, "form": form,
        "fy": fy if isinstance(fy, int) and not isinstance(fy, bool) else None,
        "fp": fp if isinstance(fp, str) else None,
    }


def _group(entries: list[dict], key_of) -> dict:
    """Collapse parsed entries sharing a period key into one record per period.

    The VALUE comes from the latest filing (a restatement known by as_of wins);
    fy/fp/form come from the EARLIEST filing, because later filings re-report
    the same period as a prior-year comparative with that filing's own fy/fp.
    """
    groups: dict = {}
    for e in entries:
        groups.setdefault(key_of(e), []).append(e)
    out = {}
    for key, es in groups.items():
        first = min(es, key=lambda e: (e["filed"], e["form"], e["fy"] or 0, e["fp"] or ""))
        last = max(es, key=lambda e: (e["filed"], e["val"]))
        out[key] = {
            "start": first["start"], "end": first["end"], "val": last["val"],
            "fy": first["fy"], "fp": first["fp"], "form": first["form"],
            "first_filed": first["filed"], "last_filed": last["filed"],
            "restated": last["form"].endswith("/A") or (
                last["filed"] > first["filed"] and last["val"] != first["val"]
            ),
        }
    return out


def _pick_concept(candidates: list[tuple[str, dict]]) -> tuple[str | None, dict]:
    """Choose the concept whose newest period end is latest (ties: priority order)."""
    best_name: str | None = None
    best: dict = {}
    best_end: date | None = None
    for name, series in candidates:
        if not series:
            continue
        newest = max(rec["end"] for rec in series.values())
        if best_end is None or newest > best_end:
            best_name, best, best_end = name, series, newest
    return best_name, best


def _duration_series(
    facts: dict, metric: str, as_of: date, stats: dict
) -> tuple[str | None, dict]:
    """(concept, {(start, end): record}) for one metric.

    Uses ONLY the concept whose newest qualifying quarter (filed <= as_of) is
    latest, so a stale concept cannot hide current data; concepts are never
    mixed across periods.
    """
    concepts, unit = DURATION_CONCEPTS[metric]
    candidates = []
    for concept in concepts:
        rows = _entries(facts, "us-gaap", concept, unit)
        if rows is None:
            continue
        parsed = [
            e for e in (_parse(r, as_of, duration=True, stats=stats) for r in rows) if e
        ]
        candidates.append((concept, _group(parsed, lambda e: (e["start"], e["end"]))))
    return _pick_concept(candidates)


def _latest_instant(
    facts: dict, taxonomy: str, concepts: tuple[str, ...], unit: str, as_of: date, stats: dict
) -> tuple[str | None, dict | None]:
    """(concept, latest instant record) using the concept with the newest end."""
    candidates = []
    for concept in concepts:
        rows = _entries(facts, taxonomy, concept, unit)
        if rows is None:
            continue
        parsed = [
            e for e in (_parse(r, as_of, duration=False, stats=stats) for r in rows) if e
        ]
        candidates.append((concept, _group(parsed, lambda e: e["end"])))
    name, series = _pick_concept(candidates)
    if not series:
        return None, None
    rec = series[max(series)]
    return name, {
        "value": str(rec["val"]),
        "end": rec["end"].isoformat(),
        "form": rec["form"],
        "first_filed": rec["first_filed"].isoformat(),
        "last_filed": rec["last_filed"].isoformat(),
    }


def _growth_pct(new: Decimal | None, old: Decimal | None) -> str | None:
    """((new - old) / |old|) * 100 quantized to 0.01 (half-even); None if undefined.

    Raises ArithmeticError (decimal.InvalidOperation) for extreme ratios; the
    caller catches it.
    """
    if new is None or old is None or old == 0:
        return None
    pct = (new - old) / abs(old) * 100
    return str(pct.quantize(_PCT, rounding=ROUND_HALF_EVEN))


def _in_window(first_filed: date, as_of: date) -> bool:
    return 0 <= (as_of - first_filed).days <= RECENT_FILING_DAYS


def earnings_facts(companyfacts: dict, as_of: date, quarters: int = 4) -> dict:
    """Recent quarterly results from SEC companyfacts, using only facts filed <= as_of.

    Returns {"ok": True, "entity", "cik", "as_of", "quarters": [...newest first, up
    to `quarters`; each has end, start, fy, fp, form, first_filed, last_filed,
    revenue, net_income, eps_diluted], "latest_instant": {"cash",
    "shares_outstanding"} (each {value, end, form, first_filed, last_filed} or
    None), "growth": {"revenue_yoy_pct", "net_income_yoy_pct", "eps_yoy_pct"},
    "concepts": {revenue, net_income, eps_diluted, cash, shares_outstanding ->
    XBRL concept used or None}, "catalysts", "latest_filed", "notes"}, or
    {"ok": False, "reason": ...}. Money and EPS are Decimal strings.

    Quarterly values are 10-Q/10-K (and /A) duration facts of 80-100 days. For
    each metric only ONE concept is used: the one whose newest qualifying
    quarter is latest (ties go to the listed priority order). For a given
    (start, end) the VALUE comes from the latest filing <= as_of (restatements
    win), while fy/fp/form come from the EARLIEST filing (later filings repeat
    the period as a comparative with their own fy/fp). first_filed is the
    earliest filing date for the period (across metrics); last_filed is the
    latest filing date supplying a value. Fourth quarters reported only as
    full-year figures are skipped, not derived. Growth compares the newest
    quarter with the one ending 350-380 days earlier. `latest_filed` is the max
    first_filed over returned quarters and instants (newest new information).

    `catalysts` is a sorted list drawn only from:
      "recent_10q"  newest quarter's first filing is a 10-Q or 10-Q/A filed 0-5
                    calendar days before or on as_of;
      "recent_10k"  likewise for a 10-K or 10-K/A (the newest quarter or a
                    cash/shares instant);
      "restatement" any returned quarter's value comes from a /A filing, or from
                    a later filing with a changed value.
    Malformed entries are skipped and counted in notes; this function does not raise.
    """
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        return {"ok": False, "reason": "as_of must be a datetime.date (not a datetime)"}
    if isinstance(quarters, bool) or not isinstance(quarters, int) or quarters < 1:
        return {"ok": False, "reason": "quarters must be an integer >= 1"}
    if not isinstance(companyfacts, dict) or not isinstance(companyfacts.get("facts"), dict):
        return {"ok": False, "reason": "companyfacts has no 'facts' object"}
    facts = companyfacts["facts"]
    stats = {"malformed": 0, "non_quarterly": 0}
    notes: list[str] = []

    picked = {m: _duration_series(facts, m, as_of, stats) for m in DURATION_CONCEPTS}
    series = {m: v[1] for m, v in picked.items()}
    periods = sorted(
        {k for s in series.values() for k in s},
        key=lambda k: (k[1], max(s[k]["last_filed"] for s in series.values() if k in s), k[0]),
        reverse=True,
    )
    if not periods:
        return {"ok": False, "reason": f"no quarterly data filed on or before {as_of.isoformat()}"}

    restated_any = False
    first_info: list[tuple[date, str]] = []  # (first_filed, form) per returned row

    def row_for(key: tuple[date, date]) -> dict:
        nonlocal restated_any
        present = [s[key] for s in series.values() if key in s]
        meta = min(present, key=lambda e: (e["first_filed"], e["form"]))
        restated_any = restated_any or any(e["restated"] for e in present)
        first_info.append((meta["first_filed"], meta["form"]))
        row = {
            "end": key[1].isoformat(), "start": key[0].isoformat(),
            "fy": meta["fy"], "fp": meta["fp"], "form": meta["form"],
            "first_filed": meta["first_filed"].isoformat(),
            "last_filed": max(e["last_filed"] for e in present).isoformat(),
        }
        for m, s in series.items():
            row[m] = str(s[key]["val"]) if key in s else None
        return row

    rows = [row_for(k) for k in periods[:quarters]]

    # Growth: newest quarter vs the (newest) quarter ending 350-380 days earlier.
    newest = periods[0]
    prior_candidates = [
        k for k in periods if YOY_MIN_DAYS <= (newest[1] - k[1]).days <= YOY_MAX_DAYS
    ]
    prior = prior_candidates[0] if prior_candidates else None  # periods is newest first
    growth: dict[str, str | None] = {}
    for m in DURATION_CONCEPTS:
        new_v = series[m][newest]["val"] if newest in series[m] else None
        old_v = series[m][prior]["val"] if prior and prior in series[m] else None
        gname = f"{m.removesuffix('_diluted')}_yoy_pct"
        try:
            growth[gname] = _growth_pct(new_v, old_v)
        except ArithmeticError:
            growth[gname] = None
            notes.append(f"{gname} not computable (extreme ratio)")
    if prior is None:
        notes.append("no quarter ending 350-380 days before the newest quarter; growth unavailable")

    cash_concept, cash = _latest_instant(facts, "us-gaap", CASH_CONCEPTS, "USD", as_of, stats)
    shares_concept, shares = _latest_instant(facts, "dei", SHARES_CONCEPTS, "shares", as_of, stats)
    for m, s in series.items():
        if not s:
            notes.append(f"missing concept: {m}")
    if cash is None:
        notes.append("missing concept: cash")
    if shares is None:
        notes.append("missing concept: shares_outstanding")
    if stats["non_quarterly"]:
        notes.append(
            f"skipped {stats['non_quarterly']} non-quarterly duration entries "
            "(full-year/year-to-date; Q4 not derived by subtraction)"
        )
    if stats["malformed"]:
        notes.append(f"skipped {stats['malformed']} malformed entries")

    new_first, new_form = first_info[0]  # rows[0] is the newest quarter
    catalysts: set[str] = set()
    if _in_window(new_first, as_of):
        if new_form in ("10-Q", "10-Q/A"):
            catalysts.add("recent_10q")
        elif new_form in ("10-K", "10-K/A"):
            catalysts.add("recent_10k")
    for inst in (cash, shares):
        if inst and inst["form"] in ("10-K", "10-K/A") and _in_window(
            date.fromisoformat(inst["first_filed"]), as_of
        ):
            catalysts.add("recent_10k")
    if restated_any:
        catalysts.add("restatement")

    first_dates = [r["first_filed"] for r in rows] + [
        i["first_filed"] for i in (cash, shares) if i
    ]
    try:
        cik: str | None = _norm_cik(companyfacts.get("cik"))
    except ValueError:
        cik = None
        notes.append("companyfacts has no valid cik")
    name = companyfacts.get("entityName")
    return {
        "ok": True,
        "entity": name if isinstance(name, str) else None,
        "cik": cik,
        "as_of": as_of.isoformat(),
        "quarters": rows,
        "latest_instant": {"cash": cash, "shares_outstanding": shares},
        "growth": growth,
        "concepts": {
            "revenue": picked["revenue"][0],
            "net_income": picked["net_income"][0],
            "eps_diluted": picked["eps_diluted"][0],
            "cash": cash_concept,
            "shares_outstanding": shares_concept,
        },
        "catalysts": sorted(catalysts),
        "latest_filed": max(first_dates),
        "notes": notes,
    }
