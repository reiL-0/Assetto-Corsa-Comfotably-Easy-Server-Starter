"""Generic cut policy. Pure decision: the caller owns the last-counted clock."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.live.nascar import CAUTION


class TrackLimitsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    wheels_out: Literal[3, 4] = 3
    min_speed_kmh: float = Field(default=40, ge=0, le=300)
    max_cut_s: float = Field(default=3, ge=0, le=60)
    min_slow_ratio: float = Field(default=0.9, ge=0, le=2)
    post_cut_slowdown: bool = True
    between_s: float = Field(default=3, ge=0, le=60)
    during_caution: bool = True


def count_cut(cfg: TrackLimitsConfig, report: dict, now: float, flag: str,
              last_counted: float | None = None) -> tuple[bool, str]:
    """Thresholds are inclusive; missing facts skip only the filters needing them."""
    if report["wheels"] < cfg.wheels_out:
        return False, "wheels_out"
    vo, vi, vp = (report.get(k) for k in ("vo", "vi", "vp"))
    if cfg.min_speed_kmh and (vo if vo is not None else report["speed"]) < cfg.min_speed_kmh:
        return False, "min_speed"
    if cfg.max_cut_s and report["ms"] > cfg.max_cut_s * 1000:
        return False, "max_cut_s"
    if cfg.min_slow_ratio and vo is not None and vo > 0 and vi is not None and vi / vo < cfg.min_slow_ratio:
        return False, "min_slow_ratio"
    if cfg.post_cut_slowdown and vo is not None and vo > 0 and vi is not None and vp is not None and min(vi, vp) / vo < (cfg.min_slow_ratio or 0.9):
        return False, "post_cut_slowdown"
    if not cfg.during_caution and flag in CAUTION:
        return False, "during_caution"
    if cfg.between_s and last_counted is not None and now - last_counted < cfg.between_s:
        return False, "between_s"
    return True, "counted"
