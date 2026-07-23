"""
A-T0 领域对象（domain 层）
===========================
合并 Bar/Signal/Order/Fill/Position/Trade/BacktestResult + enums + errors。

对齐 strategy_optimization_implementation.md v1.1 §3.1：
domain 作为最底层依赖，被 features/strategy/risk/execution/backtest 引用，
本身不依赖任何业务模块。

当前项目领域对象以 dict + dataclass 混合形式分散在各模块中，
本文件提供统一的类型别名与枚举，供跨层引用时使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════════════════════
class Direction(str, Enum):
    """T 操作方向。"""
    BUY = "buy"      # 买入（反T建仓 / 正T买回）
    SELL = "sell"    # 卖出（正T减仓 / 反T买回）


class SignalSide(str, Enum):
    """信号侧别。"""
    REDUCE = "reduce"  # 减仓信号
    ADD = "add"        # 加仓/买回信号


class MarketRegime(str, Enum):
    """市场状态标签（由 features 层产出，strategy 和 risk 都可读）。

    P0-6: regime 归入 features 层，避免 risk 反向依赖 strategy。
    """
    RANGE = "range"          # 震荡盘
    TREND_UP = "trend_up"    # 上升趋势
    TREND_DOWN = "trend_down"  # 下降趋势
    EXTREME = "extreme"      # 极端趋势（ADX极高 + 价格远离VWAP）


class LegStatus(str, Enum):
    """交易腿状态（与 execution.TradeLeg.status 对齐）。"""
    OPEN = "open"
    PAIRED = "paired"
    EXPIRED = "expired"


class CostScenario(str, Enum):
    """成本场景。"""
    OPTIMISTIC = "optimistic"
    BASE = "base"
    PESSIMISTIC = "pessimistic"


# ═══════════════════════════════════════════════════════════════
# 基础数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass
class Bar:
    """单根K线（分钟线）。"""
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0


@dataclass
class Fill:
    """成交记录。"""
    direction: str       # "buy" / "sell"
    shares: int
    price: float
    time: str
    date: str
    bar_idx: int
    cost: float = 0.0


@dataclass
class Trade:
    """配对交易（FIFO 配对结果）。"""
    open_fill: Fill
    close_fill: Fill
    pnl: float
    paired: bool = True


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════
class AT0Error(Exception):
    """A-T0 项目基础异常。"""


class InsufficientDataError(AT0Error):
    """数据不足，无法计算指标。"""


class PositionLockedError(AT0Error):
    """T+1 锁定，持仓不可卖。"""


class RiskVetoError(AT0Error):
    """风控硬否决。"""
