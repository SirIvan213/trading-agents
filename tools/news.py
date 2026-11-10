"""News: headlines (Alpaca), 8-K filings (SEC EDGAR) and a deterministic digest.

All logic takes already-fetched data so it can be tested offline. Network code
is limited to the fetch_* functions, each with an injectable opener. No LLM and
no sentiment scores anywhere in this module.
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tools.http import get_json
from tools.scan import validate_symbols

NY = ZoneInfo("America/New_York")
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
MAX_NEWS_PAGES = 20
MAX_NEWS_PAGE_SIZE = 50
SEC_MAX_CALLS_PER_SECOND = 10  # SEC fair-access ceiling
SEC_LIMITER_CALLS = 9  # module limiter runs one under the ceiling for margin
SEC_PLACEHOLDER = "you@example.com"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

# 8-K item code -> catalyst name.
ITEM_CATALYSTS: dict[str, str] = {
    "1.01": "material_agreement",
    "1.03": "bankruptcy",
    "1.05": "cybersecurity_incident",
    "2.01": "acquisition_or_disposition",
    "2.02": "earnings",
    "2.06": "impairment",
    "3.01": "delisting_notice",
    "3.02": "unregistered_equity_sale",  # dilution risk
    "4.01": "auditor_change",
    "4.02": "non_reliance_restatement",
    "5.02": "officer_change",
    "7.01": "reg_fd",
    "8.01": "other_event",
}
# 8-K items that are never catalysts. 9.01 "Financial Statements and Exhibits" only lists
# what is attached to the filing (it rides along with 2.02, 8.01 and the rest), so on its
# own it says nothing about the company (issue #87). It stays in `items`.
NON_CATALYST_ITEMS = frozenset({"9.01"})
# Catalysts that are red flags when they come from a filing.
RED_FLAG_CATALYSTS = frozenset({"bankruptcy", "delisting_notice", "non_reliance_restatement"})

# Headline keyword table: flag -> explicit word forms (inflections are listed, not
# stemmed). Matching is case-insensitive and whole-word (a form must not be glued to
# other letters/digits); spaces in a phrase match any run of whitespace.
# Notes: bare "approval" is deliberately NOT a keyword (it matches "shareholder
# approval", "board approval" and so on); FDA news is caught by "FDA" and "CRL".
# Known limitation: "ATM" also matches an ATM machine; flags are hints for a human
# or agent, not verdicts.
# The same holds for "halt": every form of the word counts as a halt red flag, including
# "halting", so "ABCD halting production" is flagged "halt" even though it is not a trading
# halt (#26). That is deliberate: over-flagging only blocks or flags a symbol, while missing
# a real halt could let an order through. The event gate treats a flagged halt headline
# dated today as an EVENT (tools/event_gate.py).
HEADLINE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "offering": ("offering", "offerings", "public offering", "public offerings",
                 "registered direct", "ATM"),
    # LULD volatility interruptions are officially "trading pauses" (#112)
    "halt": ("halt", "halts", "halted", "halting", "trading paused", "paused trading",
             "trading pause", "trading pauses", "LULD", "trading suspended", "suspends trading",
             "suspended trading"),
    "fda": ("FDA", "CRL"),
    "guidance": ("guidance", "outlook"),
    "analyst": ("downgrade", "downgrades", "downgraded", "downgrading",
                "upgrade", "upgrades", "upgraded", "upgrading"),
    "earnings": ("earnings", "EPS", "quarterly results"),
    "m_and_a": ("merger", "mergers", "acquire", "acquires", "acquired", "acquiring",
                "acquisition", "acquisitions", "buyout", "buyouts"),
    "bankruptcy": ("bankruptcy", "chapter 11"),
    "delisting": ("delist", "delists", "delisted", "delisting"),
    "legal": ("investigation", "investigations", "SEC probe", "lawsuit", "lawsuits"),
}
# Headline flags that count as red flags in digest().
HEADLINE_RED_FLAGS = frozenset({"bankruptcy", "delisting", "halt"})

_KEYWORD_RES: dict[str, re.Pattern[str]] = {
    flag: re.compile(
        r"(?<![A-Za-z0-9])(?:"
        + "|".join(r"\s+".join(re.escape(w) for w in p.split()) for p in phrases)
        + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for flag, phrases in HEADLINE_KEYWORDS.items()
}


def _utc_rfc3339(dt: datetime) -> str:
    if not isinstance(dt, datetime) or dt.tzinfo is None:
        raise ValueError("start/end must be timezone-aware datetimes")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _alpaca_headers() -> dict:
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the environment")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}


def fetch_news(
    symbols: list[str],
    start: datetime,
    end: datetime,
    limit: int = 50,
    opener: Callable | None = None,
) -> dict:
    """Fetch Alpaca news for `symbols` between `start` and `end` (newest first).

    `limit` is the page size (1..50); next_page_token is followed up to
    MAX_NEWS_PAGES pages, then RuntimeError. Returns {"news": [...]}; each item
    has id, headline, summary, author, source, url, symbols, created_at,
    updated_at (RFC3339 UTC). Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY.
    """
    headers = _alpaca_headers()
    syms = validate_symbols(symbols)
    if not syms:
        raise ValueError("at least one symbol is required")
    ok_limit = isinstance(limit, int) and not isinstance(limit, bool)
    if not ok_limit or not 1 <= limit <= MAX_NEWS_PAGE_SIZE:
        raise ValueError(f"limit must be an integer 1..{MAX_NEWS_PAGE_SIZE}")
    start_s, end_s = _utc_rfc3339(start), _utc_rfc3339(end)
    if start_s > end_s:  # fixed-width UTC strings sort chronologically
        raise ValueError("start must not be after end")
    base = {
        "symbols": ",".join(syms),
        "start": start_s,
        "end": end_s,
        "limit": str(limit),
        "sort": "desc",
    }
    merged: list = []
    token: str | None = None
    for _ in range(MAX_NEWS_PAGES):
        params = dict(base)
        if token:
            params["page_token"] = token
        url = f"{NEWS_URL}?{urllib.parse.urlencode(params, safe=',:')}"
        page = get_json(url, headers, opener=opener, expect_keys=["news"])
        if not isinstance(page, dict):
            raise RuntimeError("unexpected news response shape")
        # A missing or null "news" is an error, never "no news": read as empty it would also
        # read as "no halts" (#112).
        items = page.get("news")
        if not isinstance(items, list):
            raise RuntimeError("unexpected news response shape")
        merged.extend(items)
        token = page.get("next_page_token")
        if not token:
            break
    else:
        raise RuntimeError(f"news pagination exceeded {MAX_NEWS_PAGES} pages")
    return {"news": merged}


# --------------------------------------------------------------------------
# SEC EDGAR
# --------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds.

    `clock` and `sleep` are injectable so tests never really sleep.
    """

    def __init__(
        self,
        max_calls: int = SEC_MAX_CALLS_PER_SECOND,
        period: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
            raise ValueError("max_calls must be an integer >= 1")
        if not period > 0:
            raise ValueError("period must be > 0")
        self.max_calls = max_calls
        self.period = period
        self.clock = clock
        self.sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Block (via `sleep`) until another call is allowed, then record it."""
        with self._lock:
            while True:
                now = self.clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    break
                self.sleep(max(self.period - (now - self._calls[0]), 0.0))
            self._calls.append(now)


SEC_LIMITER = RateLimiter(max_calls=SEC_LIMITER_CALLS)


def _sec_headers() -> dict:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua or not ua.strip():
        raise RuntimeError("SEC_USER_AGENT must be set in the environment")
    if SEC_PLACEHOLDER in ua.lower():
        raise RuntimeError("SEC_USER_AGENT still holds the .env.example placeholder")
    return {"User-Agent": ua.strip(), "Accept": "application/json"}


def _sec_get(url: str, opener: Callable | None, limiter: RateLimiter | None, expect: list[str]):
    headers = _sec_headers()
    (limiter or SEC_LIMITER).wait()
    return get_json(url, headers, opener=opener, expect_keys=expect)


def _norm_cik(cik: object) -> str:
    s = str(cik).strip()
    if not re.fullmatch(r"[0-9]{1,10}", s) or int(s) <= 0:
        raise ValueError(f"invalid CIK {ascii(cik)}")
    return s.zfill(10)


def fetch_cik_map(
    opener: Callable | None = None, *, limiter: RateLimiter | None = None
) -> dict[str, str]:
    """Fetch SEC's ticker list and return {TICKER: 10-digit zero-padded CIK}.

    RuntimeError if SEC_USER_AGENT is unset/blank/placeholder. Malformed entries are skipped.
    """
    body = _sec_get(CIK_MAP_URL, opener, limiter, ["0"])
    if not isinstance(body, dict):
        raise RuntimeError("unexpected company_tickers response shape")
    out: dict[str, str] = {}
    for entry in body.values():
        if not isinstance(entry, dict):
            continue
        ticker, cik = entry.get("ticker"), entry.get("cik_str")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        try:
            norm = _norm_cik(cik)
        except ValueError:
            continue
        out.setdefault(ticker.strip().upper(), norm)
    return out


def fetch_submissions(
    cik: str, opener: Callable | None = None, *, limiter: RateLimiter | None = None
) -> dict:
    """Fetch the EDGAR submissions JSON for `cik` (padded to 10 digits)."""
    padded = _norm_cik(cik)
    body = _sec_get(SUBMISSIONS_URL.format(cik=padded), opener, limiter, ["filings", "cik"])
    if not isinstance(body, dict):
        raise RuntimeError("unexpected submissions response shape")
    return body


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)  # compare in UTC; convert to NY only for output


def parse_8k_filings(submissions: dict, since: datetime) -> list[dict]:
    """Extract 8-K and 8-K/A filings accepted at or after `since`, newest first.

    Reads the parallel arrays in filings.recent. Raises ValueError on a malformed
    submissions object or unequal array lengths. Each result has accession, form,
    filed_at (NY, ISO), items, catalysts, red_flags, url, primary_document and
    time_estimated (True when acceptanceDateTime was missing and the end of the
    filingDate in NY was used instead).
    """
    if not isinstance(since, datetime) or since.tzinfo is None:
        raise ValueError("since must be a timezone-aware datetime")
    if not isinstance(submissions, dict):
        raise ValueError("submissions must be an object")
    recent = (submissions.get("filings") or {}).get("recent") if isinstance(
        submissions.get("filings"), dict
    ) else None
    if not isinstance(recent, dict):
        raise ValueError("submissions missing filings.recent")
    names = (
        "accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument",
    )
    arrays = []
    for n in names:
        arr = recent.get(n)
        if not isinstance(arr, list):
            raise ValueError(f"filings.recent.{n} must be a list")
        arrays.append(arr)
    if len({len(a) for a in arrays}) != 1:
        raise ValueError("filings.recent arrays have unequal lengths")
    try:
        cik_s = str(submissions.get("cik")).strip()
        if not re.fullmatch(r"[0-9]{1,10}", cik_s):
            raise ValueError("bad cik")
        cik_int = int(cik_s)
    except ValueError:
        raise ValueError("submissions missing a numeric cik") from None

    out: list[tuple[datetime, dict]] = []
    for acc, fdate, accepted, form, items, doc in zip(*arrays, strict=True):
        if form not in ("8-K", "8-K/A"):
            continue
        if not isinstance(acc, str) or not isinstance(doc, str) or not acc or not doc:
            raise ValueError("8-K entry missing accessionNumber or primaryDocument")
        when = _parse_dt(accepted)
        estimated = False
        if when is None:
            try:
                day = datetime.strptime(str(fdate), "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"8-K {acc} has no usable acceptance time or date") from None
            when = day.replace(hour=23, minute=59, second=59, tzinfo=NY).astimezone(UTC)
            estimated = True
        if when < since.astimezone(UTC):
            continue
        codes = [c.strip() for c in str(items or "").split(",") if c.strip()]
        catalysts = [
            ITEM_CATALYSTS.get(c, f"item_{c}") for c in codes if c not in NON_CATALYST_ITEMS
        ]
        out.append((when, {
            "accession": acc,
            "form": form,
            "filed_at": when.astimezone(NY).isoformat(),
            "items": codes,
            "catalysts": catalysts,
            "red_flags": sorted({c for c in catalysts if c in RED_FLAG_CATALYSTS}),
            "url": f"{EDGAR_ARCHIVES}/{cik_int}/{acc.replace('-', '')}/{doc}",
            "primary_document": doc,
            "time_estimated": estimated,
        }))
    # Sort on the aware instant, not the local-time string (DST fall-back hour).
    out.sort(key=lambda t: (t[0].astimezone(UTC), t[1]["accession"]), reverse=True)
    return [f for _, f in out]


# --------------------------------------------------------------------------
# Headlines
# --------------------------------------------------------------------------


def headline_flags(text: str) -> list[str]:
    """Return sorted flags whose keywords (see HEADLINE_KEYWORDS) appear in `text`."""
    if not isinstance(text, str):
        return []
    return sorted(flag for flag, rx in _KEYWORD_RES.items() if rx.search(text))


def _norm_headline(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def digest(
    symbol: str,
    news: dict,
    filings: list[dict],
    now: datetime,
    lookback_hours: int = 24,
) -> dict:
    """Deterministic catalyst digest for one symbol.

    Keeps news items that list `symbol`, were created within the lookback window
    and not after `now`; de-duplicates by normalized headline (earliest kept);
    sorts newest first. Malformed items are skipped and counted. `filings` is the
    output of parse_8k_filings, passed through. Times are NY ISO strings.

    Raises ValueError when `news` is not an Alpaca reply {"news": [...]}: a failed or malformed
    fetch must never read as "no headlines, no halts" (#112).
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    bad_lookback = isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int)
    if bad_lookback or lookback_hours < 1:
        raise ValueError("lookback_hours must be an integer >= 1")
    sym = symbol.strip().upper()
    # All comparisons/arithmetic in UTC (same-ZoneInfo NY datetimes compare by wall
    # clock and are wrong in the fall-back hour); NY is for output only.
    now_utc = now.astimezone(UTC)
    start = now_utc - timedelta(hours=lookback_hours)
    raw_items = news.get("news") if isinstance(news, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError("news reply is malformed (no news list): not the same as no news")

    considered = skipped = outside = 0
    kept: dict[str, tuple[datetime, dict]] = {}
    duplicates = 0
    for item in raw_items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        syms = item.get("symbols")
        if not isinstance(syms, list) or sym not in {str(s).upper() for s in syms}:
            continue
        considered += 1
        headline = item.get("headline")
        created = _parse_dt(item.get("created_at"))
        key = _norm_headline(headline) if isinstance(headline, str) else ""
        if created is None or not key:
            skipped += 1
            continue
        if created < start or created > now_utc:
            outside += 1
            continue
        entry = {
            "time": created.astimezone(NY).isoformat(),
            "source": item.get("source"),
            "headline": headline.strip(),
            "url": item.get("url"),
            "flags": headline_flags(headline),
        }
        if key in kept:
            duplicates += 1
            if created >= kept[key][0]:
                continue
        kept[key] = (created, entry)

    ordered = sorted(kept.values(), key=lambda t: (t[0], t[1]["headline"]), reverse=True)
    headlines = [e for _, e in ordered]
    filing_list = [f for f in filings if isinstance(f, dict)] if isinstance(filings, list) else []
    catalysts: set[str] = set()
    red: set[str] = set()
    for h in headlines:
        catalysts.update(h["flags"])
        red.update(f for f in h["flags"] if f in HEADLINE_RED_FLAGS)
    for f in filing_list:
        catalysts.update(f.get("catalysts", []))
        red.update(f.get("red_flags", []))
    return {
        "symbol": sym,
        "window_start": start.astimezone(NY).isoformat(),
        "now": now_utc.astimezone(NY).isoformat(),
        "headlines": headlines,
        "filings": filing_list,
        "catalysts": sorted(catalysts),
        "red_flags": sorted(red),
        "counts": {
            "news_for_symbol": considered,
            "headlines": len(headlines),
            "duplicates_removed": duplicates,
            "outside_window": outside,
            "malformed_skipped": skipped,
            "filings": len(filing_list),
        },
    }


# --------------------------------------------------------------------------
# Earnings-report headlines for the event gate's news signal (#169)
# --------------------------------------------------------------------------
# The digest's "earnings" flag (HEADLINE_KEYWORDS) matches the bare word anywhere, which suits
# the analyst's context but over-blocks as a gate signal: "Dow Jumps Over 600 Points;
# Constellation Energy Posts Upbeat Earnings" is tagged with many symbols. The gate counts a
# headline only when ONE clause of it names the company (or its ticker) AND a report-event
# phrase. Clauses are split on ";" and "|", which roundups use to join separate stories.

# Corporate words dropped from the END of a company name before matching ("Apple Inc." ->
# "apple", "OneMain Holdings, Inc." -> "onemain"). They are only stripped as trailing words,
# so a name that starts with one ("New York Times") keeps it.
_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd",
    "limited", "plc", "llc", "lp", "llp", "sa", "nv", "ag", "se", "as", "asa", "ab", "spa",
    "holdings", "holding", "group", "the", "class", "a", "b", "c", "common", "stock",
    "ordinary", "shares", "adr", "ads", "new", "de", "del",
})
# A one-word short form (the first word of a longer name) must not be one of these: "General
# Motors" must not match "general", nor "First Solar" match "first".
_COMMON_FIRST_WORDS = frozenset({
    "american", "first", "general", "united", "national", "international", "global", "new",
    "north", "south", "east", "west", "western", "eastern", "central", "pacific", "atlantic",
    "energy", "financial", "bank", "capital", "health", "healthcare", "technology",
    "technologies", "digital", "data", "power", "home", "real", "royal", "standard", "smart",
    "applied", "advanced", "public", "service", "services", "trust", "life", "great", "blue",
    "green", "red", "black", "white", "gold", "golden", "silver", "star", "sun", "solar",
    "community", "citizens", "peoples", "federal", "state", "us", "usa", "world", "big",
})
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_APOSTROPHES = re.compile(r"['’](?=[A-Za-z])")  # "McDonald's" -> "McDonalds"

# Report-event phrases: a scheduled or released quarterly report, not the bare word.
_W = r"(?:[\w$.,'’&-]+\s+)"  # one word and its trailing space (bounded repeats below)
_QUARTER = (r"(?:q[1-4]|(?:first|second|third|fourth)[\s-]+quarter|quarterly|fiscal"
            r"(?:[\s-]+year)?|fy\s?\d{2,4}|full[\s-]+year)")
# "results" of a report, not of a trial, vote or probe ("Positive Results From Phase 3 Trial",
# "results of shareholder vote")
_RESULTS = r"results(?!\s+(?:from|of|in)\b)"
_REPORT_EVENT = re.compile(
    r"(?<![a-z0-9])(?:"
    # "to report third-quarter earnings", "will announce results"
    r"(?:to|will)\s+(?:report|announce|release|post)\s+" + _W + r"{0,6}?(?:earnings|eps|"
    + _RESULTS + ")"
    # "reports Q3 results", "reported fourth quarter earnings"
    + r"|report(?:s|ed)?\s+" + _W + r"{0,6}?(?:earnings|eps|" + _RESULTS + ")"
    # "announces third quarter 2026 financial results", "announced results for the quarter"
    + r"|announce(?:s|d)?\s+" + _W + r"{0,6}?" + _RESULTS
    # "posts upbeat earnings", "posted record quarterly profit" (not "loss": price moves)
    + r"|post(?:s|ed)?\s+" + _W + r"{0,5}?(?:earnings|results|profit|eps)"
    # "Q3 earnings", "third-quarter results", "fiscal year 2026 results"
    r"|" + _QUARTER + r"\s+" + _W + r"{0,2}?(?:earnings|results|eps|profit)"
    # "earnings call", "earnings release", "earnings report", "earnings beat"
    r"|earnings\s+(?:call|calls|release|releases|report|reports|date|preview|beat|beats"
    r"|miss|misses|surprise|lag|lags|top|tops)"
    # "falls after earnings", "ahead of its Q3 earnings" (not "after earnings season", and not
    # "on earnings optimism": "on" is left out)
    r"|(?:after|ahead\s+of)\s+(?:its\s+)?(?:" + _QUARTER + r"\s+)?earnings"
    r"(?!\s+(?:season|week|optimism|hopes|growth|power|fears|worries|momentum)\b)"
    # "results due", "earnings due"
    r"|(?:results|earnings)\s+due"
    r"|(?:conference|investor)\s+call\s+to\s+discuss\s+" + _W + r"{0,4}?results"
    # "beats estimates", "misses analyst expectations", "tops the $1.60 estimate"
    r"|(?:beat|beats|beating|miss|misses|missed|missing|top|tops|topped|topping|exceed|exceeds"
    r"|exceeded|lag|lags|lagged|surpass|surpasses|surpassed)\s+" + _W + r"{0,4}?"
    r"(?:estimates?|expectations|forecasts?|consensus|views)"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r"[;|]")
_EXCHANGE_MARK = r"(?:NASDAQ|NYSE(?:\s+AMERICAN)?|NYSEAMERICAN|AMEX)\s*:\s*"


def _words(text: str) -> list[str]:
    return [w.lower() for w in _TOKEN.findall(text.replace("&", " and "))]


def name_forms(name: object) -> list[tuple[str, ...]]:
    """Word sequences that name the company in a headline: the name without trailing
    corporate words, and its first word alone when that word is distinctive (4+ letters, not
    a common word). [] for a name with nothing left. "&" reads as "and"; apostrophes are
    dropped ("MCDONALD'S" and "MCDONALDS" give the same form)."""
    if not isinstance(name, str):
        return []
    words = _words(_APOSTROPHES.sub("", name))
    while words and words[-1] in _NAME_SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words = words[1:]
    if not words:
        return []
    forms = [tuple(words)]
    if len(words) > 1 and len(words[0]) >= 4 and words[0] not in _COMMON_FIRST_WORDS:
        forms.append((words[0],))
    return forms


# Linking words that stay lower case inside a name ("Bank of America", "Bank of New York").
_LINK_WORDS = frozenset({"of", "the", "and", "de", "du", "la", "for", "in", "at", "on"})


def _capitalised_tokens(clause: str) -> list[tuple[str, bool]]:
    """(lower-case word, written as a name) for each word of the clause: a word with any
    upper-case letter ("Apple", "eBay", "iRobot", "AT"), a number, or a linking word."""
    out = []
    for tok in _TOKEN.findall(clause.replace("&", " and ")):
        low = tok.lower()
        out.append((low, tok != low or tok[0].isdigit() or low in _LINK_WORDS))
    return out


def _names_company(clause: str, forms: list[tuple[str, ...]], tickers: set[str]) -> bool:
    # The name must be written as a name (capitalised): "lower target ahead of ..." is not
    # Target. Matched with and without apostrophes ("McDonald's" and "McDonalds").
    for variant in {clause, _APOSTROPHES.sub("", clause)}:
        toks = _capitalised_tokens(variant)
        for form in forms:
            n = len(form)
            for i in range(len(toks) - n + 1):
                window = toks[i:i + n]
                if tuple(w for w, _ in window) == form and all(c for _, c in window):
                    return True
    for t in tickers:
        # Case-sensitive, as a whole token. A ticker of 1-2 letters ("ON", "T") must carry a
        # ticker marker ($ON, (ON), NASDAQ: ON) so a capitalised word never counts.
        bare = re.escape(t)
        marked = rf"(?:\$|\(|(?i:{_EXCHANGE_MARK})){bare}(?![A-Za-z0-9])"
        if re.search(marked, clause):
            return True
        if len(t) >= 3 and re.search(rf"(?<![A-Za-z0-9$.]){bare}(?![A-Za-z0-9])", clause):
            return True
    return False


def earnings_event_headline(headline: object, tickers: object, names: object) -> bool:
    """True when ONE clause of `headline` names the company (a form of one of `names`, written
    capitalised, or one of `tickers` as a ticker) AND a report-event phrase ("to report ...
    earnings", "reports Q3 results", "announces ... results", "earnings call", "beats
    estimates", ...). The bare word "earnings" is not enough, and neither is another
    company's report in the same roundup (#169)."""
    if not isinstance(headline, str) or not headline.strip():
        return False
    forms = [f for n in (names if isinstance(names, (list, tuple, set)) else [])
             for f in name_forms(n)]
    syms = {t.strip().upper() for t in (tickers if isinstance(tickers, (list, tuple, set))
                                        else []) if isinstance(t, str) and t.strip()}
    syms |= {s.replace(".", "-") for s in syms if "." in s} | {
        s.replace("-", ".") for s in syms if "-" in s}
    for clause in _CLAUSE_SPLIT.split(headline):
        if _REPORT_EVENT.search(clause) and _names_company(clause, forms, syms):
            return True
    return False
