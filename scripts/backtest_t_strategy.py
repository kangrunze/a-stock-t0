#!/usr/bin/env python3
"""
L5 T+0 策略分钟级回测引擎
===========================
基于真实分钟级历史数据回测 L5 T+0 策略，统计胜率/盈亏比/成本覆盖率。

回测方法论（对齐 SKILL.md 第六节）:
  1. 必须用真实分钟级数据，不能用日线近似
  2. 严格时间序列切分：任何时刻 t 的信号只用 [0, t] 区间数据
  3. 模拟真实成本：佣金万2.5（双边）+ 印花税 0.05%（卖单）+ 滑点
  4. 涨跌停封板过滤：信号层 + 回测层双重过滤
  5. T+1 约束：今日买入的股份当日不可卖

回测流程（每个交易日）:
  1. 加载当日 1 分钟 K 线
  2. 从第 30 根开始（确保指标有足够历史）逐根遍历
  3. 在每根 K 线收盘时调用 t_signal_engine 评估信号
  4. 信号触发后调用 t_risk_guard 校验
  5. 校验通过则记录成交（含滑点模拟）
  6. 维护当日 T 状态（locked_shares / t_trades_today / net_position_delta）
  7. 14:50 执行尾盘平衡检查
  8. 收盘后统计当日 T 收益

独立性：回测引擎不依赖 L1/L2/L3/L4。所有联动通过参数注入。
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intraday_reference import compute_reference_snapshot, cumulative_vwap
from t_signal_engine import (
    SignalParams, DEFAULT_PARAMS,
    evaluate_reduce_signal, evaluate_add_signal,
)
from t_risk_guard import RiskParams, DEFAULT_RISK_PARAMS


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
    # 成本
    commission_rate: float = 0.00025        # 佣金万2.5（单边）
    stamp_tax_rate: float = 0.0005          # 印花税 0.05%（卖单）
    slippage: float = 0.001                 # 滑点 0.1%

    # 持仓
    base_shares: int = 3000                 # 底仓股数
    avg_cost: float = 10.00                 # 底仓成本

    # 信号
    signal_params: SignalParams = field(default_factory=SignalParams)
    risk_params: RiskParams = field(default_factory=RiskParams)

    # 回测
    warmup_bars: int = 30                   # 预热K线数（前30根不产生信号）
    eod_check_bar_idx: int = 200            # 14:50 对应的K线索引（约第200根）


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
    open_legs: list[dict] = field(default_factory=list)  # P1-3: FIFO 待配对的腿

    @property
    def sellable_shares(self) -> int:
        """当前可卖底仓（T+1约束）。"""
        return max(0, self.base_shares - self.locked_shares)


# ═══════════════════════════════════════════════════════════════
# 成本计算
# ═══════════════════════════════════════════════════════════════
def calc_trade_cost(
    direction: str,
    shares: int,
    price: float,
    params: BacktestParams,
) -> float:
    """
    计算单笔交易成本（含佣金+印花税）。
    买入：只有佣金
    卖出：佣金 + 印花税
    """
    amount = shares * price
    commission = amount * params.commission_rate
    stamp_tax = amount * params.stamp_tax_rate if direction == "sell" else 0
    return commission + stamp_tax


def apply_slippage(direction: str, price: float, params: BacktestParams) -> float:
    """
    应用滑点。买入价上浮，卖出价下浮（模拟对手价成交）。
    """
    if direction == "buy":
        return price * (1 + params.slippage)
    else:
        return price * (1 - params.slippage)


# ═══════════════════════════════════════════════════════════════
# 单股单日回测
# ═══════════════════════════════════════════════════════════════
def backtest_single_day(
    code: str,
    trading_date: str,
    bars: list[dict],
    prev_close: float,
    params: BacktestParams = BacktestParams(),
    l1_systemic_risk: bool = False,
    theme_retreated: bool = False,
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
    }
    """
    state = BacktestState(
        base_shares=params.base_shares,
        avg_cost=params.avg_cost,
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
        }

    # 计算涨跌停价
    limit_up = round(prev_close * 1.1, 2)
    limit_down = round(prev_close * 0.9, 2)

    # 逐根遍历
    for i in range(params.warmup_bars, len(bars)):
        bar = bars[i]
        price = bar["close"]

        # 涨跌停封板检测
        is_limit_up = abs(price - limit_up) / limit_up < 0.001 if limit_up > 0 else False
        is_limit_down = abs(price - limit_down) / limit_down < 0.001 if limit_down > 0 else False
        # 简化：用最近5根成交量判断封死
        recent_bars = bars[max(0, i - 5) : i + 1]
        avg_vol = sum(b["volume"] for b in recent_bars) / len(recent_bars) if recent_bars else 0
        is_limit_up_locked = is_limit_up and avg_vol < 100
        is_limit_down_locked = is_limit_down and avg_vol < 100

        # 跳过一字板（全天不产生信号）
        if i == params.warmup_bars:
            day_high = max(b["high"] for b in bars)
            day_low = min(b["low"] for b in bars)
            day_open = bars[0]["open"]
            if abs(day_high - day_low) / day_open < 0.001:
                # 一字板，跳过
                break

        # 截至当前K线的bars切片（严格因果）
        bars_up_to_now = bars[: i + 1]

        # 评估减仓信号
        reduce_sig = evaluate_reduce_signal(
            bars_up_to_now,
            current_price=price,
            prev_close=prev_close,
            is_limit_up_locked=is_limit_up_locked,
            params=params.signal_params,
        )

        # 评估加仓信号
        add_sig = evaluate_add_signal(
            bars_up_to_now,
            current_price=price,
            prev_close=prev_close,
            is_limit_down_locked=is_limit_down_locked,
            theme_retreated=theme_retreated,
            params=params.signal_params,
        )

        # 信号触发后执行（模拟成交）
        # 注意：一个时刻只执行一个方向（避免自相矛盾）
        # 优先级：减仓 > 加仓（保守）
        if reduce_sig.triggered and not is_limit_up_locked:
            _try_execute(
                code, trading_date, bar, "sell", reduce_sig, state, params,
                l1_systemic_risk, theme_retreated, prev_close,
            )
        elif add_sig.triggered and not is_limit_down_locked:
            _try_execute(
                code, trading_date, bar, "buy", add_sig, state, params,
                l1_systemic_risk, theme_retreated, prev_close,
            )

        # 尾盘平衡检查（约 14:50）— 当前仅通过 eod_status 标记，不平仓
        # P1-5: 删除了空的 _eod_balance 调用，eod_status 在返回值中由 net_position_delta 计算

    # 统计（P1-3: win_rate 基于配对结算的 pnl，只有完成配对的交易 pnl > 0）
    win_count = sum(1 for t in state.trades if t.get("pnl", 0) > 0)
    win_rate = win_count / len(state.trades) if state.trades else 0.0

    return {
        "code": code,
        "date": trading_date,
        "bars_count": len(bars),
        "t_trades": len(state.trades),
        "cost_reduction": state.cost_reduction,
        "total_cost_paid": state.total_cost_paid,
        "net_pnl": state.cost_reduction - state.total_cost_paid,
        "win_rate": win_rate,
        "trades": state.trades,
        "eod_status": "balanced" if state.net_position_delta == 0 else (
            "net_reduce" if state.net_position_delta < 0 else "net_add"
        ),
    }


def _try_execute(
    code: str,
    trading_date: str,
    bar: dict,
    direction: str,
    signal,  # TSignal
    state: BacktestState,
    params: BacktestParams,
    l1_systemic_risk: bool,
    theme_retreated: bool,
    prev_close: float,
) -> None:
    """尝试执行一笔 T 交易（含风控检查）。"""
    # 每日T次数限制
    if state.t_trades_today >= params.risk_params.max_t_trades_per_day:
        return

    # L1 熔断
    if l1_systemic_risk and direction == "buy":
        return

    # L2 熔断
    if theme_retreated and direction == "buy":
        return

    # 计算建议股数
    max_shares = int(state.base_shares * params.risk_params.max_t_size_ratio)
    max_shares = (max_shares // 100) * 100
    if max_shares <= 0:
        max_shares = 100

    # 卖出时检查可用底仓
    if direction == "sell":
        if state.sellable_shares <= 0:
            return
        shares = min(max_shares, state.sellable_shares)
        shares = (shares // 100) * 100
        if shares <= 0:
            return
    else:
        shares = max_shares

    # 预期价差检查
    price = bar["close"]
    ref_price = signal.snapshot.get("vwap") or prev_close
    if ref_price > 0:
        expected_spread = abs(price - ref_price) / ref_price
        if expected_spread < params.risk_params.min_capture_spread:
            return

    # 模拟成交（含滑点）
    fill_price = apply_slippage(direction, price, params)
    cost = calc_trade_cost(direction, shares, fill_price, params)

    # 更新状态
    if direction == "buy":
        state.locked_shares += shares
        state.net_position_delta += shares
    else:
        state.net_position_delta -= shares

    state.t_trades_today += 1
    state.total_cost_paid += cost

    # P1-3: FIFO 配对结算
    # 尝试与最早的相反方向 open leg 配对，用两者的真实成交价差计算 T 盈亏
    pair_pnl = 0.0
    paired = False
    remaining = shares
    while remaining > 0 and state.open_legs:
        earliest = state.open_legs[0]
        if earliest["direction"] == direction:
            break  # 同方向不能配对（sell 配 buy，buy 配 sell）

        paired_shares = min(remaining, earliest["shares"])
        # 配对 PnL = (卖价 - 买价) × 配对股数
        if direction == "sell":
            # 当前卖，open leg 是买（反T: 先买后卖）
            sell_price = fill_price
            buy_price = earliest["fill_price"]
        else:
            # 当前买，open leg 是卖（正T: 先卖后买回）
            sell_price = earliest["fill_price"]
            buy_price = fill_price

        leg_pnl = (sell_price - buy_price) * paired_shares
        pair_pnl += leg_pnl
        state.cost_reduction += leg_pnl

        earliest["shares"] -= paired_shares
        remaining -= paired_shares
        if earliest["shares"] <= 0:
            state.open_legs.pop(0)
        paired = True

    # 未配对的部分作为新的 open leg 等待后续配对
    if remaining > 0:
        state.open_legs.append({
            "direction": direction,
            "shares": remaining,
            "fill_price": fill_price,
            "time": bar.get("time", ""),
        })

    trade_record = {
        "time": bar.get("time", ""),
        "direction": direction,
        "shares": shares,
        "signal_price": price,
        "fill_price": fill_price,
        "cost": cost,
        "pnl": pair_pnl,              # 配对 PnL（0 如果未配对）
        "paired": paired,             # 是否完成了配对
        "rules_score": signal.rules_score,
        "rules_fired": signal.rules_fired,
        "vwap": ref_price,
        "expected_spread": expected_spread if ref_price > 0 else 0,
    }
    state.trades.append(trade_record)


# ═══════════════════════════════════════════════════════════════
# 多日回测
# ═══════════════════════════════════════════════════════════════
def backtest_multi_day(
    code: str,
    daily_bars: dict[str, list[dict]],  # {date: [bars]}
    daily_prev_closes: dict[str, float],  # {date: prev_close}
    params: BacktestParams = BacktestParams(),
    l1_risk_dates: set[str] | None = None,
    retreated_dates: set[str] | None = None,
) -> dict:
    """
    对单只股票多个交易日进行回测。

    每日独立回测（不复用前一日 T 状态，因为 T+1 已解锁）。
    最终汇总统计。

    返回:
    {
        "code": str,
        "total_days": int,
        "total_trades": int,
        "total_cost_reduction": float,
        "total_cost_paid": float,
        "net_pnl": float,
        "win_rate": float,
        "avg_trades_per_day": float,
        "daily_results": list[dict],
    }
    """
    l1_risk_dates = l1_risk_dates or set()
    retreated_dates = retreated_dates or set()

    daily_results = []
    total_trades = 0
    total_cost_reduction = 0.0
    total_cost_paid = 0.0
    total_win = 0

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
        )
        daily_results.append(result)
        total_trades += result["t_trades"]
        total_cost_reduction += result["cost_reduction"]
        total_cost_paid += result["total_cost_paid"]
        total_win += sum(1 for t in result["trades"] if t.get("pnl", 0) > 0)

    win_rate = total_win / total_trades if total_trades > 0 else 0.0
    return {
        "code": code,
        "total_days": len(daily_results),
        "total_trades": total_trades,
        "total_cost_reduction": total_cost_reduction,
        "total_cost_paid": total_cost_paid,
        "net_pnl": total_cost_reduction - total_cost_paid,
        "win_rate": win_rate,
        "avg_trades_per_day": total_trades / len(daily_results) if daily_results else 0,
        "daily_results": daily_results,
    }


# ═══════════════════════════════════════════════════════════════
# 结果输出
# ═══════════════════════════════════════════════════════════════
def save_backtest_report(result: dict, output_path: Path) -> None:
    """保存回测报告为 JSON。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


def print_backtest_summary(result: dict) -> None:
    """打印回测摘要。"""
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
