"""Probe the Alpaca PAPER account for behaviours its docs leave open (#196, before PR 3).

    python -m pipeline.probe_broker                    # dry plan: prints the steps, sends nothing
    python -m pipeline.probe_broker --live             # runs them on the paper account (SPY)
    python -m pipeline.probe_broker --live --symbol QQQ --out probe.json --raw-out raw.json

Only Ivan runs it with --live, during market hours. "--live" here means "send to the PAPER
account"; it has nothing to do with live trading (CLAUDE.md rule 1), which this script
cannot reach. Without --live NOTHING is requested: it prints the plan and exits 3. It uses
tools/broker.py, so the paper host is fixed and the client refuses unless limits.paper is
true and the paper account verifies.

With --live, on ONE liquid symbol and ONE share per order:
  0. verify the paper account; refuse unless the market is open and closes in more than
     MIN_MINUTES_TO_CLOSE (the legs are time in force day), and the symbol has no position
     and no open order (the probe never touches anything else);
  1. look up an unknown client_order_id (tools/broker reads HTTP 404 as None);
  2. send a 1-share BUY bracket: limit entry at the ask + a small allowance (marketable),
     stop 0.5% below, target 0.5% above, time in force day;
  3. look it up by its client_order_id (does the reply show the legs?);
  4. send the SAME client_order_id again (is a duplicate refused, and how?). If Alpaca
     accepts it, that order is cancelled at once; if it fills first, the account briefly
     holds 2 shares, and the cleanup closes both;
  5. wait for the entry to fill; record the position (qty, qty_available) and what the
     open-orders listing shows; then try to close the position while the legs are open;
  6. cancel the legs, then close the position.
Whatever happens, including Ctrl-C, it then cleans up, retrying each step up to
CLEANUP_PASSES times: it cancels the symbol's open orders (and their legs), closes its
position, and checks the account is flat in that symbol. It prints a report (and writes it
as JSON with --out; Alpaca's raw replies, account number redacted, with --raw-out).

This is a manual test tool, not the decision path: its 1-share probe order is not an
agent's plan and does not go through risk_check. Paper only; Alpaca paper calls and the IEX
quote are free.

Exit codes: 0 every step ran without an error and the account is flat; 4 flat, but the run
stopped early, the entry never filled, or any step or cleanup call errored (incomplete: the
report's "errors" and cleanup notes say which); 1 NOT flat (check the paper account now);
2 refused before any order; 3 dry run (nothing requested). An answer the probe exists to
record (the 422 on the resend, the 403 on closing with the legs open) is a finding, not an
error.

After a close whose outcome is unclear (a BrokerError with may_have_acted: a timeout, a 5xx,
an unreadable reply), the probe never closes again blindly: it records check_close (the
position and the symbol's recent orders). The cleanup leaves a working close order alone and
waits for it, and finds the bracket's held stop through the entry lookup: the open-orders
listing does not show it (#196).
"""
from __future__ import annotations

import argparse
import io
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pipeline.smoke_common import load_env
from tools.broker import FINAL_STATUSES, AlpacaPaperBroker, BrokerError, bracket_request
from tools.config import load_limits
from tools.http import _default_open
from tools.scan import fetch_snapshots, validate_symbols

OK, NOT_FLAT, REFUSED, DRY_RUN, INCOMPLETE = 0, 1, 2, 3, 4
CENT = Decimal("0.01")
BAND = Decimal("0.005")  # stop and target 0.5% from the ask
FILL_WAIT_S, LEGS_WAIT_S, FLAT_WAIT_S, POLL_S = 30, 15, 30, 1.0
MIN_MINUTES_TO_CLOSE = 10
CLEANUP_PASSES = 3
MAX_QUOTE_AGE = timedelta(seconds=15)
FINAL = FINAL_STATUSES  # one set: tools/broker.py
# The answers the probe exists to record (#196): anything else is an error, not a finding.
EXPECTED_RESEND = (422, 40010001)  # a resent client_order_id
EXPECTED_CLOSE_WITH_LEGS = (403, 40310000)  # closing while the legs hold the shares


def plan_lines(symbol: str) -> list[str]:
    return [
        f"symbol: {symbol}, 1 share per order, Alpaca PAPER account only "
        "(paper-api.alpaca.markets)",
        f"0. verify the paper account; refuse unless the market is open (and closes in more "
        f"than {MIN_MINUTES_TO_CLOSE} minutes) and the symbol has no position and no open order",
        "1. look up an unknown client_order_id",
        "2. send a 1-share BUY bracket: limit at ask + allowance, stop -0.5%, target +0.5%, day",
        "3. look it up by client_order_id (are the legs shown?)",
        "4. send the same client_order_id again (is the duplicate refused?)",
        "5. wait for the fill; record the position and the open orders; try to close the "
        "position while the legs are open",
        "6. cancel the legs; close the position",
        f"then (always, also on Ctrl-C, up to {CLEANUP_PASSES} passes): cancel the symbol's "
        "open orders, close its position, check it is flat",
    ]


def _err(exc: BrokerError) -> dict:
    return {"error": str(exc), "status": exc.status, "code": exc.code}


def _why(exc: BaseException) -> str:
    return str(exc)[:200] if isinstance(exc, (BrokerError, RuntimeError, ValueError)) \
        else type(exc).__name__


def probe_order(symbol: str, ask: Decimal) -> dict:
    """The 1-share probe order, in tools.size's order shape, for tools.broker.bracket_request."""
    allowance = max((ask * Decimal("0.001")).quantize(CENT, ROUND_CEILING), Decimal("0.05"))
    stop = (ask * (1 - BAND)).quantize(CENT, ROUND_FLOOR)
    target = (ask * (1 + BAND)).quantize(CENT, ROUND_CEILING)
    return {"symbol": symbol, "side": "buy", "qty": 1, "type": "market",
            "order_class": "bracket", "entry": f"{ask:.2f}", "stop_loss": f"{stop:.2f}",
            "take_profit": f"{target:.2f}", "slippage": f"{allowance:.2f}"}


class RecordingOpener:
    """Wraps the default opener and keeps Alpaca's raw replies (method, path, status, body),
    with the account's number and id redacted, so they can become test fixtures (#196)."""

    def __init__(self, inner: Callable = _default_open) -> None:
        self.inner, self.replies = inner, []

    def _keep(self, req: Any, status: int | None, raw: bytes) -> None:
        try:
            body: Any = json.loads(raw.decode("utf-8")) if raw.strip() else None
        except (ValueError, UnicodeDecodeError):
            body = raw[:2000].decode("utf-8", "replace")
        if isinstance(body, dict) and "account_number" in body:
            # the account reply: its number and its id both identify the account
            body = {**body, "account_number": "REDACTED"}
            if "id" in body:
                body["id"] = "REDACTED"
        self.replies.append({"method": req.get_method(),
                             "path": urllib.parse.urlsplit(req.full_url).path,
                             "status": status, "body": body})

    def __call__(self, req: Any, timeout: float | None = None) -> Any:
        try:
            resp = self.inner(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            self._keep(req, exc.code, raw)
            raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs,
                                         io.BytesIO(raw)) from None
        recorder = self

        class Tee:
            def __enter__(self):
                self._r = resp.__enter__()
                return self

            def __exit__(self, *a):
                return resp.__exit__(*a)

            def geturl(self):
                return self._r.geturl()

            def getcode(self):
                return self._r.getcode()

            @property
            def status(self):
                return getattr(self._r, "status", None)

            def read(self, n=None):
                raw = self._r.read() if n is None else self._r.read(n)
                recorder._keep(req, getattr(self._r, "status", None), raw)
                return raw

        return Tee()


class Probe:
    """Runs the steps against a verified broker. `quote` returns the symbol's live ask."""

    def __init__(self, broker: Any, symbol: str, quote: Callable[[str], Decimal],
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.b, self.symbol, self.quote = broker, symbol, quote
        self.sleep, self.monotonic = sleep, monotonic
        self.report: dict[str, Any] = {"symbol": symbol}
        self.cid = f"probe-{secrets.token_hex(12)}"
        self.complete = False
        self.errors: list[str] = []  # step errors: any makes the run incomplete (exit 4)
        self.cleanup_errors: list[str] = []

    def _wait(self, check: Callable[[], bool], seconds: float) -> bool:
        deadline = self.monotonic() + seconds
        while True:
            if check():
                return True
            if self.monotonic() >= deadline:
                return False
            self.sleep(POLL_S)

    def _mine(self, orders: list[dict]) -> list[dict]:
        return [o for o in orders if o.get("symbol") == self.symbol]

    def _error(self, step: str, exc: BaseException) -> None:
        self.errors.append(f"{step}: {_why(exc)}")

    def _check_close(self) -> dict:
        """After an unclear close: what the broker shows, never a second close."""
        try:
            return self.b.check_close(self.symbol)
        except BrokerError as exc:
            return {"state": "unknown", **_err(exc)}

    @staticmethod
    def _is_close(o: dict) -> bool:
        """A closing order (DELETE /v2/positions: a simple sell)."""
        return o.get("side") == "sell" and o.get("order_class") == "simple"

    def _position(self) -> dict | None:
        return next((p for p in self.b.list_positions() if p["symbol"] == self.symbol), None)

    def preflight(self) -> str | None:
        """Why the probe must not run, or None."""
        account = self.b.verify_paper_account()
        self.report["account"] = {**account, "account_number": "REDACTED"}
        clock = self.b.clock()
        self.report["clock"] = clock
        if clock["is_open"] is not True:
            return "the market is closed: run the probe during market hours"
        try:
            left = (datetime.fromisoformat(clock["next_close"])
                    - datetime.fromisoformat(clock["timestamp"]))
        except (TypeError, ValueError):
            return "the broker clock is unreadable"
        if left < timedelta(minutes=MIN_MINUTES_TO_CLOSE):
            return (f"the market closes in {int(left.total_seconds() // 60)} minutes: the legs "
                    f"are time in force day, so run the probe earlier")
        if self._position() is not None:
            return f"the paper account already holds {self.symbol}: the probe leaves it alone"
        if self._mine(self.b.list_open_orders()):
            return f"the paper account has open orders in {self.symbol}"
        return None

    def run(self) -> None:
        r = self.report
        # 1. unknown client_order_id
        unknown = f"probe-unknown-{secrets.token_hex(8)}"
        try:
            found = self.b.get_order_by_client_id(unknown)
            r["1_unknown_lookup"] = {
                "result": found,
                "meaning": "None means Alpaca answered HTTP 404 (tools.broker reads 404 as "
                           "not found)" if found is None else "an order came back"}
        except BrokerError as exc:
            r["1_unknown_lookup"] = _err(exc)
            self._error("1_unknown_lookup", exc)
        # 2. send the probe bracket
        ask = self.quote(self.symbol)
        order = probe_order(self.symbol, ask)
        r["2_request"] = bracket_request(order, self.cid)
        r["2_sent"] = self.b.submit_bracket(order, self.cid)
        # 3. lookup shows legs?
        found = self.b.get_order_by_client_id(self.cid)
        r["3_lookup"] = found
        r["3_legs_shown"] = bool(found and found.get("legs"))
        # 4. resend the same client_order_id
        try:
            dup = self.b.submit_bracket(order, self.cid)
            r["4_resend"] = {"accepted": True, "order": dup}
            if dup.get("id") and dup["id"] != r["2_sent"]["id"]:
                self._cancel(dup["id"], "4_resend_cancel")
        except BrokerError as exc:
            # a refusal (4xx) is Alpaca's answer, the finding; anything that may have acted
            # (no status, a 5xx, an unreadable or mismatched reply) means the order may exist
            refused = exc.status is not None and not exc.may_have_acted
            r["4_resend"] = {"accepted": False if refused else "unknown", **_err(exc)}
            if (exc.status, exc.code) != EXPECTED_RESEND:
                self._error("4_resend", exc)
        # 5. wait for the fill; record what holds the shares; close with the legs open
        state: dict[str, Any] = {}

        def entry_final() -> bool:
            state["order"] = self.b.get_order_by_client_id(self.cid) or {}
            return state["order"].get("status") in FINAL

        self._wait(entry_final, FILL_WAIT_S)
        entry = state.get("order") or {}
        r["5_entry_status"] = entry.get("status")
        if entry.get("status") != "filled":
            r["5_note"] = "entry did not fill: the closing checks are skipped"
            return
        legs = entry.get("legs") or []
        r["5_legs_before_close"] = legs
        r["5_position_before_close"] = self._position()
        r["5_open_orders_listing"] = self._mine(self.b.list_open_orders())
        try:
            closed = self.b.close_position(self.symbol)
            r["5_close_with_legs_open"] = {"closed": True, "order": closed}
        except BrokerError as exc:
            r["5_close_with_legs_open"] = {"closed": False, **_err(exc)}
            if exc.may_have_acted:  # not a refusal: find out, never assume it failed
                r["5_close_with_legs_open"]["check"] = self._check_close()
            if (exc.status, exc.code) != EXPECTED_CLOSE_WITH_LEGS:
                self._error("5_close_with_legs_open", exc)
        # 6. cancel the legs (at once, also if the close above succeeded), then close
        for leg in legs:
            if leg.get("status") not in FINAL:
                self._cancel(leg["id"], f"6_cancel_leg_{leg.get('type')}")
        r["6_legs_gone"] = self._wait(lambda: not self._mine(self.b.list_open_orders()),
                                      LEGS_WAIT_S)
        if not r["6_legs_gone"]:
            self.errors.append("6_legs_gone: the legs were still listed")
        step5 = r.get("5_close_with_legs_open") or {}
        if self._position() is None:
            r["6_close_after_legs"] = "no position left"
        elif (step5.get("check") or {}).get("state") == "closing":
            r["6_close_after_legs"] = "a close is already working: not closed again"
        else:
            try:
                r["6_close_after_legs"] = {"closed": True,
                                           "order": self.b.close_position(self.symbol)}
            except BrokerError as exc:
                r["6_close_after_legs"] = {"closed": False, **_err(exc)}
                if exc.may_have_acted:  # find out what happened; the cleanup acts on it
                    r["6_close_after_legs"]["check"] = self._check_close()
                self._error("6_close_after_legs", exc)
        self.complete = True

    def _cancel(self, order_id: str, key: str) -> None:
        try:
            self.b.cancel_order(order_id)
            self.report[key] = {"canceled": True}
        except BrokerError as exc:
            self.report[key] = {"canceled": False, **_err(exc)}
            self._error(key, exc)

    def _cleanup_pass(self, notes: list[str]) -> bool:
        """One pass: cancel the probe entry's legs (found by lookup: the listing misses a held
        stop) and the symbol's other open orders, leaving a working close alone; close the
        position (long or short) unless a close is already working; wait until flat. Each
        step has its own try. True when flat."""
        cancel: list[str] = []
        try:
            entry = self.b.get_order_by_client_id(self.cid)
            for leg in (entry or {}).get("legs") or []:
                if leg.get("status") not in FINAL_STATUSES:
                    cancel.append(leg["id"])
        except BrokerError as exc:
            notes.append(f"look up the entry's legs: {exc}")
        closing = False
        try:
            orders = self._mine(self.b.list_open_orders())
        except BrokerError as exc:
            notes.append(f"list orders: {exc}")
            orders = []
        for o in orders:
            if self._is_close(o):
                closing = True  # a close already working: wait for it, never close again
                continue
            cancel += [leg["id"] for leg in o.get("legs") or []] + [o["id"]]
        for oid in dict.fromkeys(cancel):
            try:
                self.b.cancel_order(oid)
            except BrokerError as exc:
                notes.append(f"cancel {oid}: {exc}")
        try:
            self._wait(lambda: not [o for o in self._mine(self.b.list_open_orders())
                                    if not self._is_close(o)], LEGS_WAIT_S)
        except BrokerError as exc:
            notes.append(f"wait for cancels: {exc}")
        try:
            if not closing and self._position() is not None:
                self.b.close_position(self.symbol)
        except BrokerError as exc:
            notes.append(f"close: {exc}")
            if exc.may_have_acted:  # find out; the next pass acts on what is there
                notes.append(f"close check: {self._check_close().get('state')}")
        try:
            return self._wait(lambda: self._position() is None
                              and not self._mine(self.b.list_open_orders()), FLAT_WAIT_S)
        except BrokerError as exc:
            notes.append(f"flat check: {exc}")
            return False

    def cleanup(self) -> bool:
        """Up to CLEANUP_PASSES passes until flat with no open orders in the symbol. Never
        raises a BrokerError; returns whether it is flat."""
        notes: list[str] = []
        flat = False
        for n in range(1, CLEANUP_PASSES + 1):
            flat = self._cleanup_pass(notes)
            if flat:
                break
            notes.append(f"pass {n}: not flat yet")
        self.report["cleanup"] = {"flat": flat, "notes": notes}
        self.cleanup_errors = [x for x in notes if not x.startswith("pass ")
                               and not x.startswith("close check: ")]
        return flat


def live_quote(symbol: str, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Decimal:
    """The symbol's live IEX ask: fresh (MAX_QUOTE_AGE), at or above the bid, at least $1."""
    snap = fetch_snapshots([symbol]).get(symbol) or {}
    q = snap.get("latestQuote") or {}
    try:
        ask, bid = Decimal(str(q["ap"])), Decimal(str(q["bp"]))
        if isinstance(q["ap"], bool) or isinstance(q["bp"], bool):
            raise ValueError
        when = datetime.fromisoformat(str(q["t"]).replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise BrokerError(f"no usable live quote for {symbol}") from None
    if not (ask.is_finite() and bid.is_finite()) or ask < 1 or bid <= 0 or ask < bid:
        raise BrokerError(f"no usable live quote for {symbol}")
    if now() - when > MAX_QUOTE_AGE:
        raise BrokerError(f"the live quote for {symbol} is stale")
    return ask.quantize(CENT)


def main(argv: list[str] | None = None, *, broker: Any = None,
         quote: Callable[[str], Decimal] | None = None,
         sleep: Callable[[float], None] = time.sleep,
         monotonic: Callable[[], float] = time.monotonic) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.probe_broker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--live", action="store_true",
                   help="run the steps on the Alpaca PAPER account (otherwise: plan only)")
    p.add_argument("--symbol", default="SPY", help="one liquid symbol (default SPY)")
    p.add_argument("--out", type=Path, default=None, help="also write the report as JSON")
    p.add_argument("--raw-out", type=Path, default=None,
                   help="also write Alpaca's raw replies as JSON (account number redacted)")
    env = p.add_mutually_exclusive_group()
    env.add_argument("--env-file", type=Path, default=None, metavar="PATH")
    env.add_argument("--no-dotenv", action="store_true")
    args = p.parse_args(argv)
    try:
        (symbol,) = validate_symbols([args.symbol])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return REFUSED
    if not args.live:
        print("== probe_broker: dry plan only, NOTHING was requested ==")
        for line in plan_lines(symbol):
            print(line)
        print("\nRe-run with --live during market hours to run it on the paper account.")
        return DRY_RUN
    code = load_env(args)
    if code is not None:
        return code
    recorder = RecordingOpener()
    try:
        b = broker if broker is not None else AlpacaPaperBroker(load_limits(), opener=recorder)
    except (BrokerError, ValueError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED
    probe = Probe(b, symbol, quote or live_quote, sleep, monotonic)
    try:
        why = probe.preflight()
    except BrokerError as exc:
        why = str(exc)
    if why is not None:
        print(f"refused before any order: {why}", file=sys.stderr)
        return REFUSED
    try:
        probe.run()
    except KeyboardInterrupt:
        probe.report["stopped"] = "interrupted (Ctrl-C): cleaning up"
    except Exception as exc:  # noqa: BLE001 - report it, then always clean up
        probe.report["stopped"] = _why(exc)
    flat = probe.cleanup()
    probe.report["errors"] = probe.errors
    if not flat:
        print(f"ALERT: the paper account is NOT flat in {symbol}: check it now",
              file=sys.stderr)
    text = json.dumps(probe.report, indent=1, default=str)
    print(text)
    for path, data in ((args.out, text), (args.raw_out, json.dumps(recorder.replies, indent=1,
                                                                   default=str))):
        if path is not None:
            try:
                path.write_text(data + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"could not write {path}: {type(exc).__name__}", file=sys.stderr)
    if not flat:
        return NOT_FLAT
    if probe.errors or probe.cleanup_errors:
        print("the run is incomplete: a step or cleanup call errored (see \"errors\" and "
              "the cleanup notes)", file=sys.stderr)
        return INCOMPLETE
    return OK if probe.complete else INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
