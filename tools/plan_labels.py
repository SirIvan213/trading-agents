"""Plan labels: tool-computed context for one long exit plan, from config thresholds.

The model used to judge "extended" and "tight" with no threshold, and stayed at the minimum
reward multiple. These labels replace that judgement. They are computed in Decimal from the
plan, the session VWAP and the 5-minute ATR, against thresholds in Limits:

  extended          entry - vwap > max_extension_atr_5m * atr_5m   (None if unknown)
  stop_within_max_width  risk_per_share <= max_stop_atr_5m * atr_5m (None if unknown). Named
                    so it cannot be read as "too tight" (#125): true is good.
  The stop's anchor is ExitPlanRecord.structure_level (or_low, or_high, vwap, session_low).
  A structural anchor is still REQUIRED for the higher multiple below (a backstop that is
  always met while entries are last_close, #118), but it is no longer a label (#123): a label
  that is always true carries no information.
                    With entry = last_close (issue #118) the other levels (last_close,
                    session_high) are at or above entry and exit_plan refuses them, so this is
                    a backstop that is True on every plan plan_exit returns today.
  suggested_multiple
      good_setup_multiple when extended is False, stop_within_max_width is True, the stop
      sits under one of STRUCTURAL_LEVELS
      and the 5-minute ATR is NOT provisional; otherwise the minimum multiple,
      max(min_reward_to_risk, 1). Unknown labels never earn the higher multiple, and neither
      does a provisional ATR: early opening bars are wide, so it tends to overstate the ATR,
      which makes `extended` less likely and `stop_within_max_width` more likely (fails
      closed).

The suggested multiple is a CEILING: plan_exit refuses a reward multiple above it. Labels never
change a stop, a size or a risk limit, and nothing here reads model text.
"""
from __future__ import annotations

from decimal import Decimal

from tools.config import Limits

STRUCTURAL_LEVELS = frozenset({"or_low", "or_high", "vwap", "session_low"})
MIN_R = Decimal("1")


def _multiple_text(value: Decimal) -> str:
    """'2' for 2, '1.5' for 1.50: the spelling of the plan_exit menu."""
    return format(value.normalize(), "f")


def min_multiple(limits: Limits) -> Decimal:
    """The multiple exit_plan uses by default: max(min_reward_to_risk, 1)."""
    return max(limits.min_reward_to_risk, MIN_R)


def plan_labels(
    *,
    entry: Decimal,
    risk_per_share: Decimal,
    vwap: Decimal | None,
    atr_5m: Decimal | None,
    atr_5m_provisional: bool,
    structure_level: str | None,
    limits: Limits,
) -> dict:
    """Return {"extended", "stop_within_max_width", "suggested_multiple",
    "atr_5m_provisional"} for a long plan. JSON-serializable (bools, None and a string).

    `atr_5m` None or not positive makes extended and stop_within_max_width None; `vwap` None
    makes
    extended None. A boundary value is not extended (strictly greater than the threshold) and
    is tight (at or below the threshold).
    """
    usable = atr_5m is not None and atr_5m > 0
    extended: bool | None = None
    tight: bool | None = None
    if usable:
        assert atr_5m is not None  # for the type checker
        if vwap is not None:
            extended = (entry - vwap) > limits.max_extension_atr_5m * atr_5m
        tight = risk_per_share <= limits.max_stop_atr_5m * atr_5m
    structural = structure_level in STRUCTURAL_LEVELS
    provisional = bool(atr_5m_provisional) if usable else False
    floor = min_multiple(limits)
    if extended is False and tight is True and structural and not provisional:
        suggested = max(limits.good_setup_multiple, floor)
    else:
        suggested = floor
    return {
        "extended": extended,
        "stop_within_max_width": tight,
        "suggested_multiple": _multiple_text(suggested),
        "atr_5m_provisional": provisional,
    }
