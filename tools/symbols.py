"""The one ticker rule shared by scan, sizing, the risk gate and run_day's config (#34, #221).

A symbol is 1-5 ASCII capital letters, optionally followed by ONE share-class suffix: a dot and
1-2 letters ("BRK.B", "BF.A"). Dots appear nowhere else ("A..", "AB.", ".AB" are invalid).
Surrounding whitespace is stripped; what remains must be ASCII BEFORE it is upper-cased
("ß".upper() is "SS", and a Cyrillic "А" looks like "A"), so lookalikes and zero-width
characters inside a symbol never pass. The pattern is anchored, so SYMBOL_RE.match is safe
too.
"""
from __future__ import annotations

import re

SYMBOL_PATTERN = r"^[A-Z]{1,5}(?:\.[A-Z]{1,2})?\Z"
SYMBOL_RE = re.compile(SYMBOL_PATTERN, re.ASCII)


def normalize_symbol(raw: object) -> str | None:
    """The canonical (stripped, upper-case) symbol, or None if it breaks the rule."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.isascii():
        return None
    up = text.upper()
    return up if SYMBOL_RE.fullmatch(up) else None


def validate_symbols(symbols: list[str]) -> list[str]:
    """Normalise with normalize_symbol (1-5 ASCII letters, optional one share-class suffix
    like ".B") and de-duplicate (order kept). Raises ValueError for an invalid symbol."""
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        if not isinstance(s, str):
            raise ValueError(f"symbol must be a string, got {s!r}")
        up = normalize_symbol(s)
        if up is None:
            raise ValueError(f"invalid symbol {s!r}")
        if up not in seen:
            seen.add(up)
            out.append(up)
    return out
