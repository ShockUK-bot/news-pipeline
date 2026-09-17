"""A11 pure functions (no I/O): excursions from bars, guard outcome
classification, post-exit outcome, rate helpers. Tested directly."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

SAVE_R = 0.25          # |delta| in R beyond which a verdict "mattered"


def side_sign(side: str) -> int:
    return -1 if str(side).upper() == "SHORT" else 1


def excursions(bars: list[dict], side: str, entry_px: float, r_unit: float,
               start: Optional[datetime] = None, end: Optional[datetime] = None) -> dict:
    """MAE / MFE in R over bars[start..end] (inclusive), plus the favourable
    extreme price. bars: [{ts, open, high, low, close}] in time order."""
    m = side_sign(side)
    mae = mfe = 0.0
    fav_px = entry_px
    n = 0
    for b in bars:
        if start is not None and b["ts"] < start:
            continue
        if end is not None and b["ts"] > end:
            break
        n += 1
        win = b["high"] if m > 0 else b["low"]
        lose = b["low"] if m > 0 else b["high"]
        f = m * (win - entry_px) / r_unit
        a = -m * (lose - entry_px) / r_unit
        if f > mfe:
            mfe, fav_px = f, win
        mae = max(mae, a)
    return {"mae_r": round(mae, 3), "mfe_r": round(mfe, 3), "fav_px": fav_px, "bars": n}


def realized_r(realized_pnl: float, qty: int, r_unit: float) -> float:
    return round(float(realized_pnl) / (qty * r_unit), 3) if qty and r_unit else 0.0


def exit_efficiency(real_r: float, mfe_r: float) -> Optional[float]:
    """Realised R as a fraction of the best R seen; None when nothing was ever
    in the money (the schema's 'NULL if MFE<=0')."""
    if mfe_r <= 0:
        return None
    return round(real_r / mfe_r, 4)


def target_price(entry_px: float, side: str, target_fraction: float, magnitude: float) -> float:
    return entry_px * (1 + side_sign(side) * target_fraction * magnitude)


WINNER_R = 1.0         # a HOLD on a position already this far in profit is the ladder's call


def classify_guard(recommended_action: str, delta_r: float, threshold: float = SAVE_R,
                   unrealized_r: Optional[float] = None) -> tuple[str, float]:
    """delta_r = what the position did AFTER the verdict, in R, in the
    position's direction (positive = it went on to gain).

    EXIT / TIGHTEN_STOP verdicts: SAVE when the position then lost more than
    `threshold` R (exiting would have avoided it), SHAKEOUT when it went on to
    gain more than `threshold` R (exiting would have cost it), else NEUTRAL.
    outcome_pnl_r is the R exiting would have gained (positive = save).

    HOLD verdicts: SAVE when holding then earned more than `threshold` R,
    SHAKEOUT when it lost more than `threshold` R (an exit would have been
    right), else NEUTRAL. outcome_pnl_r is the R holding earned."""
    act = str(recommended_action).upper()
    if act in ("EXIT", "TIGHTEN_STOP"):
        pnl = -delta_r
    else:
        pnl = delta_r
        # v0.17.1: a HOLD on a position already >= WINNER_R in profit cannot
        # be a shakeout. The guard's output space is risk-reducing only and
        # the news was benign; whatever the position gives back afterwards
        # is the trailing stop's design (CRWD 09-14: 15 holds at +2.3R, the
        # trail then gave back 2.5R). Journaled NEUTRAL with the give-back
        # kept in outcome_pnl_r so the ladder question stays measurable.
        if unrealized_r is not None and unrealized_r >= WINNER_R:
            return "NEUTRAL", round(pnl, 3)
    if pnl > threshold:
        cls = "SAVE"
    elif pnl < -threshold:
        cls = "SHAKEOUT"
    else:
        cls = "NEUTRAL"
    return cls, round(pnl, 3)


def post_exit_outcome(side: str, exit_px: float, later_px: float, r_unit: float) -> float:
    """R the trade would have made (positive) or avoided losing (negative)
    had it stayed on from the exit to `later_px`."""
    return round(side_sign(side) * (later_px - exit_px) / r_unit, 3) if r_unit else 0.0


def downsample(points: list[tuple], n: int = 24) -> list[list]:
    """Keep about n evenly spaced [ts_iso, price] points, first and last kept."""
    if len(points) <= n:
        idx = range(len(points))
    else:
        step = (len(points) - 1) / (n - 1)
        idx = sorted({int(round(i * step)) for i in range(n)} | {len(points) - 1})
    return [[points[i][0].isoformat() if hasattr(points[i][0], "isoformat") else points[i][0],
             round(float(points[i][1]), 4)] for i in idx]


def rate(num: float, den: float) -> Optional[float]:
    return round(num / den, 6) if den else None
