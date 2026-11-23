"""Resolve the reconciler's blocks (Phase 5 PR 4, #155, #189).

    python -m journal.resolve                                  # list the open blocks
    python -m journal.resolve rb-000123 --note "what I did"    # resolve one

A block ("reconcile_block" in the journal) stops every new entry until it is resolved here.
Resolve one only after checking the Alpaca PAPER dashboard and fixing the cause: the block
says what the reconciler saw (a broker event the journal refused, an order it could not
read, an orphan, a position without a working stop, a fill above the limit price). The
resolution is appended to the journal with your note; nothing is ever rewritten. If the
cause is still there, the reconciler blocks again on its next pass.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from journal.log import Journal, JournalError, unresolved_blocks

NY = ZoneInfo("America/New_York")


def main(argv: list[str] | None = None, *, journal: Journal | None = None,
         now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m journal.resolve", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("block_id", nargs="?", help="the block to resolve (none: list them)")
    p.add_argument("--note", default=None, help="what was checked and done (required)")
    args = p.parse_args(argv)
    j = journal or Journal()
    try:
        open_ = unresolved_blocks(j.entries())
    except JournalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.block_id is None:
        if not open_:
            print("no open blocks: new entries are not blocked by the reconciler")
            return 0
        print(f"{len(open_)} open block(s): no new entry until each is resolved")
        for b in open_:
            print(f"  {b.get('block_id')}  {b.get('at')}  {b.get('kind')}: {b.get('reason')}")
        return 0
    if not args.note or not args.note.strip():
        print("error: --note is required: say what you checked and did", file=sys.stderr)
        return 2
    try:
        block = j.resolve_block(args.block_id, args.note, now or datetime.now(NY))
    except JournalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"resolved {args.block_id} ({block.get('kind')}: {block.get('reason')})")
    left = len(open_) - 1
    print(f"{left} open block(s) left" if left else "no open blocks left")
    return 0


if __name__ == "__main__":
    sys.exit(main())
