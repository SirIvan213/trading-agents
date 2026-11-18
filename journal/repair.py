"""Repair a journal whose last line a crash cut off (#128).

    python -m journal.repair                  # dry run: says what it would do, changes nothing
    python -m journal.repair --yes            # moves the cut-off last line aside
    python -m journal.repair --path P --yes   # another journal file

Every write appends one line ending in a newline, under the journal's lock, so a last line
WITHOUT its newline can only be a write a crash interrupted. The journal refuses to be read
while it is there (fail closed: no day totals, so no order). This tool, run by a human,
takes the lock, moves the bytes after the last newline into `<journal>.torn-<UTC time>`
(kept for the audit trail, never deleted) and truncates the journal to its last complete
line. Lines before it are never touched. A journal that ends with a newline is left alone.

It never creates a file (#192): a --path that does not exist is an error. After the check,
the complete lines are parsed and replayed too. A line mid-file that is not a JSON object,
or an order history that does not replay (a back-dated event, #194; an impossible step),
cannot be repaired here: the journal refuses to be read while it is there. It is reported
for a human and never changed.

Exit codes: 0 nothing to do or repaired; 1 a cut-off line was found in a dry run; 2 error;
3 a corrupt line or order history (reported, never changed). Exit 3 wins over exit 1: a dry
run that also finds a cut-off line says so in its output, so check for exit 0, not 1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from journal.log import DEFAULT_PATH, Journal, JournalError
from journal.orders import OrderError, replay

DRY_RUN_FOUND = 1
BAD_LINES = 3


def torn_tail(path: Path) -> bytes:
    """The bytes after the journal's last newline ("" when it ends with one or is empty)."""
    data = path.read_bytes() if path.exists() else b""
    cut = data.rfind(b"\n") + 1
    return data[cut:]


def bad_lines(path: Path) -> list[int]:
    """Line numbers (1-based) of complete lines that are not a JSON object; blank lines are
    skipped, as the journal does. The cut-off tail is not a complete line and not listed."""
    data = path.read_bytes() if path.exists() else b""
    complete = data[:data.rfind(b"\n") + 1]
    out = []
    for n, raw in enumerate(complete.split(b"\n")[:-1], 1):
        if not raw.strip():
            continue
        try:
            ok = isinstance(json.loads(raw.decode("utf-8")), dict)
        except (ValueError, UnicodeDecodeError, RecursionError):
            ok = False
        if not ok:
            out.append(n)
    return out


def history_error(path: Path) -> str | None:
    """Why the complete, well-formed lines do not replay as an order history (None if they
    do, or if some line is not a JSON object: bad_lines reports that)."""
    data = path.read_bytes() if path.exists() else b""
    complete = data[:data.rfind(b"\n") + 1]
    try:
        entries = [json.loads(x) for x in complete.decode("utf-8").splitlines() if x.strip()]
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not all(isinstance(e, dict) for e in entries):
        return None
    try:
        replay(entries)
    except OrderError as exc:
        return str(exc)
    return None


def repair(journal: Journal, *, apply: bool, now: datetime | None = None) -> dict:
    """{"torn_bytes": n, "moved_to": path or None, "bad_lines": [line numbers],
    "history_error": str or None}. With apply,
    moves a cut-off last line into `<journal>.torn-<UTC time>` and truncates the journal to
    its last newline, under the journal's lock. Complete lines that are not JSON objects are
    only reported (#192). Raises JournalError if the journal does not exist (nothing is
    created), the lock cannot be taken or a file cannot be written."""
    if not journal.path.is_file():
        raise JournalError(f"no journal at {journal.path}")
    with journal.exclusive():
        try:
            tail = torn_tail(journal.path)
            bad = bad_lines(journal.path)
            history = history_error(journal.path)
            if not tail or not apply:
                return {"torn_bytes": len(tail), "moved_to": None, "bad_lines": bad,
                        "history_error": history}
            stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            side = journal.path.with_name(f"{journal.path.name}.torn-{stamp}")
            with side.open("xb") as fh:  # never overwrite an earlier fragment
                fh.write(tail)
                fh.flush()
                os.fsync(fh.fileno())
            keep = journal.path.stat().st_size - len(tail)
            with journal.path.open("r+b") as fh:
                fh.truncate(keep)
        except OSError as exc:
            raise JournalError(f"journal repair failed ({type(exc).__name__})") from None
    return {"torn_bytes": len(tail), "moved_to": str(side), "bad_lines": bad,
            "history_error": history}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m journal.repair", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--path", type=Path, default=DEFAULT_PATH, help="journal file")
    p.add_argument("--yes", action="store_true", help="apply the repair (default: dry run)")
    args = p.parse_args(argv)
    try:
        out = repair(Journal(args.path), apply=args.yes)
    except JournalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not out["torn_bytes"]:
        print(f"{args.path}: ends with a complete line; nothing to repair.")
        code = 0
    elif out["moved_to"] is None:
        print(f"{args.path}: the last line is cut off ({out['torn_bytes']} bytes after the "
              "last newline). Re-run with --yes to move it aside.")
        code = DRY_RUN_FOUND
    else:
        print(f"{args.path}: moved the cut-off last line ({out['torn_bytes']} bytes) to "
              f"{out['moved_to']}.")
        code = 0
    if out["bad_lines"]:
        shown = ", ".join(str(n) for n in out["bad_lines"][:20])
        more = " ..." if len(out["bad_lines"]) > 20 else ""
        print(f"WARNING: {args.path}: line(s) {shown}{more} mid-file are not JSON objects. "
              "This tool does not change them, and the journal cannot be read until a human "
              "fixes them.", file=sys.stderr)
        return BAD_LINES
    if out["history_error"]:
        print(f"WARNING: {args.path}: the order history does not replay: "
              f"{out['history_error']}. This tool does not change it, and the journal cannot "
              "be read until a human fixes it.", file=sys.stderr)
        return BAD_LINES
    return code


if __name__ == "__main__":
    sys.exit(main())
