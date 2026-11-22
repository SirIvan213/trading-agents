"""run_day's config and its daily budget: the four caps of CLAUDE.md rule 6 (#156, #157).

The standing exception in CLAUDE.md rule 6 pre-approves run_day's Claude, FMP and Alpha
Vantage calls only while all four daily caps are enforced in code:

  - at most 10 decisions per New York day, counted when a decision starts;
  - at most 600,000 tokens per New York day (input, output, cache reads and cache writes,
    across all agents, retries included);
  - FMP: at most 3 requests per New York day, retries and failures included;
  - Alpha Vantage: at most 5 calls per New York day, retries and failures included.

Config: `config/run_day.json` (git-ignored; copy `config/run_day.example.json`). There is no
fallback to the example: a missing file, or a cap that is missing, not a positive whole
number, or above its CLAUDE.md ceiling (CEILINGS), refuses to start. Raising a cap needs
Ivan's explicit approval, so the ceilings are fixed in code, not read from config.

Budget: one JSON file per New York day, `journal/state/budget-YYYY-MM-DD.json`, read and
written under a cross-process file lock, so a restarted run_day continues the day's counts
and two processes can never both take the last unit of a cap. Fail closed:
  - a budget file that cannot be read or does not validate counts as every cap reached;
  - a budget file that is missing although the day already started (the journal shows a
    `run_day_start` today, or this object created it) counts as every cap reached, never as
    a fresh day.
Nothing carries over between days, and nothing here ever raises a cap.

Tokens are reserved before each API call (its worst case) and settled to the call's actual
usage when it returns; a reservation that is never settled (a timeout, a crash) stays
counted. So the day's tokens used plus reserved never exceed the cap (#157).
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

from pipeline.filelock import LockTimeout, exclusive_file_lock
from tools.atomic_write import atomic_write_text
from tools.config import CONFIG_DIR
from tools.symbols import validate_symbols  # not tools.scan: it loads tools.http (#221)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "journal" / "state"
CONFIG_PATH = CONFIG_DIR / "run_day.json"

# CLAUDE.md rule 6's caps. A config value may be lower, never higher.
CEILINGS = {"max_decisions": 10, "max_tokens": 600_000, "max_fmp_requests": 3,
            "max_av_calls": 5}
MAX_UNIVERSE = 200  # symbols in the scan universe (two snapshot requests)
MAX_CANDIDATES_PER_CYCLE = 2  # Ivan's decision (#156): at most 2 decisions per cycle
CONFIG_KEYS = frozenset({"caps", "universe", "scan_time", "cycle_minutes",
                         "candidates_per_cycle"})
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)
REQUESTS = {"fmp": "max_fmp_requests", "av": "max_av_calls"}
LOCK_TIMEOUT_S = 10.0


class RunDayConfigError(ValueError):
    """The run_day config is missing or invalid: run_day refuses to start."""


class CapReached(RuntimeError):
    """A daily cap is reached (or the budget cannot be trusted): nothing more of that kind
    may start today."""


class AlreadyDecided(CapReached):
    """This symbol was already decided today (one decision per symbol per day). Not a cap:
    the day goes on with other symbols."""


@dataclass(frozen=True)
class Caps:
    max_decisions: int
    max_tokens: int
    max_fmp_requests: int
    max_av_calls: int


@dataclass(frozen=True)
class RunDayConfig:
    caps: Caps
    universe: tuple[str, ...]
    scan_time: time = time(9, 40)
    cycle_minutes: int = 5
    candidates_per_cycle: int = 2


def _whole(value: object, name: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RunDayConfigError(f"{name} must be a whole number from {low} to {high}")
    return value


def config_from_dict(d: object) -> RunDayConfig:
    """Validate a run_day config. Raises RunDayConfigError naming the field (never echoing a
    value): every cap is required, a whole number from 1 to its CLAUDE.md ceiling."""
    if not isinstance(d, dict):
        raise RunDayConfigError("run_day config must be a JSON object")
    unknown_keys = set(d) - CONFIG_KEYS
    if unknown_keys:  # a misspelled key must not be silently ignored
        raise RunDayConfigError(f"unknown key(s): {', '.join(sorted(map(str, unknown_keys)))}")
    caps_raw = d.get("caps")
    if not isinstance(caps_raw, dict):
        raise RunDayConfigError("caps is missing")
    caps = {}
    for name, ceiling in CEILINGS.items():
        if name not in caps_raw:
            raise RunDayConfigError(f"caps.{name} is missing")
        caps[name] = _whole(caps_raw[name], f"caps.{name}", low=1, high=ceiling)
    unknown = set(caps_raw) - set(CEILINGS)
    if unknown:
        raise RunDayConfigError(f"unknown cap(s): {', '.join(sorted(unknown))}")
    universe = d.get("universe")
    if not isinstance(universe, list) or not universe:
        raise RunDayConfigError("universe must be a non-empty list of symbols")
    try:
        syms = validate_symbols(universe)
    except ValueError:
        raise RunDayConfigError("universe has an invalid symbol") from None
    if len(syms) > MAX_UNIVERSE:
        raise RunDayConfigError(f"universe has more than {MAX_UNIVERSE} symbols")
    raw_scan = d.get("scan_time", "09:40")
    try:
        hh, mm = str(raw_scan).split(":")
        scan_time = time(int(hh), int(mm))
    except (ValueError, TypeError):
        raise RunDayConfigError("scan_time must be HH:MM") from None
    if not SESSION_OPEN < scan_time < SESSION_CLOSE:
        raise RunDayConfigError("scan_time must be inside the regular session (09:30-16:00)")
    return RunDayConfig(
        caps=Caps(**caps), universe=tuple(syms), scan_time=scan_time,
        cycle_minutes=_whole(d.get("cycle_minutes", 5), "cycle_minutes", low=1, high=60),
        candidates_per_cycle=_whole(d.get("candidates_per_cycle", 2), "candidates_per_cycle",
                                    low=1, high=MAX_CANDIDATES_PER_CYCLE),
    )


def load_run_day_config(path: Path | None = None) -> RunDayConfig:
    """Read config/run_day.json (no fallback to the example). RunDayConfigError otherwise."""
    p = path or CONFIG_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunDayConfigError(f"{p.name} not found: copy run_day.example.json and set "
                                "the universe") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise RunDayConfigError(f"{p.name} is unreadable or not JSON") from None
    return config_from_dict(raw)


# ------------------------------------------------------------------------------------------
# The daily budget
# ------------------------------------------------------------------------------------------

def _empty(day: date) -> dict[str, Any]:
    return {"day": day.isoformat(), "decisions": 0, "symbols": [], "tokens_used": 0,
            "reservations": {}, "fmp_requests": 0, "av_calls": 0, "exhausted": None}


def _valid_state(s: object, day: date) -> bool:
    if not isinstance(s, dict) or s.get("day") != day.isoformat():
        return False
    for k in ("decisions", "tokens_used", "fmp_requests", "av_calls"):
        v = s.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return False
    res = s.get("reservations")
    if not isinstance(res, dict) or any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in res.values()):
        return False
    syms = s.get("symbols")
    return isinstance(syms, list) and all(isinstance(x, str) for x in syms) and \
        (s.get("exhausted") is None or isinstance(s.get("exhausted"), str))


class DailyBudget:
    """The day's four caps, on disk and shared across processes. Every method reads, checks
    and writes under the budget's file lock. Any doubt about the file (unreadable, invalid,
    missing after the day started, lock timeout) raises CapReached: fail closed."""

    def __init__(self, caps: Caps, day: date, state_dir: Path = STATE_DIR) -> None:
        for name, ceiling in CEILINGS.items():  # a hand-built Caps cannot pass CLAUDE.md
            value = getattr(caps, name, None)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise RunDayConfigError(f"caps.{name} must be a whole number from 1 to "
                                        f"{ceiling}")
        self.caps = caps
        self.day = day
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / f"budget-{day.isoformat()}.json"
        self.lock_path = self.state_dir / "budget.lock"
        self._mutex = threading.Lock()  # one thread at a time in this process

    # ---- file access (call under the locks) ----
    def _read(self) -> dict[str, Any]:
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CapReached("daily budget file is missing after the day started") from None
        except (OSError, ValueError, UnicodeDecodeError):
            raise CapReached("daily budget file is unreadable") from None
        if not _valid_state(s, self.day):
            raise CapReached("daily budget file is invalid")
        if s["exhausted"]:
            raise CapReached(f"daily budget is closed: {s['exhausted']}")
        return s

    def _write(self, s: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # atomic, with POSIX-semantics replace and a bounded PermissionError retry on Windows
        # (#213); an OSError after that is CapReached in the callers, exactly as before
        atomic_write_text(self.path, json.dumps(s, sort_keys=True))

    def _locked(self):  # noqa: ANN202
        return exclusive_file_lock(self.lock_path, LOCK_TIMEOUT_S)

    def _update(self, change) -> Any:  # noqa: ANN001
        """Read, apply `change(state) -> result`, write, under both locks. CapReached for a
        lock timeout or an untrustworthy file."""
        with self._mutex:
            try:
                with self._locked():
                    s = self._read()
                    out = change(s)
                    self._write(s)
                    return out
            except LockTimeout:
                raise CapReached("daily budget lock timed out") from None
            except OSError:
                raise CapReached("daily budget file could not be written") from None

    # ---- the day ----
    def open_day(self, *, started_today: bool) -> None:
        """Open today's budget. An existing file continues the day's counts. A missing file
        starts the day at zero, unless the day already started (`started_today`: the
        journal shows a run_day_start today), when the counts are lost: the file is written
        closed and every cap counts as reached."""
        with self._mutex:
            try:
                with self._locked():
                    if self.path.exists():
                        self._read()  # CapReached if unreadable, invalid or closed
                    elif started_today:
                        s = _empty(self.day)
                        s["exhausted"] = "budget file was missing after the day started"
                        self._write(s)
                        raise CapReached(s["exhausted"])
                    else:
                        self._write(_empty(self.day))
            except LockTimeout:
                raise CapReached("daily budget lock timed out") from None
            except OSError:
                raise CapReached("daily budget file could not be written") from None

    def snapshot(self) -> dict[str, Any]:
        """Today's counts, read under the locks and never written (a heartbeat or a check
        must not end the day on a write failure). CapReached if the budget cannot be
        trusted."""
        with self._mutex:
            try:
                with self._locked():
                    return self._read()
            except LockTimeout:
                raise CapReached("daily budget lock timed out") from None
            except OSError:  # the lock file cannot be opened: the budget cannot be trusted
                raise CapReached("daily budget lock could not be opened") from None

    def tokens_committed(self, s: dict[str, Any]) -> int:
        return s["tokens_used"] + sum(s["reservations"].values())

    # ---- decisions ----
    def start_decision(self, symbol: str) -> int:
        """Count one decision for `symbol` (when it starts). Returns the day's count. Raises
        CapReached at the decision cap, when the token cap is already used up, or when
        `symbol` was already decided today (one decision per symbol per day)."""
        sym = symbol.strip().upper()

        def change(s: dict[str, Any]) -> int:
            if s["decisions"] >= self.caps.max_decisions:
                raise CapReached(f"decision cap reached ({self.caps.max_decisions} today)")
            if self.tokens_committed(s) >= self.caps.max_tokens:
                raise CapReached(f"token cap reached ({self.caps.max_tokens} today)")
            if sym in s["symbols"]:
                raise AlreadyDecided(f"{sym} was already decided today")
            s["decisions"] += 1
            s["symbols"].append(sym)
            return s["decisions"]
        return self._update(change)

    def decided(self, symbol: str) -> bool:
        return symbol.strip().upper() in self.snapshot()["symbols"]

    # ---- tokens ----
    def reserve_tokens(self, amount: int) -> str:
        """Reserve `amount` tokens (a call's worst case) before the call. Returns the
        reservation id. Raises CapReached if used + reserved + amount would pass the cap."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError("amount must be a positive whole number")
        rid = uuid.uuid4().hex

        def change(s: dict[str, Any]) -> str:
            if self.tokens_committed(s) + amount > self.caps.max_tokens:
                raise CapReached(
                    f"token cap: {amount} more would pass {self.caps.max_tokens} today")
            s["reservations"][rid] = amount
            return rid
        return self._update(change)

    def settle_tokens(self, rid: str, actual: int) -> None:
        """Replace a reservation with the call's actual tokens (never less than zero). An
        actual above the reservation is still recorded in full: the count is the truth."""
        used = max(int(actual), 0)

        def change(s: dict[str, Any]) -> None:
            s["reservations"].pop(rid, None)
            s["tokens_used"] += used
        self._update(change)

    # ---- FMP and Alpha Vantage requests ----
    def count_request(self, source: str) -> int:
        """Count one request to `source` ("fmp" or "av") BEFORE it is made. Returns the
        day's count. Raises CapReached at the cap (the request must not be made)."""
        key = {"fmp": "fmp_requests", "av": "av_calls"}.get(source)
        if key is None:
            raise ValueError(f"unknown source {source!r}")
        cap = getattr(self.caps, REQUESTS[source])

        def change(s: dict[str, Any]) -> int:
            if s[key] >= cap:
                raise CapReached(f"{source} request cap reached ({cap} today)")
            s[key] += 1
            return s[key]
        return self._update(change)
