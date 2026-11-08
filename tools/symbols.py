"""The one ticker rule shared by scan, sizing and the risk gate (issue #34).

A symbol is 1-5 ASCII capital letters, optionally followed by ONE share-class suffix: a dot and
1-2 letters ("BRK.B", "BF.A"). Dots appear nowhere else ("A..", "AB.", ".AB" are invalid).
Input is stripped and upper-cased first, but must be ASCII BEFORE upper-casing: "ß".upper() is
"SS" and a Cyrillic "А" looks like "A", so lookalikes and zero-width characters never pass.
"""
from __future__ import annotations

import re

SYMBOL_PATTERN = r"[A-Z]{1,5}(?:\.[A-Z]{1,2})?"
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
