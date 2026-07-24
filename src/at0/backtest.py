"""
backtest 层合并模块
==================

将 scripts/ 下的四个回测相关脚本合并为 src/at0/backtest.py 单文件，作为
backtest 层的统一入口。原始脚本暂保留在 scripts/ 不动，本文件为包内合并版本。

包含四个职责区（engine + walk_forward + metrics + artifacts）：
  - metrics:      统计口径 + 汇总（原 backtest_metrics.py）
  - engine:       回测引擎主循环（原 backtest_t_strategy.py）
  - artifacts:    run_id 版本化 + 数据指纹（原 run_artifacts.py）
  - walk_forward: 网格搜索 + 滚动样本外验证（原 tune_params.py）

合并说明：
  - 保留全部原始函数/类/常量实现，不做逻辑修改
  - 本地脚本 import 改为包内相对 import（.features/.strategy/.risk/.execution/.data/.cli）
  - 删除 sys.path.insert（包模块不需要）
  - 同文件内调用删除跨文件 import
"""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .features import compute_reference_snapshot, cumulative_vwap
from .strategy import (
    SignalParams, DEFAULT_PARAMS,
    evaluate_reduce_signal, evaluate_add_signal,
)
# P0 整改：引入统一成本模型、交易生命周期、敞口策略
from .risk import (
    RiskParams, DEFAULT_RISK_PARAMS,
    CostModel, ExposurePolicy,
    approve_signal, eod_risk_disposal, EodRiskEvent,
)
from .execution import TradeLifecycle, LegStatus
from .sample_data import PATTERN_GENERATORS, gen_pattern


# ═══ backtest: backtest_metrics（统计口径 + 汇总） ═══


# ═══════════════════════════════════════════════════════════════
# 未配对敞口浮盈浮亏
# ═══════════════════════════════════════════════════════════════
def compute_unrealized_pnl(open_legs: list[dict], last_close: float) -> float:
    """
    计算未配对敞口的浮盈浮亏。

    买入腿（反T先买）：浮盈 = (最后收盘价 - 买入价) × 股数
    卖出腿（正T先卖）：浮盈 = (卖出价 - 最后收盘价) × 股数（卖出了，需要买回）

    与 TradeLifecycle.unrealized_pnl 的口径完全一致。
    """
    total = 0.0
    for leg in open_legs:
        if leg["direction"] == "buy":
            total += (last_close - leg["fill_price"]) * leg["shares"]
        else:  # sell
            total += (leg["fill_price"] - last_close) * leg["shares"]
    return round(total, 2)


# ═══════════════════════════════════════════════════════════════
# 单日统计
# ═══════════════════════════════════════════════════════════════
def compute_daily_stats(
    trades: list[dict],
    net_position_delta: int,
    risk_events: list[dict],
) -> dict:
    """
    计算单日回测统计：胜率、尾盘状态。

    返回:
      {"win_rate": float, "eod_status": str}
    """
    # 仅统计已配对交易的胜率（与 summarize_one_stock 口径一致）
    paired = [t for t in trades if t.get("paired")]
    win_count = sum(1 for t in paired if t.get("pnl", 0) > 0)
    win_rate = win_count / len(paired) if paired else 0.0

    eod_status = "balanced" if net_position_delta == 0 else (
        "net_reduce" if net_position_delta < 0 else "net_add"
    )
    if risk_events:
        has_expired = any(e.get("type") == "expired" for e in risk_events)
        if has_expired:
            eod_status = "has_expired_legs"

    return {"win_rate": win_rate, "eod_status": eod_status}


# ═══════════════════════════════════════════════════════════════
# 单股多日汇总
# ═══════════════════════════════════════════════════════════════
def extract_trades(result: dict) -> list[dict]:
    """从 backtest_multi_day 的 result 提取扁平 trades 列表。"""
    trades = []
    for dr in result.get("daily_results", []):
        for t in dr.get("trades", []):
            trades.append(t)
    return trades


def summarize_one_stock(code: str, result: dict) -> dict:
    """
    单只股票汇总（含跨日配对 + 未配对敞口浮盈浮亏）。

    统计口径：
      - paired_trades: 已配对交易数（paired=True）
      - win_rate: 基于已配对交易
      - net_pnl: 已实现净盈亏 = 毛盈亏 - 总成本
      - net_pnl_with_unrealized: 含未配对浮盈浮亏
    """
    trades = extract_trades(result)
    paired = [t for t in trades if t.get("paired")]
    wins = [t for t in paired if t.get("pnl", 0) > 0]
    losses = [t for t in paired if t.get("pnl", 0) < 0]
    gross_pnl = sum(t.get("pnl", 0) for t in trades)
    total_cost = sum(t.get("cost", 0) for t in trades)
    unrealized = result.get("unrealized_pnl", 0.0)
    final_legs = result.get("final_open_legs_count", 0)
    return {
        "code": code,
        "total_trades": len(trades),
        "paired_trades": len(paired),
        "unpaired_trades": len(trades) - len(paired),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": round(len(wins) / len(paired), 4) if paired else 0.0,
        "gross_pnl": round(gross_pnl, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(gross_pnl - total_cost, 2),
        "unrealized_pnl": round(unrealized, 2),
        "net_pnl_with_unrealized": round(gross_pnl - total_cost + unrealized, 2),
        "final_open_legs_count": final_legs,
    }


# ═══════════════════════════════════════════════════════════════
# 批量聚合
# ═══════════════════════════════════════════════════════════════
def aggregate_batch(
    per_stock: list[dict],
    all_trades_count: int,
) -> dict:
    """
    聚合批量回测整体指标。

    返回 overall dict，字段与 batch_summary.json 的 overall 对齐。
    """
    total_paired = sum(s.get("paired_trades", 0) for s in per_stock)
    total_wins = sum(s.get("win_trades", 0) for s in per_stock)
    total_net = sum(s.get("net_pnl", 0) for s in per_stock)
    total_gross = sum(s.get("gross_pnl", 0) for s in per_stock)
    total_cost = sum(s.get("total_cost", 0) for s in per_stock)
    total_unrealized = sum(s.get("unrealized_pnl", 0) for s in per_stock)
    total_final_legs = sum(s.get("final_open_legs_count", 0) for s in per_stock)
    overall_wr = (total_wins / total_paired) if total_paired else 0.0

    profitable = [s for s in per_stock
                  if "error" not in s and s.get("net_pnl_with_unrealized", s.get("net_pnl", 0)) > 0]
    losing = [s for s in per_stock
              if "error" not in s and s.get("net_pnl_with_unrealized", s.get("net_pnl", 0)) < 0]

    return {
        "stocks": len(per_stock),
        "total_trades": all_trades_count,
        "paired_trades": total_paired,
        "win_trades": total_wins,
        "win_rate": round(overall_wr, 4),
        "gross_pnl": round(total_gross, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(total_net, 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "net_pnl_with_unrealized": round(total_net + total_unrealized, 2),
        "final_open_legs_count": total_final_legs,
        "profitable_stocks": len(profitable),
        "losing_stocks": len(losing),
    }


# ═══════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════
def print_backtest_summary(result: dict) -> None:
    """打印单股多日回测摘要。"""
    print("=" * 60)
    print(f"L5 T+0 回测报告 — {result.get('code', '?')}")
    print("=" * 60)
    print(f"回测天数:     {result.get('total_days', 0)}")
    print(f"总T次数:      {result.get('total_trades', 0)}")
    print(f"日均T次数:    {result.get('avg_trades_per_day', 0):.2f}")
    print(f"累计降成本:   {result.get('total_cost_reduction', 0):.2f} 元")
    print(f"累计交易成本: {result.get('total_cost_paid', 0):.2f} 元")
    print(f"净盈亏:       {result.get('net_pnl', 0):.2f} 元")
    print(f"胜率:         {result.get('win_rate', 0)*100:.1f}%")
    print("=" * 60)


def save_backtest_report(result: dict, output_path: Path) -> None:
    """保存回测报告为 JSON。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


# ═══ backtest: backtest_t_strategy（回测引擎主循环） ═══


# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MINUTE_DATA_DIR = PROJECT_ROOT / "data" / "minute_bars"
BACKTEST_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "backtest"


# ═══════════════════════════════════════════════════════════════
# 回测参数
# ═══════════════════════════════════════════════════════════════
@dataclass
class BacktestParams:
    """回测参数。"""
    # 成本（P0-1: 统一收敛到 CostModel，旧字段保留兼容）
    commission_rate: float = 0.00025        # 佣金万2.5（单边）
    stamp_tax_rate: float = 0.0005          # 印花税 0.05%（卖单）
    slippage: float = 0.001                 # 滑点 0.1%
    cost_model: Optional[CostModel] = None  # P0-1: 统一成本模型（None时用旧字段构造）

    # 持仓
    base_shares: int = 3000                 # 底仓股数
    avg_cost: float = 10.00                 # 底仓成本

    # 信号
    signal_params: SignalParams = field(default_factory=SignalParams)
    risk_params: RiskParams = field(default_factory=RiskParams)

    # 回测
    warmup_bars: int = 30                   # 预热K线数（前30根不产生信号）
    eod_check_bar_idx: int = 200            # 14:50 对应的K线索引（约第200根）

    # 信号约束（防止同方向连发、强制配对闭环）
    cooldown_bars: int = 3                  # 信号触发后N根K线内不再触发同方向信号
    require_opposite_direction: bool = True  # 有未配对腿时只允许反方向信号

    # P0-2: 交易生命周期
    max_holding_bars: int = 12              # 单笔最大持仓K线数，超过标记expired

    # P0-3: 敞口策略
    exposure_policy: Optional[ExposurePolicy] = None  # None时用默认策略

    def get_cost_model(self) -> CostModel:
        """获取统一成本模型（兼容旧字段）。"""
        if self.cost_model is not None:
            return self.cost_model
        return CostModel(
            commission_rate=self.commission_rate,
            stamp_tax_rate=self.stamp_tax_rate,
            slippage_rate=self.slippage,
        )

    def get_exposure_policy(self) -> ExposurePolicy:
        """获取敞口策略（兼容旧字段）。"""
        if self.exposure_policy is not None:
            return self.exposure_policy
        return ExposurePolicy(
            max_holding_bars=self.max_holding_bars,
            require_opposite_direction=self.require_opposite_direction,
        )


# ═══════════════════════════════════════════════════════════════
# 回测状态（单只股票单日）
# ═══════════════════════════════════════════════════════════════
@dataclass
class BacktestState:
    """单只股票单日回测状态。"""
    base_shares: int                        # 底仓股数（不变）
    avg_cost: float                         # 底仓成本（用于最终盈亏计算）
    locked_shares: int = 0                  # 今日新买、T+1锁定
    t_trades_today: int = 0                 # 今日T次数
    net_position_delta: int = 0             # 相对底仓的净增减
    cost_reduction: float = 0.0             # 累计配对结算的 T 收益（元）（P1-3: 改为配对口径）
    total_cost_paid: float = 0.0            # 累计交易成本（元）
    trades: list[dict] = field(default_factory=list)  # 成交记录
    last_signal_bar: int = -1               # 上次信号触发的K线索引（冷却期用）
    last_signal_dir: str = ""               # 上次信号方向 "buy"/"sell"
    # P0-2: 用 TradeLifecycle 替代裸 open_legs 列表
    lifecycle: Optional[TradeLifecycle] = None
    # P0-3: 尾盘风险事件
    risk_events: list[dict] = field(default_factory=list)

    @property
    def open_legs(self) -> list[dict]:
        """兼容旧接口：返回 open_legs 字典列表。"""
        if self.lifecycle:
            return [leg.to_dict() for leg in self.lifecycle.open_legs]
        return []

    @property
    def sellable_shares(self) -> int:
        """当前可卖底仓（T+1约束）。"""
        return max(0, self.base_shares - self.locked_shares)


# ═══════════════════════════════════════════════════════════════
# 成本计算（P0-1: 委托给 CostModel，保留旧签名兼容）
# ═══════════════════════════════════════════════════════════════
def calc_trade_cost(
    direction: str,
    shares: int,
    price: float,
    params: BacktestParams,
) -> float:
    """计算单笔交易成本（P0-1: 委托给 CostModel）。"""
    return params.get_cost_model().calc_cost(direction, shares, price)


def apply_slippage(direction: str, price: float, params: BacktestParams) -> float:
    """应用滑点（P0-1: 委托给 CostModel）。"""
    return params.get_cost_model().fill_price(direction, price)


# ═══════════════════════════════════════════════════════════════
# 单股单日回测
# ═══════════════════════════════════════════════════════════════
def backtest_single_day(
    code: str,
    trading_date: str,
    bars: list[dict],
    prev_close: float,
    params: Optional[BacktestParams] = None,
    l1_systemic_risk: bool = False,
    theme_retreated: bool = False,
    initial_open_legs: Optional[list[dict]] = None,
) -> dict:
    """
    对单只股票单日进行分钟级回测。

    参数:
      code: 股票代码
      trading_date: 交易日 "YYYY-MM-DD"
      bars: 当日1分钟K线（按时间升序）
      prev_close: 昨收
      params: 回测参数
      l1_systemic_risk: 模拟 L1 系统性风险日（仅允许减仓）
      theme_retreated: 模拟 L2 题材退潮（禁止加仓/买回）
      initial_open_legs: 跨日延续的未配对腿（P3-1: 跨日连续配对）

    返回:
    {
        "code": str,
        "date": str,
        "bars_count": int,
        "t_trades": int,
        "cost_reduction": float,       # T操作降低的成本（元）
        "total_cost_paid": float,      # 总交易成本
        "net_pnl": float,              # 净盈亏（成本降低 - 交易成本）
        "win_rate": float,             # 胜率
        "trades": list[dict],          # 成交明细
        "eod_status": str,             # 尾盘状态
        "final_open_legs": list[dict], # P3-1: 日终未配对腿（传给下一日）
        "last_close": float,           # P3-1: 当日收盘价（用于未配对敞口估值）
    }
    """
    params = params or BacktestParams()
    cost_model = params.get_cost_model()
    exposure_policy = params.get_exposure_policy()
    bars_count = len(bars)

    state = BacktestState(
        base_shares=params.base_shares,
        avg_cost=params.avg_cost,
        lifecycle=TradeLifecycle(max_holding_bars=params.max_holding_bars),
    )
    # P3-1: 跨日延续未配对腿（FIFO 配对队列不按日重置）
    if initial_open_legs:
        state.lifecycle.import_open_legs(
            [dict(leg) for leg in initial_open_legs]
        )

    if len(bars) < params.warmup_bars:
        return {
            "code": code,
            "date": trading_date,
            "bars_count": len(bars),
            "t_trades": 0,
            "cost_reduction": 0.0,
            "total_cost_paid": 0.0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "trades": [],
            "eod_status": "insufficient_bars",
            "final_open_legs": state.lifecycle.export_open_legs(),
            "last_close": prev_close,
        }

    # 计算涨跌停价
    limit_up = round(prev_close * 1.1, 2)
    limit_down = round(prev_close * 0.9, 2)

    # 逐根遍历
    for i in range(params.warmup_bars, len(bars)):
        bar = bars[i]
        price = bar["close"]

        # P0-2: 更新持仓时长和最大偏移
        state.lifecycle.update_holding(i, price)

        # P0-2: 检查超时腿
        expired = state.lifecycle.check_expiry(i)
        for exp_leg in expired:
            state.risk_events.append({
                "type": "expired",
                "direction": exp_leg.direction,
                "shares": exp_leg.shares,
                "fill_price": exp_leg.fill_price,
                "holding_bars": exp_leg.holding_bars,
                "bar_idx": i,
                "time": bar.get("time", ""),
            })

        # 涨跌停封板检测
        is_limit_up = abs(price - limit_up) / limit_up < 0.001 if limit_up > 0 else False
        is_limit_down = abs(price - limit_down) / limit_down < 0.001 if limit_down > 0 else False
        recent_bars = bars[max(0, i - 5) : i + 1]
        avg_vol = sum(b["volume"] for b in recent_bars) / len(recent_bars) if recent_bars else 0
        is_limit_up_locked = is_limit_up and avg_vol < 100
        is_limit_down_locked = is_limit_down and avg_vol < 100

        # 跳过一字板
        if i == params.warmup_bars:
            day_high = max(b["high"] for b in bars)
            day_low = min(b["low"] for b in bars)
            day_open = bars[0]["open"]
            if abs(day_high - day_low) / day_open < 0.001:
                break

        # 截至当前K线的bars切片（严格因果）
        bars_up_to_now = bars[: i + 1]

        # 判断是否为平仓评估（根据 open_legs 方向）
        # 有 buy open leg 时，reduce（卖出）是平仓；有 sell open leg 时，add（买入）是平仓
        has_buy_open = any(leg.direction == "buy" for leg in state.lifecycle.open_legs)
        has_sell_open = any(leg.direction == "sell" for leg in state.lifecycle.open_legs)

        # 评估信号
        reduce_sig = evaluate_reduce_signal(
            bars_up_to_now,
            current_price=price,
            prev_close=prev_close,
            is_limit_up_locked=is_limit_up_locked,
            params=params.signal_params,
            is_for_pairing=has_buy_open,
        )
        add_sig = evaluate_add_signal(
            bars_up_to_now,
            current_price=price,
            prev_close=prev_close,
            is_limit_down_locked=is_limit_down_locked,
            theme_retreated=theme_retreated,
            params=params.signal_params,
            is_for_pairing=has_sell_open,
        )

        reduce_ok = reduce_sig.triggered and not is_limit_up_locked
        add_ok = add_sig.triggered and not is_limit_down_locked

        # 约束: cooldown_bars
        if (params.cooldown_bars > 0 and state.last_signal_bar >= 0
                and (i - state.last_signal_bar) < params.cooldown_bars):
            if state.last_signal_dir == "sell":
                reduce_ok = False
            elif state.last_signal_dir == "buy":
                add_ok = False

        # 优先级：减仓 > 加仓（保守）
        if reduce_ok:
            _execute_trade(
                code, trading_date, bar, i, bars_count, "sell", reduce_sig,
                state, params, cost_model, exposure_policy,
                l1_systemic_risk, theme_retreated, prev_close,
            )
            state.last_signal_bar = i
            state.last_signal_dir = "sell"
        elif add_ok:
            _execute_trade(
                code, trading_date, bar, i, bars_count, "buy", add_sig,
                state, params, cost_model, exposure_policy,
                l1_systemic_risk, theme_retreated, prev_close,
            )
            state.last_signal_bar = i
            state.last_signal_dir = "buy"

    # P0-3: 尾盘风险处置（不再只是标记状态）
    last_close = bars[-1]["close"] if bars else prev_close
    open_legs_dicts = state.lifecycle.export_open_legs()
    if open_legs_dicts:
        events = eod_risk_disposal(
            code, trading_date, open_legs_dicts, last_close, state.lifecycle,
        )
        for ev in events:
            state.risk_events.append(ev.to_dict())

    # 统计（P0-4: 委托给 backtest_metrics）
    stats = compute_daily_stats(state.trades, state.net_position_delta, state.risk_events)

    return {
        "code": code,
        "date": trading_date,
        "bars_count": len(bars),
        "t_trades": len(state.trades),
        "cost_reduction": state.cost_reduction,
        "total_cost_paid": state.total_cost_paid,
        "net_pnl": state.cost_reduction - state.total_cost_paid,
        "win_rate": stats["win_rate"],
        "trades": state.trades,
        "eod_status": stats["eod_status"],
        "final_open_legs": state.lifecycle.export_open_legs(),
        "last_close": last_close,
        "risk_events": state.risk_events,
    }


def _execute_trade(
    code: str,
    trading_date: str,
    bar: dict,
    bar_idx: int,
    bars_count: int,
    direction: str,
    signal,
    state: BacktestState,
    params: BacktestParams,
    cost_model: CostModel,
    exposure_policy: ExposurePolicy,
    l1_systemic_risk: bool,
    theme_retreated: bool,
    prev_close: float,
) -> None:
    """
    P0-4: 将 _try_execute 拆为三步 — 风控批准 → 成交模拟 → 配对结算。
    """
    # ── 第1步：风控批准 ──
    ref_price = signal.snapshot.get("vwap") or prev_close
    price = bar["close"]

    decision = approve_signal(
        direction=direction,
        requested_shares=int(state.base_shares * params.risk_params.max_t_size_ratio),
        open_legs=state.lifecycle.export_open_legs(),
        bar_idx=bar_idx,
        bars_count=bars_count,
        policy=exposure_policy,
        sellable_shares=state.sellable_shares,
        t_trades_today=state.t_trades_today,
        max_t_trades_per_day=params.risk_params.max_t_trades_per_day,
        max_t_size_ratio=params.risk_params.max_t_size_ratio,
        base_shares=state.base_shares,
        l1_systemic_risk=l1_systemic_risk,
        theme_retreated=theme_retreated,
    )
    if not decision.approved:
        return

    shares = decision.adjusted_shares
    if shares <= 0:
        return

    # 预期价差检查（使用统一成本模型的净收益率）
    # 平仓（is_pairing）跳过 min_capture_spread：平仓时价格已回归 VWAP 附近，
    # |price - vwap| 小，该检查对平仓无意义（平仓的盈利来自开仓价与平仓价的价差，
    # 不是当前价与 vwap 的偏离）。开仓仍需检查（确保在极端开仓）。
    expected_spread = abs(price - ref_price) / ref_price if ref_price > 0 else 0.0
    if ref_price > 0 and not signal.is_pairing:
        if expected_spread < params.risk_params.min_capture_spread:
            return

    # ── 第2步：成交模拟（CostModel 统一处理滑点+成本）──
    fill_price = cost_model.fill_price(direction, price)
    cost = cost_model.calc_cost(direction, shares, fill_price)

    # 更新持仓状态
    if direction == "buy":
        state.locked_shares += shares
        state.net_position_delta += shares
    else:
        state.net_position_delta -= shares

    state.t_trades_today += 1
    state.total_cost_paid += cost

    # ── 第3步：配对结算（TradeLifecycle 统一处理 FIFO 配对）──
    trade_record = state.lifecycle.add_fill(
        direction=direction,
        shares=shares,
        fill_price=fill_price,
        fill_time=bar.get("time", ""),
        fill_date=trading_date,
        fill_bar_idx=bar_idx,
        cost=cost,
    )

    # 补充信号信息
    trade_record["signal_price"] = price
    trade_record["rules_score"] = signal.rules_score
    trade_record["rules_fired"] = signal.rules_fired
    trade_record["vwap"] = ref_price
    trade_record["expected_spread"] = expected_spread if ref_price > 0 else 0

    # 累计配对盈亏
    state.cost_reduction += trade_record["pnl"]
    state.trades.append(trade_record)


# ═══════════════════════════════════════════════════════════════
# 多日回测
# ═══════════════════════════════════════════════════════════════
def backtest_multi_day(
    code: str,
    daily_bars: dict[str, list[dict]],  # {date: [bars]}
    daily_prev_closes: dict[str, float],  # {date: prev_close}
    params: Optional[BacktestParams] = None,
    l1_risk_dates: set[str] | None = None,
    retreated_dates: set[str] | None = None,
) -> dict:
    """
    对单只股票多个交易日进行回测。

    P3-1: FIFO 配对队列 open_legs 跨日连续，不再按日重置。
    日内状态（locked_shares/t_trades_today/last_signal_bar）仍每日重置
    （T+1 解锁的是 locked_shares，与 open_legs 无关）。
    回测结束时对仍未配对的 open_legs 按最后收盘价计算浮盈浮亏。

    返回:
    {
        "code": str,
        "total_days": int,
        "total_trades": int,
        "total_cost_reduction": float,    # 已实现配对盈亏
        "total_cost_paid": float,         # 总交易成本
        "net_pnl": float,                 # 净盈亏 = 已实现 - 成本
        "unrealized_pnl": float,          # P3-1: 未配对敞口浮盈浮亏
        "net_pnl_with_unrealized": float, # P3-1: 含浮盈浮亏的净盈亏
        "final_open_legs_count": int,     # P3-1: 回测结束未配对腿数
        "win_rate": float,                # 基于已配对交易
        "avg_trades_per_day": float,
        "daily_results": list[dict],
    }
    """
    params = params or BacktestParams()
    l1_risk_dates = l1_risk_dates or set()
    retreated_dates = retreated_dates or set()

    daily_results = []
    total_trades = 0
    total_cost_reduction = 0.0
    total_cost_paid = 0.0
    total_win = 0
    carry_open_legs: list[dict] = []  # P3-1: 跨日延续的未配对腿
    last_close = 0.0

    for date_str in sorted(daily_bars.keys()):
        bars = daily_bars[date_str]
        prev_close = daily_prev_closes.get(date_str, 0)
        if prev_close <= 0 or len(bars) < params.warmup_bars:
            continue

        result = backtest_single_day(
            code=code,
            trading_date=date_str,
            bars=bars,
            prev_close=prev_close,
            params=params,
            l1_systemic_risk=date_str in l1_risk_dates,
            theme_retreated=date_str in retreated_dates,
            initial_open_legs=carry_open_legs,  # P3-1: 传入跨日腿
        )
        daily_results.append(result)
        total_trades += result["t_trades"]
        total_cost_reduction += result["cost_reduction"]
        total_cost_paid += result["total_cost_paid"]
        total_win += sum(1 for t in result["trades"] if t.get("pnl", 0) > 0)
        carry_open_legs = result.get("final_open_legs", [])
        last_close = result.get("last_close", prev_close)

    # P3-1: 回测结束时对未配对敞口按最后收盘价计算浮盈浮亏
    # P0-4: 委托给 backtest_metrics.compute_unrealized_pnl
    unrealized_pnl = compute_unrealized_pnl(carry_open_legs, last_close)

    win_rate = total_win / total_trades if total_trades > 0 else 0.0
    net_pnl = total_cost_reduction - total_cost_paid
    return {
        "code": code,
        "total_days": len(daily_results),
        "total_trades": total_trades,
        "total_cost_reduction": total_cost_reduction,
        "total_cost_paid": total_cost_paid,
        "net_pnl": net_pnl,
        "unrealized_pnl": unrealized_pnl,  # P3-1: 未配对敞口浮盈浮亏
        "net_pnl_with_unrealized": round(net_pnl + unrealized_pnl, 2),  # P3-1: 含浮盈浮亏
        "final_open_legs_count": len(carry_open_legs),  # P3-1: 回测结束未配对腿数
        "win_rate": win_rate,
        "avg_trades_per_day": total_trades / len(daily_results) if daily_results else 0,
        "daily_results": daily_results,
    }


# ═══════════════════════════════════════════════════════════════
# 结果输出（P0-4: save_backtest_report / print_backtest_summary
# 已迁移至 backtest_metrics，此处通过 import re-export 保持兼容）
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    from minute_bar_fetcher import load_minute_bars_from_csv

    parser = argparse.ArgumentParser(description="L5 T+0 backtester")
    parser.add_argument("--code", default="600xxx.SH", help="股票代码")
    parser.add_argument("--date", default=None, help="单日回测 YYYY-MM-DD")
    parser.add_argument("--prev-close", type=float, default=10.00, help="昨收价")
    parser.add_argument("--base-shares", type=int, default=3000, help="底仓股数")
    parser.add_argument("--avg-cost", type=float, default=10.00, help="底仓成本")
    parser.add_argument("--l1-risk", action="store_true", help="模拟 L1 系统性风险")
    parser.add_argument("--retreated", action="store_true", help="模拟 L2 题材退潮")
    args = parser.parse_args()

    params = BacktestParams(
        base_shares=args.base_shares,
        avg_cost=args.avg_cost,
    )

    if args.date:
        # 单日回测
        bars = load_minute_bars_from_csv(args.code, args.date)
        if not bars:
            print(f"[ERROR] no bars for {args.code} on {args.date}")
            sys.exit(1)
        result = backtest_single_day(
            code=args.code,
            trading_date=args.date,
            bars=bars,
            prev_close=args.prev_close,
            params=params,
            l1_systemic_risk=args.l1_risk,
            theme_retreated=args.retreated,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        parser.print_help()


# ═══ backtest: run_artifacts（run_id 版本化 + 数据指纹） ═══


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "outputs" / "runs"


# ═══════════════════════════════════════════════════════════════
# 哈希工具
# ═══════════════════════════════════════════════════════════════
def _hash_dict(d: dict) -> str:
    """对 dict 做 SHA1，返回前8位。"""
    raw = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


# ═══════════════════════════════════════════════════════════════
# 数据指纹
# ═══════════════════════════════════════════════════════════════
def compute_data_fingerprint(
    code: str,
    start_date: str,
    end_date: str,
    daily_meta: dict,
    frequency: str = "",
) -> dict:
    """
    构建数据指纹（非数据本身，而是可追溯的元信息）。

    :param daily_meta: run_backtest 的 daily_meta {date: {source, frequency, bars_count}}
    """
    sources_used = sorted({
        meta.get("source", "")
        for meta in daily_meta.values()
        if meta.get("source")
    })
    total_bars = sum(meta.get("bars_count", 0) for meta in daily_meta.values())
    return {
        "code": code,
        "start_date": start_date,
        "end_date": end_date,
        "frequency": frequency,
        "trading_days": len(daily_meta),
        "total_bars": total_bars,
        "sources_used": sources_used,
    }


# ═══════════════════════════════════════════════════════════════
# 代码版本
# ═══════════════════════════════════════════════════════════════
def _get_git_commit() -> str:
    """获取当前 git commit hash，失败返回 'unknown'。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ═══════════════════════════════════════════════════════════════
# run_id 生成
# ═══════════════════════════════════════════════════════════════
def generate_run_id(params_dict: dict, data_fingerprint: dict) -> str:
    """
    生成唯一 run_id: {timestamp}_{param_hash8}_{data_hash8}
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    param_hash = _hash_dict(params_dict)
    data_hash = _hash_dict(data_fingerprint)
    return f"{timestamp}_{param_hash}_{data_hash}"


# ═══════════════════════════════════════════════════════════════
# artifacts 落盘
# ═══════════════════════════════════════════════════════════════
def save_run_artifacts(
    run_id: str,
    params_dict: dict,
    data_fingerprint: dict,
    output_files: dict[str, Path],
    run_type: str = "single",
) -> Path:
    """
    将运行 artifacts 落盘到 outputs/runs/<run_id>/。

    :param output_files: {"trades": Path, "report": Path, "html": Path, ...}
    :param run_type: "single" / "batch"
    :return: run 目录路径
    """
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # params.json
    with open(run_dir / "params.json", "w", encoding="utf-8") as f:
        json.dump(params_dict, f, ensure_ascii=False, indent=2, default=str)

    # data_fingerprint.json
    with open(run_dir / "data_fingerprint.json", "w", encoding="utf-8") as f:
        json.dump(data_fingerprint, f, ensure_ascii=False, indent=2, default=str)

    # code_version.txt
    commit = _get_git_commit()
    with open(run_dir / "code_version.txt", "w", encoding="utf-8") as f:
        f.write(commit)

    # manifest.json
    manifest = {
        "run_id": run_id,
        "run_type": run_type,
        "created_at": datetime.now().isoformat(),
        "param_hash": _hash_dict(params_dict),
        "data_hash": _hash_dict(data_fingerprint),
        "git_commit": commit,
        "output_files": {k: str(v) for k, v in output_files.items()},
    }
    with open(run_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    return run_dir


# ═══ backtest: tune_params（网格搜索 + 滚动样本外验证） ═══


# ═══════════════════════════════════════════════════════════════
# 调优配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "backtest"

# 参数搜索空间
PARAM_GRID = {
    "vwap_dev_atr_multiplier": [0.6, 0.8, 1.0],
    "rsi_overbought": [65.0, 70.0, 75.0],
    "rsi_oversold": [25.0, 30.0, 35.0],
    "min_capture_spread": [0.004, 0.006, 0.008],
    "max_t_size_ratio": [0.3, 0.5],
}

# 测试形态
PATTERNS = list(PATTERN_GENERATORS.keys())

# 每种形态生成 N 天数据（不同 seed）
DAYS_PER_PATTERN = 3


# ═══════════════════════════════════════════════════════════════
# 单组参数评估
# ═══════════════════════════════════════════════════════════════
def evaluate_param_set(
    signal_p: SignalParams,
    risk_p: RiskParams,
) -> dict:
    """
    用给定参数在所有形态 × 多天上跑回测，返回汇总统计。
    """
    backtest_p = BacktestParams(
        base_shares=3000,
        avg_cost=10.00,
        signal_params=signal_p,
        risk_params=risk_p,
    )

    pattern_results = {}
    all_net_pnl = []
    all_trades = 0
    all_wins = 0
    all_days = 0

    for pattern in PATTERNS:
        pattern_pnl = 0.0
        pattern_trades = 0
        pattern_wins = 0
        for day in range(DAYS_PER_PATTERN):
            bars = gen_pattern(pattern, base_price=10.00, seed=42 + day)
            result = backtest_single_day(
                code="TEST",
                trading_date=f"2026-07-{20+day:02d}",
                bars=bars,
                prev_close=10.00,
                params=backtest_p,
            )
            pattern_pnl += result["net_pnl"]
            pattern_trades += result["t_trades"]
            pattern_wins += sum(1 for t in result["trades"] if t.get("pnl", 0) > 0)
            all_days += 1

        pattern_results[pattern] = {
            "net_pnl": pattern_pnl,
            "trades": pattern_trades,
            "wins": pattern_wins,
            "win_rate": pattern_wins / pattern_trades if pattern_trades > 0 else 0,
            "avg_trades_per_day": pattern_trades / DAYS_PER_PATTERN,
        }
        all_net_pnl.append(pattern_pnl)
        all_trades += pattern_trades
        all_wins += pattern_wins

    return {
        "pattern_results": pattern_results,
        "total_net_pnl": sum(all_net_pnl),
        "total_trades": all_trades,
        "total_wins": all_wins,
        "overall_win_rate": all_wins / all_trades if all_trades > 0 else 0,
        "worst_pattern_pnl": min(all_net_pnl),
        "best_pattern_pnl": max(all_net_pnl),
        "avg_pattern_pnl": sum(all_net_pnl) / len(all_net_pnl),
        "total_days": all_days,
    }


# ═══════════════════════════════════════════════════════════════
# 真实数据模式（real）
# ═══════════════════════════════════════════════════════════════
REAL_POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
MIN_TRADES_THRESHOLD = 10  # 触发次数下限


def load_real_data(
    codes: list[str],
    start: str,
    end: str,
    source: str = "baostock",
) -> dict:
    """
    拉取真实多股票多日数据，缓存到内存（避免网格搜索时重复拉取）。

    :return: {code: {daily_bars, daily_prev_closes, daily_meta, all_dates}}
    """
    from .data import fetch_multi_day

    cache = {}
    for i, code in enumerate(codes):
        print(f"  [load_real_data {i+1}/{len(codes)}] {code}...", end=" ", flush=True)
        daily_bars, daily_prev_closes, daily_meta = fetch_multi_day(
            code, start, end, source
        )
        if daily_bars:
            all_dates = sorted(daily_bars.keys())
            cache[code] = {
                "daily_bars": daily_bars,
                "daily_prev_closes": daily_prev_closes,
                "daily_meta": daily_meta,
                "all_dates": all_dates,
            }
            print(f"{len(all_dates)}天")
        else:
            print("无数据，跳过")
    return cache


def evaluate_param_set_real(
    signal_p: SignalParams,
    risk_p: RiskParams,
    real_data: dict,
    target_dates: set[str],
) -> dict:
    """
    用真实数据在指定日期集合上评估一组参数。
    P3-1: 使用含浮盈浮亏的净盈亏(net_pnl_with_unrealized)作为排名指标。

    :param target_dates: 只回测这些日期（用于训练/验证集切分）
    """
    from .cli import run, adapt_params_by_frequency
    from .data import normalize_code

    total_gross = 0.0
    total_cost = 0.0
    total_trades = 0
    total_paired = 0
    total_wins = 0
    total_unrealized = 0.0  # P3-1: 未配对敞口浮盈浮亏
    total_final_legs = 0

    for code, data in real_data.items():
        # 按目标日期过滤
        daily_bars = {
            d: b for d, b in data["daily_bars"].items() if d in target_dates
        }
        daily_prev = {
            d: p for d, p in data["daily_prev_closes"].items() if d in target_dates
        }
        if not daily_bars:
            continue

        first_meta = next(iter(data["daily_meta"].values()))
        freq = first_meta.get("frequency", "5min")
        bpd = first_meta.get("bars_count", 48)

        avg_cost = min(daily_prev.values())
        bp = BacktestParams(
            base_shares=3000,
            avg_cost=avg_cost,
            signal_params=signal_p,
            risk_params=risk_p,
        )
        bp = adapt_params_by_frequency(bp, freq, bpd)

        result = backtest_multi_day(
            code=normalize_code(code)["pure"],
            daily_bars=daily_bars,
            daily_prev_closes=daily_prev,
            params=bp,
        )

        for dr in result.get("daily_results", []):
            for t in dr.get("trades", []):
                total_trades += 1
                total_cost += t.get("cost", 0)
                if t.get("paired"):
                    total_paired += 1
                    if t.get("pnl", 0) > 0:
                        total_wins += 1
                total_gross += t.get("pnl", 0)

        # P3-1: 累计未配对敞口浮盈浮亏
        total_unrealized += result.get("unrealized_pnl", 0.0)
        total_final_legs += result.get("final_open_legs_count", 0)

    net_pnl = total_gross - total_cost
    return {
        "total_net_pnl": round(net_pnl + total_unrealized, 2),  # P3-1: 改用含浮盈口径排名
        "realized_net_pnl": round(net_pnl, 2),  # 已实现（不含浮盈）
        "unrealized_pnl": round(total_unrealized, 2),
        "gross_pnl": round(total_gross, 2),
        "total_cost": round(total_cost, 2),
        "total_trades": total_trades,
        "paired_trades": total_paired,
        "win_trades": total_wins,
        "win_rate": round(total_wins / total_paired, 4) if total_paired else 0.0,
        "final_open_legs_count": total_final_legs,
        "insufficient_sample": total_trades < MIN_TRADES_THRESHOLD,
    }


def rolling_out_of_sample(
    real_data: dict,
    verbose: bool = True,
) -> dict:
    """
    滚动样本外验证：前 2/3 交易日选参数，后 1/3 验证。

    1. 训练集上网格搜索所有参数组合
    2. 过滤触发次数 < MIN_TRADES_THRESHOLD 的组合（标注"样本不足"）
    3. 取训练集 Top 5 参数在验证集上验证
    4. 只有验证集仍稳健的参数才算数
    """
    # 收集所有交易日
    all_dates_set = set()
    for data in real_data.values():
        all_dates_set.update(data["all_dates"])
    all_dates = sorted(all_dates_set)

    split = int(len(all_dates) * 2 / 3)
    train_dates = set(all_dates[:split])
    val_dates = set(all_dates[split:])

    if verbose:
        print(f"\n交易日总数: {len(all_dates)}")
        print(f"训练集: {len(train_dates)}天 ({all_dates[0]} ~ {all_dates[split-1]})")
        print(f"验证集: {len(val_dates)}天 ({all_dates[split]} ~ {all_dates[-1]})")
        print(f"参数组合数: {len(list(itertools.product(*PARAM_GRID.values())))}")
        print("=" * 100)

    # 1. 训练集网格搜索
    keys = list(PARAM_GRID.keys())
    value_lists = [PARAM_GRID[k] for k in keys]
    combinations = list(itertools.product(*value_lists))

    train_results = []
    for idx, combo in enumerate(combinations):
        params_dict = dict(zip(keys, combo))
        signal_p = SignalParams(
            vwap_dev_atr_multiplier=params_dict["vwap_dev_atr_multiplier"],
            rsi_overbought=params_dict["rsi_overbought"],
            rsi_oversold=params_dict["rsi_oversold"],
        )
        risk_p = RiskParams(
            min_capture_spread=params_dict["min_capture_spread"],
            max_t_size_ratio=params_dict["max_t_size_ratio"],
        )
        eval_result = evaluate_param_set_real(
            signal_p, risk_p, real_data, train_dates
        )
        eval_result["params"] = params_dict
        train_results.append(eval_result)

        if verbose and (idx + 1) % 20 == 0:
            valid_so_far = sum(1 for r in train_results if not r["insufficient_sample"])
            print(f"  训练集进度: {idx+1}/{len(combinations)} "
                  f"(有效{valid_so_far} 样本不足{idx+1-valid_so_far})")

    # 2. 分离样本不足的
    valid = [r for r in train_results if not r["insufficient_sample"]]
    insufficient = [r for r in train_results if r["insufficient_sample"]]
    valid.sort(key=lambda x: x["total_net_pnl"], reverse=True)

    if verbose:
        print(f"\n训练集结果: 有效{len(valid)} 样本不足{len(insufficient)}")
        if insufficient:
            print(f"  [警告] {len(insufficient)} 组参数触发次数 < {MIN_TRADES_THRESHOLD}，已标注为样本不足")

    # 3. Top 5 在验证集上验证
    top_n = min(5, len(valid))
    val_results = []
    for i, r in enumerate(valid[:top_n]):
        params_dict = r["params"]
        signal_p = SignalParams(
            vwap_dev_atr_multiplier=params_dict["vwap_dev_atr_multiplier"],
            rsi_overbought=params_dict["rsi_overbought"],
            rsi_oversold=params_dict["rsi_oversold"],
        )
        risk_p = RiskParams(
            min_capture_spread=params_dict["min_capture_spread"],
            max_t_size_ratio=params_dict["max_t_size_ratio"],
        )
        val_eval = evaluate_param_set_real(
            signal_p, risk_p, real_data, val_dates
        )
        val_eval["params"] = params_dict
        val_eval["train_net_pnl"] = r["total_net_pnl"]
        val_eval["train_win_rate"] = r["win_rate"]
        val_eval["train_trades"] = r["total_trades"]
        val_results.append(val_eval)

        if verbose:
            print(f"  验证 Top{i+1}: 训练净{r['total_net_pnl']:+.2f} "
                  f"→ 验证净{val_eval['total_net_pnl']:+.2f} "
                  f"胜率{val_eval['win_rate']*100:.1f}% "
                  f"交易{val_eval['total_trades']}笔")

    return {
        "all_dates": all_dates,
        "train_dates": sorted(train_dates),
        "val_dates": sorted(val_dates),
        "train_results_valid": valid,
        "train_results_insufficient": insufficient,
        "val_results": val_results,
    }


def print_real_results(roos: dict):
    """打印滚动样本外验证结果。"""
    print(f"\n{'='*120}")
    print("滚动样本外验证结果")
    print(f"{'='*120}")
    print(f"训练集: {len(roos['train_dates'])}天  验证集: {len(roos['val_dates'])}天")
    print(f"有效参数组合: {len(roos['train_results_valid'])}  "
          f"样本不足: {len(roos['train_results_insufficient'])}")

    print(f"\n{'='*120}")
    print("训练集 Top 5 参数 → 验证集表现")
    print(f"{'='*120}")
    print(f"{'排名':<4} {'训练净盈亏':>12} {'验证净盈亏':>12} {'训练胜率':>8} {'验证胜率':>8} "
          f"{'训练笔数':>8} {'验证笔数':>8} {'VWAP×ATR':>8} {'RSI高':>6} {'RSI低':>6} {'价差':>6} {'仓位':>6}")
    print("-" * 120)
    for i, r in enumerate(roos["val_results"]):
        p = r["params"]
        train_wr = f"{r['train_win_rate']*100:.1f}%"
        val_wr = f"{r['win_rate']*100:.1f}%" if r["paired_trades"] else "N/A"
        print(f"{i+1:<4} {r['train_net_pnl']:>12.2f} {r['total_net_pnl']:>12.2f} "
              f"{train_wr:>8} {val_wr:>8} "
              f"{r['train_trades']:>8} {r['total_trades']:>8} "
              f"{p['vwap_dev_atr_multiplier']:>8} {p['rsi_overbought']:>6.0f} "
              f"{p['rsi_oversold']:>6.0f} {p['min_capture_spread']*100:>5.1f}% "
              f"{p['max_t_size_ratio']*100:>5.0f}%")

    if roos["train_results_insufficient"]:
        print(f"\n[样本不足] 以下 {len(roos['train_results_insufficient'])} 组参数触发次数 < {MIN_TRADES_THRESHOLD}，不参与排名:")
        for r in roos["train_results_insufficient"][:5]:
            p = r["params"]
            print(f"  交易{r['total_trades']}笔  VWAP×ATR={p['vwap_dev_atr_multiplier']} "
                  f"RSI={p['rsi_overbought']}/{p['rsi_oversold']}")


def save_real_report(roos: dict, output_path: Path):
    """保存滚动样本外验证报告。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "real_rolling_out_of_sample",
        "min_trades_threshold": MIN_TRADES_THRESHOLD,
        "train_dates": roos["train_dates"],
        "val_dates": roos["val_dates"],
        "train_valid_count": len(roos["train_results_valid"]),
        "train_insufficient_count": len(roos["train_results_insufficient"]),
        "val_top5": roos["val_results"],
        "train_top10": roos["train_results_valid"][:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# 网格搜索（合成数据模式）
# ═══════════════════════════════════════════════════════════════
def grid_search(verbose: bool = True) -> list[dict]:
    """网格搜索所有参数组合。返回按 total_net_pnl 降序排列的结果列表。"""
    keys = list(PARAM_GRID.keys())
    value_lists = [PARAM_GRID[k] for k in keys]
    combinations = list(itertools.product(*value_lists))

    if verbose:
        print(f"参数组合数: {len(combinations)}")
        print(f"每种组合测试形态: {len(PATTERNS)} × {DAYS_PER_PATTERN} 天 = {len(PATTERNS)*DAYS_PER_PATTERN} 日")
        print(f"总回测次数: {len(combinations) * len(PATTERNS) * DAYS_PER_PATTERN}")
        print("=" * 100)

    results = []
    for idx, combo in enumerate(combinations):
        params_dict = dict(zip(keys, combo))
        signal_p = SignalParams(
            vwap_dev_atr_multiplier=params_dict["vwap_dev_atr_multiplier"],
            rsi_overbought=params_dict["rsi_overbought"],
            rsi_oversold=params_dict["rsi_oversold"],
        )
        risk_p = RiskParams(
            min_capture_spread=params_dict["min_capture_spread"],
            max_t_size_ratio=params_dict["max_t_size_ratio"],
        )

        eval_result = evaluate_param_set(signal_p, risk_p)
        eval_result["params"] = params_dict
        results.append(eval_result)

        if verbose and (idx + 1) % 10 == 0:
            print(f"  进度: {idx+1}/{len(combinations)}")

    # 按 total_net_pnl 降序
    results.sort(key=lambda x: x["total_net_pnl"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════════════════════════
def print_top_results(results: list[dict], top_n: int = 10):
    """打印 Top N 参数组合。"""
    print(f"\n{'='*120}")
    print(f"Top {top_n} 参数组合（按总净盈亏降序）")
    print(f"{'='*120}")
    print(f"{'排名':<4} {'总盈亏':>10} {'最差形态':>10} {'胜率':>8} {'T次数':>6} "
          f"{'VWAP×ATR':>8} {'RSI高':>6} {'RSI低':>6} {'价差':>6} {'仓位':>6}")
    print("-" * 120)
    for i, r in enumerate(results[:top_n]):
        p = r["params"]
        print(f"{i+1:<4} {r['total_net_pnl']:>10.2f} {r['worst_pattern_pnl']:>10.2f} "
              f"{r['overall_win_rate']*100:>7.1f}% {r['total_trades']:>6} "
              f"{p['vwap_dev_atr_multiplier']:>8} {p['rsi_overbought']:>6.0f} "
              f"{p['rsi_oversold']:>6.0f} {p['min_capture_spread']*100:>5.1f}% "
              f"{p['max_t_size_ratio']*100:>5.0f}%")

    print(f"\n{'='*120}")
    print("最优组合各形态明细:")
    print(f"{'='*120}")
    best = results[0]
    print(f"{'形态':<20} {'净盈亏':>10} {'T次数':>6} {'胜率':>8} {'日均T':>8}")
    print("-" * 60)
    for pattern, stats in best["pattern_results"].items():
        print(f"{pattern:<20} {stats['net_pnl']:>10.2f} {stats['trades']:>6} "
              f"{stats['win_rate']*100:>7.1f}% {stats['avg_trades_per_day']:>8.2f}")


def save_report(results: list[dict], output_path: Path):
    """保存完整报告为 JSON。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 只保存 Top 20 + 最差 5，避免文件过大
    top20 = results[:20]
    worst5 = results[-5:]
    report = {
        "summary": {
            "total_combinations": len(results),
            "best_total_pnl": results[0]["total_net_pnl"],
            "worst_total_pnl": results[-1]["total_net_pnl"],
            "best_params": results[0]["params"],
        },
        "top_20": top20,
        "worst_5": worst5,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="L5 参数调优")
    parser.add_argument("--data-source", default="synthetic",
                        choices=["synthetic", "real"],
                        help="数据源: synthetic=合成数据(默认), real=真实多股票数据")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式（缩小参数空间）")
    parser.add_argument("--start", default="2026-06-22",
                        help="real 模式起始日期")
    parser.add_argument("--end", default="2026-07-22",
                        help="real 模式结束日期")
    parser.add_argument("--source", default="baostock",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"],
                        help="real 模式数据源")
    parser.add_argument("--max-codes", type=int, default=5,
                        help="real 模式最多用多少只股票（默认5，避免太慢）")
    args = parser.parse_args()

    if args.quick:
        # 快速模式：只测核心参数
        PARAM_GRID["vwap_dev_atr_multiplier"] = [0.8]
        PARAM_GRID["rsi_overbought"] = [70.0]
        PARAM_GRID["rsi_oversold"] = [30.0]
        PARAM_GRID["min_capture_spread"] = [0.006]
        PARAM_GRID["max_t_size_ratio"] = [0.3, 0.5]

    if args.data_source == "real":
        # ── 真实数据模式：滚动样本外验证 ──
        if not REAL_POOL_PATH.exists():
            print(f"[tune] 候选池不存在: {REAL_POOL_PATH}")
            print("[tune] 先跑: python scripts/gen_candidate_pool.py")
            sys.exit(1)

        with open(REAL_POOL_PATH, "r", encoding="utf-8") as f:
            pool = json.load(f)
        codes = [c["code"] for c in pool["candidates"][:args.max_codes]]
        print(f"[tune] real 模式: {len(codes)} 只股票, {args.start}~{args.end}, source={args.source}")
        print(f"[tune] 加载数据...")
        real_data = load_real_data(codes, args.start, args.end, args.source)
        if not real_data:
            print("[tune] 未加载到任何数据，退出")
            sys.exit(1)
        print(f"[tune] 成功加载 {len(real_data)} 只股票数据")

        roos = rolling_out_of_sample(real_data, verbose=True)
        print_real_results(roos)

        report_path = OUTPUT_DIR / "param_tuning_real_report.json"
        save_real_report(roos, report_path)
        print(f"\n[OK] 真实数据调优报告已保存至: {report_path}")
    else:
        # ── 合成数据模式（原有逻辑） ──
        results = grid_search(verbose=True)
        print_top_results(results, top_n=10)

        report_path = OUTPUT_DIR / "param_tuning_report.json"
        save_report(results, report_path)
        print(f"\n[OK] 完整报告已保存至: {report_path}")
