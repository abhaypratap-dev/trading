"""Built-in strategy registry. Uploaded/AI-written strategies are registered at runtime."""
from __future__ import annotations

from yonexis.sdk import Strategy

from .first_red_candle_zone import FirstRedCandleZone
from .hari_master_block import HariMasterBlock
from .reversal_rsi import ReversalRSI
from .momentum import Momentum
from .breakout_sr import BreakoutSR
from .scalping_vwap import ScalpingVWAP
from .pivot_points import PivotPoints
from .orb import OpeningRangeBreakout
from .gap_and_go import GapAndGo
from .flag_pattern import FlagPattern
from .ma_crossover import MACrossover
from .pullback import Pullback
from .vwap_reversion import VWAPReversion
from .supertrend_trend import SupertrendTrend

# The professional intraday library (all share sdk.intraday.IntradayStrategy) - used by the Strategy Lab and AI picker.
LIBRARY = [ReversalRSI, Momentum, BreakoutSR, ScalpingVWAP, PivotPoints, OpeningRangeBreakout, GapAndGo, FlagPattern,
           MACrossover, Pullback, VWAPReversion, SupertrendTrend]

REGISTRY: dict[str, type[Strategy]] = {
    FirstRedCandleZone.name: FirstRedCandleZone,
    HariMasterBlock.name: HariMasterBlock,
    **{c.name: c for c in LIBRARY},
}


def register(cls: type[Strategy]) -> None:
    REGISTRY[cls.name] = cls


def get(name: str) -> type[Strategy]:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]
