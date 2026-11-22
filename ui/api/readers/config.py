"""The risk limits and run_day's caps, read by the pipeline's own loaders.

  - limits: tools.config.load_limits, on config/limits.json or (when it does not exist)
    config/limits.example.json, the same fallback load_limits uses; the source is shown;
  - caps: pipeline.budget.load_run_day_config on config/run_day.json (no fallback: run_day
    refuses to start without it), shown next to the hard ceilings (pipeline.budget.CEILINGS).

So the UI shows exactly what the pipeline would accept or refuse. Nothing in the pipeline
writes these files, so the loaders' ordinary reads never block a writer (state files, which
the pipeline does replace, are read with open_shared). From pipeline.budget only
load_run_day_config and CEILINGS are imported (tests/test_ui_readonly.py): the UI never
constructs a DailyBudget or takes its lock. An error never echoes a file's contents.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.budget import CEILINGS, load_run_day_config
from tools.config import load_limits


@dataclass(frozen=True)
class Loaded:
    """A parsed config, or why there is none. `source` names the file used."""

    value: Any
    source: str | None
    error: str | None = None

    def view(self) -> dict:
        return {"source": self.source, "ok": self.error is None, "error": self.error}


def read_limits(config_dir: Path) -> Loaded:
    """config/limits.json, or config/limits.example.json when it does not exist."""
    path = Path(config_dir) / "limits.json"
    if not path.exists():
        path = Path(config_dir) / "limits.example.json"
    try:
        limits = load_limits(path)
    except FileNotFoundError:
        return Loaded(None, None, "no limits config")
    except OSError as exc:
        return Loaded(None, path.name, f"limits config is unreadable ({type(exc).__name__})")
    except Exception:  # noqa: BLE001 - never echo the config's contents
        return Loaded(None, path.name, "limits config is invalid")
    return Loaded(limits, path.name)


def read_run_day_config(config_dir: Path) -> Loaded:
    """config/run_day.json, through run_day's own loader and validator."""
    path = Path(config_dir) / "run_day.json"
    if not path.exists():
        return Loaded(None, None, "config/run_day.json is missing: run_day refuses to start")
    try:
        cfg = load_run_day_config(path)
    except Exception:  # noqa: BLE001 - never echo the config's contents
        return Loaded(None, path.name, "run_day config is invalid: run_day refuses to start")
    return Loaded(cfg, path.name)


def ceilings() -> dict[str, int]:
    """A copy of the hard daily ceilings (CLAUDE.md rule 6), never the module's own dict."""
    return dict(CEILINGS)
