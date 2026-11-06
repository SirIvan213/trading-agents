r"""Deterministic scan for money values in debate text (CLAUDE.md rule 2).

Bull/Bear text may quote a plan record's exact entry, stop, target or risk_per_share, and
nothing else: no other price, and never a size or share count. This module is a conservative
regex filter, not a parser: nothing it finds is ever used to build an order.

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

Allowed values: entry, stop, target, risk_per_share and reward_to_risk of every plan.
Share counts from analyst text (e.g. "CEO sold 50,000 shares") are still flagged: conservative
by design; the model must rephrase ("a large insider sale") without the count.

Known false positives (accepted, the guard errs on the side of flagging): a ratio such as
"2.50" in prose unless it equals a plan value; "stop 3 times"; "target 2 analysts";
"short interest 12"; "position 3 of 5". The fix for the model is to rephrase without the number.
"""
from __future__ import annotations

import re
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


def money_mentions(text: str) -> list[str]:
    """Every substring that looks like a price, size or money value (patterns a-d above)."""
    return [mention for _, mention, _ in _scan(text)]


def money_violations(text: str, allowed: set[Decimal]) -> list[str]:
    """Mentions whose value is not Decimal-equal to an allowed value; (b) sizes always count."""
    bad: list[str] = []
    for kind, mention, number in _scan(text):
        value = _to_decimal(number)
        if kind == "b" or value is None or value not in allowed:
            bad.append(mention)
    return bad
