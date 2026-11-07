r"""Deterministic scan for money values in debate text (CLAUDE.md rule 2).

Bull/Bear text may quote a number only if it appears in the decision packet: a plan record's
entry, stop, target, risk_per_share or reward_to_risk, or a number written verbatim in an
analyst's evidence. Any other price, size, stop or target is rejected. This module is a
conservative regex filter, not a parser: nothing it finds is ever used to build an order.

Patterns (a number is `\d[\d,]*(\.\d+)?`):
  (a) `$` followed by a number                      "$101.50", "$ 5"
  (b) a number followed within 2 words by share(s), qty, quantity, contracts, lot(s)
      (also "shares: 100" / "qty 50")               "100 shares", "50 more shares"
  (c) a number within 3 words after a keyword (at most 2 words between), and price-like:
      - strong keywords entry, stop, stop-loss, target, price, limit: a `$`, decimals, or an
        integer with at most ONE word between      "entry 102", "target near 105", "stop 101"
      - weak keywords risk, size, buy, sell: only with a `$` or decimals ("buy 99.10")
      - `at`: only directly followed by `$` or a decimal number ("at 101.50", "at $99")
  (e) a number directly after buy, sell, long, short, size, position, qty or trade (at most one
      word between): "buy 100", "size of 500", "go long 100", "sell short 50", "trade 1000 USD"
  (a2) a number followed by dollar(s), usd or bucks: "98 dollars"
  (b) also covers unit(s): "position of 100 units"
  (d) a bare number with exactly 2 decimals that is not immediately followed by "%" or "x"
                                                    "101.50"; but "1.25x" and "3.00%" pass

A (b) mention is ALWAYS a violation (plan records hold no size). Any other mention is a
violation unless its value is Decimal-equal to an allowed plan value ("101.5" == "101.50").

Documented exemptions (apply ONLY to a bare (d) number or a weak (c) hit; they never apply to
a `$` amount, a strong keyword, an (e) or an (a2) hit, so "stop item 98.50", "stop at
$98.50:00" and "buy at 101:50" are still flagged):
  - 8-K item numbers: a number immediately preceded by `item`, `item_` or `item-`
    (case-insensitive): "item 9.01", "Item 2.02", "item_9.01", "8-K item 9.01".
  - clock times such as 10:30 or 09:45 (a number touching a `:digit` pair).
  - "at least 3", "at most 2", "risk of 2 halts", "price action over 15 minutes" and
    "buy signal in 3 of 3 analysts" are prose counts, not prices.
  - "per share", "/share" and "a share" are a price unit, not a size: "Risk is 2.39 per share"
    is not a (b) mention, but the number is treated as a price and must be an allowed value.

Allowed values (issue #87):
  - Plan values: entry, stop, target, risk_per_share and reward_to_risk of every plan, matched
    Decimal-equal ("101.5" == "101.50"). They never allow a (b) size: "104 shares" is flagged
    even when 104.00 is a plan's target.
  - Quoted values: any number written in the evidence of an analyst report whose run is ok,
    matched VERBATIM as a string ("2.54" allows "2.54" but not "2.540" or "2,54"; "50,000"
    allows "50,000" but not "50000"). A quoted value is allowed in every pattern, sizes
    included: "CEO sold 50,000 shares" passes when an analyst's evidence says "50,000".
    Repeating a number the analysts already reported adds no new money value; the guard's job
    is to stop the debaters inventing prices, sizes, stops or targets.
Any other number in a money pattern is a violation. Share counts NOT in the evidence (e.g. a
model-invented "CEO sold 50,000 shares") are flagged; the model must rephrase without them.
Accepted consequence: a small integer in evidence ("2 of 3 analysts") also allows it in a
money pattern ("buy 2"). Nothing in debate text is ever used to build an order (rule 2).

Known false positives (accepted, the guard errs on the side of flagging): a ratio such as
"2.50" in prose unless it equals a plan value; "stop 3 times"; "target 2 analysts";
"short interest 12"; "position 3 of 5". The fix for the model is to rephrase without the number.
"""
from __future__ import annotations

import re
from collections.abc import Collection
from decimal import Decimal, InvalidOperation

from agents.schemas import DecisionPacket

_NUM = r"\d[\d,]*(?:\.\d+)?"
_SIZE_WORDS = r"(?:shares?|qty|quantity|contracts?|lots?|units?)"
_STRONG = {"entry", "stop", "stop-loss", "target", "price", "limit"}
_KEYWORDS = r"(?:entry|stop[- ]loss|stop|target|price|limit|risk|size|buy|sell|at)"

_A = re.compile(rf"\$\s*({_NUM})")
_A_WORD = re.compile(rf"(?<![\w.,])({_NUM})\s*(?:dollars?|usd|bucks)\b", re.I)
_E = re.compile(
    rf"\b(buy|sell|long|short|size|position|qty|trade)\b"
    rf"(?=((?:[\s:=,]+[A-Za-z][\w'-]*){{0,1}})[\s:=,]+(\$?)({_NUM}))",
    re.I,
)
_PER_SHARE = r"(?:/\s*|per\s+|a\s+)share"
_PS = re.compile(rf"(?<![\w.,])({_NUM})\s*{_PER_SHARE}\b", re.I)
_B = re.compile(
    rf"(?<![\w.,])({_NUM})(?!\s*{_PER_SHARE})(?:\s+[A-Za-z][\w'-]*){{0,2}}?\s+{_SIZE_WORDS}\b",
    re.I,
)
_B_REV = re.compile(rf"(?<!per\s)(?<!/)(?<!\ba\s)\b{_SIZE_WORDS}\s*[:=]?\s*\$?({_NUM})", re.I)
# Zero-width lookahead so overlapping keywords ("buy at ...", "stop at ...") are all tried.
_C = re.compile(
    rf"\b({_KEYWORDS})\b(?=((?:[\s:=,]+[A-Za-z][\w'-]*){{0,2}})[\s:=,]+(\$?)({_NUM}))", re.I
)
_D = re.compile(r"(?<![\d.,])(\d+\.\d{2})(?!\d)(?!\s*%)(?!\s*x\b)", re.I)
_ITEM = re.compile(r"item[\s_-]*$", re.I)
_CLOCK_AFTER = re.compile(r":\d\d")


def _exempt(text: str, start: int, end: int) -> bool:
    """True for an 8-K item number or a clock time."""
    if _ITEM.search(text[:start]):
        return True
    if _CLOCK_AFTER.match(text, end) or text[max(0, start - 1):start] == ":":
        return True
    return False


def _c_hits(text: str) -> list[tuple[str, str, str, int]]:
    """Pattern (c) hits as (kind, mention, number, number_start), price-like numbers only.

    kind is "c" for a strong keyword or a `$` amount (never exempt) and "w" for a weak
    keyword / `at` hit (exemptions apply).
    """
    hits = []
    for m in _C.finditer(text):
        kw = m.group(1).lower().replace(" ", "-")
        between, dollar, num = m.group(2), m.group(3), m.group(4)
        words = len(between.split())
        decimals = "." in num
        if kw == "at":
            ok = words == 0 and (dollar or decimals)
        elif kw in _STRONG:
            ok = dollar or decimals or words <= 1
        else:
            ok = dollar or decimals
        if ok:
            kind = "c" if (kw in _STRONG or dollar) else "w"
            hits.append((kind, text[m.start():m.end(4)], num, m.start(4)))
    return hits


def _scan(text: str) -> list[tuple[str, str, str]]:
    """Return (kind, mention, number) per distinct number position; (b) wins over the rest.

    Weak hits (bare 2-decimal numbers, weak-keyword hits) are subject to the item/clock
    exemptions; keyword, `$`, currency-word, (e) and per-share hits are not.
    """
    weak: dict[int, tuple[str, str, str]] = {}
    for m in _D.finditer(text):
        weak[m.start(1)] = ("d", m.group(0).strip(), m.group(1))
    c_hits = _c_hits(text)
    for kind, mention, num, start in c_hits:
        if kind == "w":
            weak[start] = ("w", mention, num)
    found = {k: v for k, v in weak.items() if not _exempt(text, k, k + len(v[2]))}
    for kind, mention, num, start in c_hits:
        if kind == "c":
            found[start] = ("c", mention, num)
    for kind, pattern in (("a", _A), ("a", _A_WORD), ("p", _PS)):
        for m in pattern.finditer(text):
            found[m.start(1)] = (kind, m.group(0).strip(), m.group(1))
    for m in _E.finditer(text):
        found[m.start(4)] = ("e", text[m.start():m.end(4)], m.group(4))
    for pattern in (_B, _B_REV):
        for m in pattern.finditer(text):
            found[m.start(1)] = ("b", m.group(0).strip(), m.group(1))
    return [found[k] for k in sorted(found)]


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def allowed_values(packet: DecisionPacket) -> set[Decimal]:
    """Entry, stop, target, risk_per_share and reward_to_risk of every plan, as Decimals."""
    out: set[Decimal] = set()
    for plan in packet.exit_plans:
        for raw in (plan.entry, plan.stop, plan.target, plan.risk_per_share, plan.reward_to_risk):
            value = _to_decimal(str(raw))
            if value is not None:
                out.add(value)
    return out


_TOKEN = re.compile(r"(?<![\d.,])\d[\d,]*(?:\.\d+)?")


def _verbatim(raw: str) -> str:
    """A number as written, minus a trailing comma ("102," in "entry 102, stop" -> "102")."""
    return raw.rstrip(",")


def quoted_values(packet: DecisionPacket) -> frozenset[str]:
    """Every number written in analyst evidence, verbatim, from reports whose run is ok.

    A report whose run failed (the neutral fallback) contributes nothing: its single evidence
    point is runner text, not analysis.
    """
    out: set[str] = set()
    for agent, report in packet.reports.items():
        run = packet.runs.get(agent)
        if run is None or not run.ok:
            continue
        for ev in report.evidence:
            out.update(_verbatim(m.group(0)) for m in _TOKEN.finditer(ev.point))
    return frozenset(out)


def money_mentions(text: str) -> list[str]:
    """Every substring that looks like a price, size or money value (patterns a-d above)."""
    return [mention for _, mention, _ in _scan(text)]


def money_violations(
    text: str, allowed: set[Decimal], quoted: Collection[str] = frozenset()
) -> list[str]:
    """Mentions that are neither a verbatim quoted value nor Decimal-equal to a plan value.

    `allowed` are plan values (prices only: a (b) size is never allowed by them); `quoted`
    are verbatim numbers from analyst evidence (allowed in any pattern, sizes included).
    """
    bad: list[str] = []
    for kind, mention, number in _scan(text):
        if _verbatim(number) in quoted:
            continue
        value = _to_decimal(number)
        if kind == "b" or value is None or value not in allowed:
            bad.append(mention)
    return bad
