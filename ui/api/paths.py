"""Where the UI reads from: the pipeline's own default locations, or test folders.

STATE_DIR is pipeline.budget.STATE_DIR (journal/state), defined here so the API never imports
pipeline.budget: that module imports tools.scan, which loads the network module tools.http
(ui/tests/test_state_and_config.py checks the two stay equal).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from journal.log import DEFAULT_PATH as JOURNAL_PATH
from tools.config import CONFIG_DIR

STATE_DIR = Path(JOURNAL_PATH).parent / "state"


@dataclass(frozen=True)
class Paths:
    journal: Path
    state_dir: Path
    config_dir: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(Path(JOURNAL_PATH), STATE_DIR, Path(CONFIG_DIR))
