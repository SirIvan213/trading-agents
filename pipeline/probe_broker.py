"""Probe the Alpaca PAPER account for behaviours its docs leave open (#196, before PR 3).

    python -m pipeline.probe_broker                    # dry plan: prints the steps, sends nothing
    python -m pipeline.probe_broker --live             # runs them on the paper account (SPY)
    python -m pipeline.probe_broker --live --symbol QQQ

Only Ivan runs it with --live, during market hours. Without --live NOTHING is requested: it
prints the plan and exits 3. It uses tools/broker.py, so the paper host is fixed and the
client refuses unless limits.paper is true and the paper account verifies.

With --live, on ONE liquid symbol and ONE share:
  0. verify the paper account; refuse unless the market is open (broker clock) and the
     symbol has no position and no open order (the probe never touches anything else);
  1. look up an unknown client_order_id (is the answer 404?);
  2. send a 1-share BUY bracket: limit entry at the ask + a small allowance (marketable),
     stop 0.5% below, target 0.5% above, time in force day;
  3. look it up by its client_order_id (does the reply show the legs?);
  4. send the SAME client_order_id again (is a duplicate refused, and how?);
  5. wait for the entry to fill, then try to close the position while the legs are open
     (does it fail, and how?);
  6. cancel the legs, then close the position.
Whatever happens, it then cleans up: it cancels the symbol's open orders, closes its
position and checks the account is flat in that symbol. It prints a report (and writes it
as JSON with --out).

This is a manual test tool, not the decision path: its 1-share probe order is not an
agent's plan and does not go through risk_check. Paper only; Alpaca paper calls are free.

Exit codes: 0 done and flat; 1 finished but NOT flat (check the paper account now);
2 refused before any order; 3 dry run (nothing requested).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from collections.abc import Callable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from pipeline.smoke_common import load_env
from tools.broker import AlpacaPaperBroker, BrokerError, bracket_request
from tools.config import load_limits
from tools.scan import fetch_snapshots, validate_symbols

DRY_RUN, REFUSED, NOT_FLAT, OK = 3, 2, 1, 0
CENT = Decimal("0.01")
BAND = Decimal("0.005")  # stop and target 0.5% from the ask
FILL_WAIT_S, LEGS_WAIT_S, FLAT_WAIT_S, POLL_S = 30, 15, 30, 1.0


def plan_lines(symbol: str) -> list[str]:
    return [
        f"symbol: {symbol}, 1 share, Alpaca PAPER account only (paper-api.alpaca.markets)",
        "0. verify the paper account; refuse unless the market is open and the symbol has no "
        "position and no open order",
        "1. look up an unknown client_order_id",
        "2. send a 1-share BUY bracket: limit at ask + allowance, stop -0.5%, target +0.5%, day",
        "3. look it up by client_order_id (are the legs shown?)",
        "4. send the same client_order_id again (is the duplicate refused?)",
        "5. wait for the fill; try to close the position while the legs are open",
        "6. cancel the legs; close the position",
        "then: cancel the symbol's open orders, close its position, check it is flat",
    ]


def _err(exc: BrokerError) -> dict:
    return {"error": str(exc), "status": exc.status, "code": exc.code}


def probe_order(symbol: str, ask: Decimal) -> dict:
    """The 1-share probe order, in tools.size's order shape, for tools.broker.bracket_request."""
    allowance = max((ask * Decimal("0.001")).quantize(CENT, ROUND_CEILING), Decimal("0.05"))
    stop = (ask * (1 - BAND)).quantize(CENT, ROUND_FLOOR)
    target = (ask * (1 + BAND)).quantize(CENT, ROUND_CEILING)
    return {"symbol": symbol, "side": "buy", "qty": 1, "type": "market",
            "order_class": "bracket", "entry": f"{ask:.2f}", "stop_loss": f"{stop:.2f}",
            "take_profit": f"{target:.2f}", "slippage": f"{allowance:.2f}"}


class Probe:
    """Runs the steps against a verified broker. `quote` returns the symbol's live ask."""

    def __init__(self, broker: Any, symbol: str, quote: Callable[[str], Decimal],
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.b, self.symbol, self.quote = broker, symbol, quote
        self.sleep, self.monotonic = sleep, monotonic
        self.report: dict[str, Any] = {"symbol": symbol}
        self.cid = f"probe-{secrets.token_hex(12)}"

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

    def _position(self) -> dict | None:
        return next((p for p in self.b.list_positions() if p["symbol"] == self.symbol), None)

    def preflight(self) -> str | None:
        """Why the probe must not run, or None."""
        self.report["account"] = self.b.verify_paper_account()
        clock = self.b.clock()
        self.report["clock"] = clock
        if clock["is_open"] is not True:
            return "the market is closed: run the probe during market hours"
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
            r["1_unknown_lookup"] = {"result": self.b.get_order_by_client_id(unknown)}
        except BrokerError as exc:
            r["1_unknown_lookup"] = _err(exc)
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
            r["4_resend"] = {"accepted": False, **_err(exc)}
        # 5. wait for the fill, then close with the legs open
        filled = self._wait(
            lambda: (self.b.get_order_by_client_id(self.cid) or {}).get("status") == "filled",
            FILL_WAIT_S)
        r["5_entry_filled"] = filled
        if not filled:
            r["5_note"] = "entry did not fill in time: the closing checks are skipped"
            return
        legs = (self.b.get_order_by_client_id(self.cid) or {}).get("legs") or []
        r["5_legs_before_close"] = [{"id": leg["id"], "status": leg["status"],
                                     "type": leg["type"]} for leg in legs]
        try:
            r["5_close_with_legs_open"] = {"closed": True,
                                           "order": self.b.close_position(self.symbol)}
        except BrokerError as exc:
            r["5_close_with_legs_open"] = {"closed": False, **_err(exc)}
        # 6. cancel the legs, then close
        for leg in legs:
            if leg["status"] in ("new", "held", "accepted", "pending_new", "partially_filled"):
                self._cancel(leg["id"], f"6_cancel_leg_{leg['type']}")
        r["6_legs_gone"] = self._wait(lambda: not self._mine(self.b.list_open_orders()),
                                      LEGS_WAIT_S)
        if self._position() is not None:
            try:
                r["6_close_after_legs"] = {"closed": True,
                                           "order": self.b.close_position(self.symbol)}
            except BrokerError as exc:
                r["6_close_after_legs"] = {"closed": False, **_err(exc)}

    def _cancel(self, order_id: str, key: str) -> None:
        try:
            self.b.cancel_order(order_id)
            self.report[key] = {"canceled": True}
        except BrokerError as exc:
            self.report[key] = {"canceled": False, **_err(exc)}

    def cleanup(self) -> bool:
        """Cancel the symbol's open orders, close its position, and check it is flat.
        Never raises; returns True when flat with no open orders in the symbol."""
        notes: list[str] = []
        try:
            for o in self._mine(self.b.list_open_orders()):
                for leg in o.get("legs") or []:
                    try:
                        self.b.cancel_order(leg["id"])
                    except BrokerError as exc:
                        notes.append(f"cancel leg {leg['id']}: {exc}")
                try:
                    self.b.cancel_order(o["id"])
                except BrokerError as exc:
                    notes.append(f"cancel {o['id']}: {exc}")
            self._wait(lambda: not self._mine(self.b.list_open_orders()), LEGS_WAIT_S)
            if self._position() is not None:
                try:
                    self.b.close_position(self.symbol)
                except BrokerError as exc:
                    notes.append(f"close: {exc}")
            flat = self._wait(lambda: self._position() is None
                              and not self._mine(self.b.list_open_orders()), FLAT_WAIT_S)
        except BrokerError as exc:
            notes.append(f"cleanup: {exc}")
            flat = False
        self.report["cleanup"] = {"flat": flat, "notes": notes}
        return flat


def live_quote(symbol: str) -> Decimal:
    snap = fetch_snapshots([symbol]).get(symbol) or {}
    ask = (snap.get("latestQuote") or {}).get("ap")
    if isinstance(ask, bool) or not isinstance(ask, (int, float, str)):
        raise BrokerError(f"no live ask for {symbol}")
    d = Decimal(str(ask)).quantize(CENT)
    if not d.is_finite() or d < 1:
        raise BrokerError(f"no usable live ask for {symbol}")
    return d


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
    try:
        b = broker if broker is not None else AlpacaPaperBroker(load_limits())
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
    except Exception as exc:  # noqa: BLE001 - report it, then always clean up
        probe.report["stopped"] = (str(exc) if isinstance(exc, BrokerError)
                                   else type(exc).__name__)
    flat = probe.cleanup()
    text = json.dumps(probe.report, indent=1, default=str)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    if not flat:
        print(f"ALERT: the paper account is NOT flat in {symbol}: check it now",
              file=sys.stderr)
        return NOT_FLAT
    return OK


if __name__ == "__main__":
    sys.exit(main())
