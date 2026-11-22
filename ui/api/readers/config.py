"""The risk limits, parsed by the project's own validator.

Read through open_shared like every other file the UI reads, then validated by
tools.config.limits_from_dict, so the UI shows exactly what the pipeline would accept or
refuse. An error never echoes the file's contents.

run_day's config (config/run_day.json) is not read yet: its validator lives in
pipeline.budget, which loads the network module tools.http through tools.scan (follow-up).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.config import Limits, limits_from_dict
from ui.api.readers.shared_open import read_bytes_shared


@dataclass(frozen=True)
class Loaded:
    """A parsed config, or why there is none. `source` names the file used."""

    value: Any
    source: str | None
    error: str | None = None

    def view(self) -> dict:
        return {"source": self.source, "ok": self.error is None, "error": self.error}


def _json(path: Path) -> object:
    return json.loads(read_bytes_shared(path).decode("utf-8"))


def read_limits(config_dir: Path) -> Loaded:
    """config/limits.json, or config/limits.example.json when it does not exist (the same
    fallback as tools.config.load_limits)."""
    path = Path(config_dir) / "limits.json"
    if not path.exists():
        path = Path(config_dir) / "limits.example.json"
    try:
        raw = _json(path)
        limits: Limits = limits_from_dict(raw)  # type: ignore[arg-type]
    except FileNotFoundError:
        return Loaded(None, None, "no limits config")
    except Exception:  # noqa: BLE001 - never echo the config's contents
        return Loaded(None, path.name, "limits config is invalid")
    return Loaded(limits, path.name)
