"""
execution 层合并模块
====================
本模块合并自 scripts/trade_lifecycle.py 与 scripts/position_tracker.py，
构成 execution 层的两个核心子模块：

  1. matcher（FIFO 配对）：TradeLifecycle 管理 open/closed 腿队列，
     负责跨日 FIFO 配对结算、持仓时长跟踪、最大偏移跟踪、超时腿标记。
  2. portfolio（分仓）：position_tracker 提供 positions.json 的唯一读写入口，
     管理底仓 / T+1 锁定 / 今日 T 状态，所有写操作加文件锁。

FIFO 跨日配对不变量（顶层约束）：
  - matcher 的 FIFO 队列生命周期 = 整个回测区间，禁止按日 reset()。
  - 跨日未配对腿通过 initial_open_legs 在交易日之间传递
    （export_open_legs / import_open_legs 完成跨日延续）。
  - 同方向腿不配对（sell 配 buy，buy 配 sell）。
"""
from __future__ import annotations

# ── imports from trade_lifecycle ──
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── imports from position_tracker ──
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterator


# ═══ execution: trade_lifecycle（FIFO 配对 + 持仓时长） ═══
# 顶部不变量：matcher 的 FIFO 队列生命周期 = 整个回测区间，禁止按日 reset()。
#            跨日未配对腿通过 initial_open_legs 在交易日之间传递。
#
# 原始模块文档（scripts/trade_lifecycle.py）：
#   交易生命周期管理（P0-2 整改）
#   将 backtest_t_strategy.py 中隐式的 open_legs 列表改为明确的交易生命周期：
#     candidate -> filled -> open -> paired / stopped / expired
#   核心职责：
#     - FIFO 配对结算（跨日连续，不按日重置）
#     - 持仓时长跟踪（holding_bars）
#     - 最大有利/不利偏移跟踪（max_favorable / max_adverse）
#     - 超时腿标记为 expired
#     - 未配对敞口浮盈浮亏计算
#   注意：本模块只负责交易生命周期管理，不判断信号好坏、不修改仓位。
class LegStatus(str, Enum):
    """交易腿生命周期状态。"""
    OPEN = "open"          # 已成交，等待配对
    PAIRED = "paired"      # 已完成 FIFO 配对
    STOPPED = "stopped"    # 止损/止盈强制平仓
    EXPIRED = "expired"    # 超时未配对，标记过期（进入风险报告）


@dataclass
class TradeLeg:
    """单笔交易腿（一次买入或卖出成交）。"""
    direction: str                   # "buy" / "sell"
    shares: int                      # 成交股数
    fill_price: float                # 成交价（含滑点）
    fill_time: str                   # 成交时间 HH:MM
    fill_date: str                   # 成交日期 YYYY-MM-DD
    fill_bar_idx: int                # 成交时的 K 线索引
    cost: float = 0.0                # 单笔交易成本
    status: LegStatus = LegStatus.OPEN
    paired_pnl: float = 0.0          # 配对盈亏（配对后填入）
    holding_bars: int = 0            # 持仓时长（K 线数）
    max_favorable: float = 0.0       # 最大有利偏移（正数）
    max_adverse: float = 0.0         # 最大不利偏移（正数）
    expire_bar_idx: Optional[int] = None  # 过期时的 K 线索引
    open_vwap_dev: Optional[float] = None  # 开仓时刻的 vwap_dev（方案C1：用于动态平仓阈值计算）
    stop_fill_price: Optional[float] = None  # 方案C2：止损实际成交价（触发价位，非 bar.close）

    def to_dict(self) -> dict:
        """转换为字典（兼容旧 open_legs 格式）。

        P0-2 整改（2026-07-24）：补 open_vwap_dev 字段，确保跨日延续时
        方案C1动态平仓阈值不丢失（否则退化为固定 floor 0.8%，过早平仓）。
        """
        return {
            "direction": self.direction,
            "shares": self.shares,
            "fill_price": self.fill_price,
            "time": self.fill_time,
            "date": self.fill_date,
            "cost": self.cost,
            "status": self.status.value,
            "paired_pnl": round(self.paired_pnl, 4),
            "holding_bars": self.holding_bars,
            "max_favorable": round(self.max_favorable, 4),
            "max_adverse": round(self.max_adverse, 4),
            "open_vwap_dev": self.open_vwap_dev,
            "stop_fill_price": self.stop_fill_price,
            "expire_bar_idx": self.expire_bar_idx,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TradeLeg":
        """从字典创建（兼容旧 open_legs 格式）。

        P0-2 整改（2026-07-24）：读回 open_vwap_dev，保持跨日腿动态平仓阈值连续。
        旧格式无此字段时退化为 None（_compute_pairing_threshold 会退化为固定 floor）。
        """
        return cls(
            direction=d["direction"],
            shares=d["shares"],
            fill_price=d["fill_price"],
            fill_time=d.get("time", ""),
            fill_date=d.get("date", ""),
            fill_bar_idx=d.get("fill_bar_idx", 0),
            cost=d.get("cost", 0.0),
            open_vwap_dev=d.get("open_vwap_dev"),
            stop_fill_price=d.get("stop_fill_price"),
            expire_bar_idx=d.get("expire_bar_idx"),
        )


class TradeLifecycle:
    """
    交易生命周期管理器。

    核心规则：
      - FIFO 配对队列贯穿整个回测区间，不按日重置
      - 同方向腿不配对（sell 配 buy，buy 配 sell）
      - 超过 max_holding_bars 的腿标记为 expired
      - 每根 K 线更新持仓时长和最大偏移
    """

    def __init__(self, max_holding_bars: int = 12):
        """
        :param max_holding_bars: 单笔最大持仓 K 线数，超过则标记 expired
        """
        self.max_holding_bars = max_holding_bars
        self.open_legs: list[TradeLeg] = []      # 等待配对的腿
        self.closed_legs: list[TradeLeg] = []     # 已配对/过期/止损的腿
        self.all_trades: list[dict] = []          # 所有成交记录（含配对状态）

    # ── 添加成交 ──
    def add_fill(
        self,
        direction: str,
        shares: int,
        fill_price: float,
        fill_time: str,
        fill_date: str,
        fill_bar_idx: int,
        cost: float = 0.0,
        open_vwap_dev: Optional[float] = None,
    ) -> dict:
        """
        添加一笔成交，尝试 FIFO 配对。

        open_vwap_dev: 开仓时刻的 vwap_dev（仅在新开 open leg 时传入，
                      用于后续平仓的动态阈值计算）。平仓配对时可不传。
        :return: 成交记录 dict（含 paired/pnl 字段）
        """
        pair_pnl = 0.0
        paired = False
        remaining = shares

        # FIFO 配对：与最早的相反方向 open leg 配对
        while remaining > 0 and self.open_legs:
            earliest = self.open_legs[0]
            if earliest.direction == direction:
                break  # 同方向不配对

            paired_shares = min(remaining, earliest.shares)

            # 配对 PnL = (卖价 - 买价) × 配对股数
            if direction == "sell":
                sell_price = fill_price
                buy_price = earliest.fill_price
            else:
                sell_price = earliest.fill_price
                buy_price = fill_price

            leg_pnl = (sell_price - buy_price) * paired_shares
            pair_pnl += leg_pnl
            earliest.paired_pnl += leg_pnl

            earliest.shares -= paired_shares
            remaining -= paired_shares

            if earliest.shares <= 0:
                earliest.status = LegStatus.PAIRED
                self.closed_legs.append(self.open_legs.pop(0))

            paired = True

        # 未配对的部分作为新的 open leg
        if remaining > 0:
            new_leg = TradeLeg(
                direction=direction,
                shares=remaining,
                fill_price=fill_price,
                fill_time=fill_time,
                fill_date=fill_date,
                fill_bar_idx=fill_bar_idx,
                cost=cost,
                open_vwap_dev=open_vwap_dev,
            )
            self.open_legs.append(new_leg)

        trade_record = {
            "time": fill_time,
            "date": fill_date,
            "direction": direction,
            "shares": shares,
            "fill_price": round(fill_price, 4),
            "cost": round(cost, 4),
            "pnl": round(pair_pnl, 4),
            "paired": paired,
            "holding_bars": 0,
            "status": "paired" if paired else "open",
        }
        self.all_trades.append(trade_record)
        return trade_record

    # ── 更新持仓状态 ──
    def update_holding(self, bar_idx: int, current_price: float,
                       bar_low: Optional[float] = None,
                       bar_high: Optional[float] = None) -> None:
        """
        每根 K 线调用一次，更新所有 open leg 的持仓时长和最大偏移。

        方案C2（2026-07-24）：max_adverse / max_favorable 改用**盘中极值**
        （bar.low / bar.high）而非收盘价，使止损触发反映日内真实穿透，
        配合 check_stop_loss 按触发价成交，实现"盘中穿透即触发"。
        bar_low / bar_high 缺失时退化为收盘价口径（向后兼容）。
        """
        for leg in self.open_legs:
            leg.holding_bars = bar_idx - leg.fill_bar_idx

            if leg.direction == "buy":
                favorable = current_price - leg.fill_price
                adverse = leg.fill_price - current_price
                if bar_high is not None:
                    favorable = max(favorable, bar_high - leg.fill_price)
                if bar_low is not None:
                    adverse = max(adverse, leg.fill_price - bar_low)
            else:
                favorable = leg.fill_price - current_price
                adverse = current_price - leg.fill_price
                if bar_low is not None:
                    favorable = max(favorable, leg.fill_price - bar_low)
                if bar_high is not None:
                    adverse = max(adverse, bar_high - leg.fill_price)

            leg.max_favorable = max(leg.max_favorable, favorable)
            leg.max_adverse = max(leg.max_adverse, adverse)

    # ── 检查超时 ──
    def check_expiry(self, bar_idx: int) -> list[TradeLeg]:
        """
        检查超时腿，标记为 expired 并移入 closed_legs。

        :return: 本次过期的腿列表
        """
        expired = []
        remaining_open = []
        for leg in self.open_legs:
            if leg.holding_bars >= self.max_holding_bars:
                leg.status = LegStatus.EXPIRED
                leg.expire_bar_idx = bar_idx
                self.closed_legs.append(leg)
                expired.append(leg)
            else:
                remaining_open.append(leg)
        self.open_legs = remaining_open
        return expired

    # ── 检查止损（移动止盈 + 固定止损兜底）──
    def check_stop_loss(
        self,
        bar_idx: int,
        bar: dict,
        stop_loss_ratio: float = 0.015,
        trailing_ratio: float = 0.5,
    ) -> list[TradeLeg]:
        """
        检查移动止损（移动止盈 + 固定止损兜底）。

        1. 有过盈利（max_favorable > 0）：从最高点回撤 trailing_ratio(0.5) 触发移动止盈，
           保住至少一半利润。盘中穿透即触发（bar.low/high），成交价 = 止损线。
        2. 从未盈利：固定止损 max_adverse >= fill_price × stop_loss_ratio(1.5%) 防大亏。

        v4实验（分离止盈止损，trailing=0）失败：平仓信号在5min不可靠触发，
        盈利腿变超时/止损，胜率从82%暴跌到36%。故恢复移动止盈。

        :param bar_idx: 当前 K 线索引
        :param bar: 当前 K 线（需含 low/high）
        :param stop_loss_ratio: 固定止损比例（默认 1.5%，仅 max_favorable==0 时生效）
        :param trailing_ratio: 移动止盈回撤比例（默认 0.5，从最高点回撤50%触发）
        :return: 本次止损的腿列表
        """
        if stop_loss_ratio <= 0 and trailing_ratio <= 0:
            return []
        stopped = []
        remaining_open = []
        bar_low = bar.get("low")
        bar_high = bar.get("high")

        for leg in self.open_legs:
            should_stop = False
            stop_fill = 0.0

            if leg.max_favorable > 0 and trailing_ratio > 0:
                # 移动止损：从最大有利偏移回撤超过 trailing_ratio
                retained = leg.max_favorable * (1 - trailing_ratio)
                if leg.direction == "buy":
                    stop_line = leg.fill_price + retained
                    if bar_low is not None and bar_low <= stop_line:
                        should_stop = True
                        stop_fill = stop_line
                else:  # sell
                    stop_line = leg.fill_price - retained
                    if bar_high is not None and bar_high >= stop_line:
                        should_stop = True
                        stop_fill = stop_line
            else:
                # 固定止损：从未盈利时防大亏
                threshold = abs(leg.fill_price) * stop_loss_ratio
                if leg.max_adverse >= threshold:
                    should_stop = True
                    if leg.direction == "buy":
                        stop_fill = leg.fill_price - threshold
                    else:
                        stop_fill = leg.fill_price + threshold

            if should_stop:
                leg.status = LegStatus.STOPPED
                leg.expire_bar_idx = bar_idx
                if leg.direction == "buy":
                    stop_pnl = (stop_fill - leg.fill_price) * leg.shares
                else:
                    stop_pnl = (leg.fill_price - stop_fill) * leg.shares
                leg.stop_fill_price = stop_fill
                leg.paired_pnl = stop_pnl
                close_dir = "sell" if leg.direction == "buy" else "buy"
                self.all_trades.append({
                    "time": f"stop@bar{bar_idx}",
                    "date": leg.fill_date,
                    "direction": close_dir,
                    "shares": leg.shares,
                    "fill_price": round(stop_fill, 4),
                    "cost": 0.0,
                    "pnl": round(stop_pnl, 4),
                    "paired": True,
                    "holding_bars": leg.holding_bars,
                    "status": "stopped",
                })
                self.closed_legs.append(leg)
                stopped.append(leg)
            else:
                remaining_open.append(leg)
        self.open_legs = remaining_open
        return stopped

    # ── 未配对敞口浮盈浮亏 ──
    def unrealized_pnl(self, current_price: float) -> float:
        """
        计算所有 open leg 的浮盈浮亏（不含平仓成本和滑点）。

        买腿：(当前价 - 成本价) × 股数
        卖腿：(成本价 - 当前价) × 股数

        P1-2 语义说明（2026-07-24）：
          - 本方法仅遍历 self.open_legs，**不含已 expired 的腿**
            （check_expiry 已把超时腿移入 closed_legs）。
          - 若需 expired 腿的真实盈亏，从 closed_legs 中筛 LegStatus.EXPIRED
            或从 risk_events 提取（backtest.py 已在 risk_events 累计
            expired_legs_real_pnl，最终计入 net_pnl_with_unrealized）。
          - 本方法不含平仓成本和滑点；如需含成本口径，用
            backtest.compute_unrealized_pnl(open_legs, last_close, cost_model)。
        """
        total = 0.0
        for leg in self.open_legs:
            if leg.direction == "buy":
                total += (current_price - leg.fill_price) * leg.shares
            else:
                total += (leg.fill_price - current_price) * leg.shares
        return round(total, 4)

    # ── 统计 ──
    @property
    def open_legs_count(self) -> int:
        return len(self.open_legs)

    @property
    def paired_count(self) -> int:
        return sum(1 for t in self.all_trades if t.get("paired"))

    @property
    def total_pnl(self) -> float:
        """已配对的总盈亏。"""
        return round(sum(t.get("pnl", 0) for t in self.all_trades), 4)

    # ── 跨日延续 ──
    def export_open_legs(self) -> list[dict]:
        """导出 open legs（用于跨日延续）。"""
        return [leg.to_dict() for leg in self.open_legs]

    def import_open_legs(self, legs: list[dict]) -> None:
        """
        导入 open legs（跨日延续）。
        调整 fill_bar_idx 使 holding_bars 跨日连续：设为 -prev_holding_bars，
        这样 update_holding(0) 时 holding_bars = 0 - (-prev) = prev。
        """
        imported = []
        for d in legs:
            leg = TradeLeg.from_dict(d)
            prev_holding = d.get("holding_bars", 0)
            leg.fill_bar_idx = -prev_holding  # 跨日延续：holding_bars 从上次结束处继续
            imported.append(leg)
        self.open_legs = imported


# ═══ execution: position_tracker（持仓状态 + T+1 锁定） ═══
#
# 原始模块文档（scripts/position_tracker.py）：
#   L5 持仓状态追踪器
#   positions.json 的唯一读写入口。所有写操作加文件锁，避免手动更新
#   与脚本自动更新并发覆盖。
#
#   positions.json 结构 (Single Source of Truth):
#   {
#     "600xxx.SH": {
#       "base_shares": 3000,           # 底仓股数（T+1已解锁，可卖）
#       "avg_cost": 12.35,             # 底仓成本价
#       "entry_date": "2026-07-15",
#       "sector_tag": "机器人概念",       # 关联 L2 题材（可选）
#       "t_eligible": true,            # 是否允许做T
#       "today_t_state": {
#         "locked_shares": 0,          # 今日新买入、当天不可卖的股份数
#         "t_trades_today": 0,         # 今日已做T次数
#         "net_position_delta": 0      # 相对底仓的净增减
#       }
#     }
#   }
#
#   独立性：本模块不依赖 L1/L2/L3/L4。positions.json 是 L5 唯一硬依赖。

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # src/at0/ -> src/ -> 项目根
POSITIONS_FILE = PROJECT_ROOT / "data" / "positions.json"
LOCK_FILE = POSITIONS_FILE.with_suffix(".json.lock")


# ═══════════════════════════════════════════════════════════════
# 文件锁（跨平台）
# ═══════════════════════════════════════════════════════════════
@contextmanager
def _file_lock(lock_path: Path = LOCK_FILE, timeout: float = 5.0) -> Iterator[None]:
    """
    跨平台文件锁。Windows 用 msvcrt.locking，Linux/Mac 用 fcntl.flock。
    超时未获取锁则抛出 TimeoutError。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, IOError):
                if time.time() > deadline:
                    raise TimeoutError(f"file_lock timeout: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


# ═══════════════════════════════════════════════════════════════
# 读写 API
# ═══════════════════════════════════════════════════════════════
def load_positions(path: Path = POSITIONS_FILE) -> dict:
    """加载所有持仓状态。文件不存在或损坏返回 {}。"""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] load_positions failed: {e}", file=sys.stderr)
    return {}


def save_positions(positions: dict, path: Path = POSITIONS_FILE) -> None:
    """原子写入持仓状态（加文件锁）。仅用于一次性覆盖写，不涉及读-改-写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock():
        # 原子写：先写临时文件，再 rename
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def _atomic_update(mutate_fn, path: Path = POSITIONS_FILE) -> None:
    """
    原子读-改-写：在同一个文件锁内完成 load → mutate → save。

    P0-3 修复：apply_t_trade / reset_today_state / set_t_eligible 之前
    是 load_positions()（无锁）→ 改 dict → save_positions()（有锁），
    锁只包住了写，读-改-写窗口期内并发调用会互相覆盖丢失更新。

    本函数把整个读-改-写包在同一个 _file_lock() 块里，mutate_fn 在内存中
    修改 positions dict，修改完成后在同一锁内写入文件。

    参数:
      mutate_fn(positions: dict) -> None: 在内存中修改 positions dict
    """
    with _file_lock():
        positions = load_positions(path)
        mutate_fn(positions)
        # 原子写（不再调用 save_positions，因为已经在锁内，避免重入死锁）
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def get_position(code: str, path: Path = POSITIONS_FILE) -> Optional[dict]:
    """读取单只股票的持仓状态。"""
    return load_positions(path).get(code)


def get_sellable_shares(code: str, path: Path = POSITIONS_FILE) -> int:
    """
    计算当前可卖股份数 = base_shares - today_t_state.locked_shares。
    T+1 约束的硬性体现：今日新买的股份当天不可卖。
    """
    pos = get_position(code, path)
    if not pos:
        return 0
    base = int(pos.get("base_shares", 0))
    locked = int(pos.get("today_t_state", {}).get("locked_shares", 0))
    return max(0, base - locked)


def get_t_trades_today(code: str, path: Path = POSITIONS_FILE) -> int:
    """读取今日已做T次数。"""
    pos = get_position(code, path)
    if not pos:
        return 0
    return int(pos.get("today_t_state", {}).get("t_trades_today", 0))


def get_net_position_delta(code: str, path: Path = POSITIONS_FILE) -> int:
    """读取相对底仓的净增减（用于尾盘平衡检查）。"""
    pos = get_position(code, path)
    if not pos:
        return 0
    return int(pos.get("today_t_state", {}).get("net_position_delta", 0))


# ═══════════════════════════════════════════════════════════════
# T 操作后状态更新
# ═══════════════════════════════════════════════════════════════
def apply_t_trade(
    code: str,
    direction: str,
    shares: int,
    price: float,
    path: Path = POSITIONS_FILE,
) -> None:
    """
    在一笔 T 交易完成后更新持仓状态。

    direction:
      - "sell"        正T 卖出底仓 / 反T 卖出老仓
                      → locked_shares 不变（卖的是老仓）
                      → net_position_delta -= shares
                      → t_trades_today += 1
      - "buy"         反T 买入 / 正T 买回
                      → locked_shares += shares（T+1 锁定）
                      → net_position_delta += shares
                      → t_trades_today += 1（反T 算一次完整 T；正T 买回也算一次）

    注意：调用方必须先通过 t_risk_guard 校验，本函数不做风控。
    P0-3: 使用 _atomic_update 保证读-改-写原子性，防止并发覆盖。
    """
    if direction not in {"buy", "sell"}:
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction}")
    if shares <= 0:
        raise ValueError(f"shares must be positive, got {shares}")

    def _mutate(positions: dict) -> None:
        if code not in positions:
            raise KeyError(f"position not found: {code}")
        pos = positions[code]
        today = pos.setdefault("today_t_state", {})
        if direction == "buy":
            today["locked_shares"] = int(today.get("locked_shares", 0)) + shares
            today["net_position_delta"] = int(today.get("net_position_delta", 0)) + shares
        else:  # sell
            today["net_position_delta"] = int(today.get("net_position_delta", 0)) - shares
        today["t_trades_today"] = int(today.get("t_trades_today", 0)) + 1
        today["last_trade_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today["last_trade_direction"] = direction
        today["last_trade_shares"] = shares
        today["last_trade_price"] = price

    _atomic_update(_mutate, path)


def reset_today_state(path: Path = POSITIONS_FILE) -> int:
    """
    每个交易日开盘前调用：清零所有持仓的 today_t_state，
    并把昨日的 locked_shares 转入 base_shares（T+1 已解锁）。

    返回重置的持仓数。
    P0-3: 使用 _atomic_update 保证读-改-写原子性。
    """
    today_str = date.today().isoformat()
    count_holder = {"count": 0}

    def _mutate(positions: dict) -> None:
        if not positions:
            return
        for code, pos in positions.items():
            today = pos.get("today_t_state", {})
            # 昨日买入的股份今日解锁，并入 base_shares
            yesterday_locked = int(today.get("locked_shares", 0))
            if yesterday_locked > 0:
                pos["base_shares"] = int(pos.get("base_shares", 0)) + yesterday_locked
            pos["today_t_state"] = {
                "locked_shares": 0,
                "t_trades_today": 0,
                "net_position_delta": 0,
                "reset_date": today_str,
            }
            count_holder["count"] += 1

    _atomic_update(_mutate, path)
    return count_holder["count"]


def set_t_eligible(code: str, eligible: bool, path: Path = POSITIONS_FILE) -> None:
    """手动/外部系统设置 t_eligible 状态（例如 L1/L2 熔断联动）。
    P0-3: 使用 _atomic_update 保证读-改-写原子性。
    """
    def _mutate(positions: dict) -> None:
        if code not in positions:
            return
        positions[code]["t_eligible"] = eligible

    _atomic_update(_mutate, path)


# ═══════════════════════════════════════════════════════════════
# 示例 / 初始化
# ═══════════════════════════════════════════════════════════════
def init_sample_positions(path: Path = POSITIONS_FILE) -> None:
    """初始化示例持仓（用于测试 / 回测样例）。"""
    sample = {
        "600xxx.SH": {
            "base_shares": 3000,
            "avg_cost": 12.35,
            "entry_date": "2026-07-15",
            "sector_tag": "机器人概念",
            "t_eligible": True,
            "today_t_state": {
                "locked_shares": 0,
                "t_trades_today": 0,
                "net_position_delta": 0,
            },
        },
    }
    save_positions(sample, path)
    print(f"[OK] sample positions written to {path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="L5 position tracker CLI")
    parser.add_argument("--init-sample", action="store_true", help="写入示例持仓")
    parser.add_argument("--show", action="store_true", help="打印当前持仓")
    parser.add_argument("--reset-today", action="store_true", help="清零今日 T 状态")
    args = parser.parse_args()

    if args.init_sample:
        init_sample_positions()
    elif args.show:
        positions = load_positions()
        print(json.dumps(positions, ensure_ascii=False, indent=2))
    elif args.reset_today:
        n = reset_today_state()
        print(f"[OK] reset today_t_state for {n} positions")
    else:
        parser.print_help()
