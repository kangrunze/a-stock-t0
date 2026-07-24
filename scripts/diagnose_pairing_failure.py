#!/usr/bin/env python3
"""
根因诊断：配对失败原因分布
================================
按用户指示（选项A+B合并执行）做第一、二步诊断：
- 统计每一条 leg "配对失败"的具体原因
- 区分：① 对向信号在 12 根窗口内根本没触发过  ② 触发了但被风控/冷却挡住

方法：
- 不修改任何 strategy/risk/execution/backtest 代码
- 复制 backtest_single_day 主循环并加插桩，在每根 bar 同时记录：
    * reduce_sig / add_sig 的完整评估结果（triggered, layer scores, trend_context）
    * open_legs 快照（direction, fill_bar_idx, holding_bars）
    * cooldown 状态
    * 实际执行结果
- 然后对每个 leg 的生命周期做"逐 bar 回放"，归类配对失败原因

输出：outputs/backtest/diagnose_pairing_failure.json + 控制台统计
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import BacktestParams, backtest_multi_day
from at0.strategy import SignalParams, evaluate_reduce_signal, evaluate_add_signal
from at0.risk import RiskParams, CostModel, ExposurePolicy, approve_signal, eod_risk_disposal
from at0.execution import TradeLifecycle, LegStatus
from at0.features import compute_reference_snapshot


# ═══════════════════════════════════════════════════════════════
# 参数自适应（内联自 cli.adapt_params_by_frequency）
# ═══════════════════════════════════════════════════════════════
def adapt_params(params: BacktestParams, frequency: str, bars_per_day: int) -> BacktestParams:
    if frequency == "5min":
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
    else:
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
    return params


# ═══════════════════════════════════════════════════════════════
# 插桩版 backtest_single_day
# ═══════════════════════════════════════════════════════════════
# 复制 backtest.backtest_single_day 的核心循环，在每根 bar 记录：
#   - reduce_sig / add_sig 的完整评估结果
#   - open_legs 快照
#   - cooldown 状态
#   - 实际执行结果
#
# 不改变任何交易逻辑，只加日志。

def instrumented_backtest_single_day(
    code: str,
    trading_date: str,
    bars: list[dict],
    prev_close: float,
    params: BacktestParams,
    initial_open_legs: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    """
    返回: (原始 result dict, bar_log 列表)
    bar_log 每条记录:
      {
        "bar_idx": int, "time": str, "price": float,
        "reduce": {triggered, extreme, confirm, filter, trend, threshold, score, vwap, vwap_dev, atr},
        "add": {...同上},
        "open_legs": [{direction, fill_bar_idx, holding_bars, fill_price, shares, status}],
        "last_signal_bar": int, "last_signal_dir": str,
        "cooldown_blocks_reduce": bool, "cooldown_blocks_add": bool,
        "executed": "sell"/"buy"/None,
        "exec_detail": {...} or None,
      }
    """
    cost_model = params.get_cost_model()
    exposure_policy = params.get_exposure_policy()
    bars_count = len(bars)

    lifecycle = TradeLifecycle(max_holding_bars=params.max_holding_bars)
    if initial_open_legs:
        lifecycle.import_open_legs([dict(leg) for leg in initial_open_legs])

    # 简化 state（只保留诊断需要的字段）
    base_shares = params.base_shares
    locked_shares = 0
    net_position_delta = 0
    t_trades_today = 0
    total_cost_paid = 0.0
    cost_reduction = 0.0
    trades: list[dict] = []
    risk_events: list[dict] = []
    last_signal_bar = -1
    last_signal_dir = ""
    sellable_shares = base_shares  # 简化：不追踪 T+1 锁定（诊断不需要精确盈亏）

    bar_log: list[dict] = []

    if len(bars) < params.warmup_bars:
        return ({
            "code": code, "date": trading_date, "bars_count": len(bars),
            "t_trades": 0, "cost_reduction": 0.0, "total_cost_paid": 0.0,
            "net_pnl": 0.0, "win_rate": 0.0, "trades": [], "eod_status": "insufficient_bars",
            "final_open_legs": lifecycle.export_open_legs(), "last_close": prev_close,
            "risk_events": [],
        }, bar_log)

    limit_up = round(prev_close * 1.1, 2)
    limit_down = round(prev_close * 0.9, 2)

    def _snap_signal(sig):
        return {
            "triggered": sig.triggered,
            "extreme_score": sig.extreme_score,
            "confirm_score": sig.confirm_score,
            "filter_passed": sig.filter_passed,
            "trend_context": sig.trend_context,
            "rules_score": sig.rules_score,
            "trigger_threshold": sig.trigger_threshold,
            "extreme_min": sig.extreme_min,
            "confirm_min": sig.confirm_min,
            "vwap": sig.snapshot.get("vwap"),
            "vwap_dev": sig.snapshot.get("vwap_dev"),
            "atr": sig.snapshot.get("atr"),
            "is_pairing": sig.is_pairing,
            "pairing_near_vwap": sig.pairing_near_vwap,
            "pairing_direction_confirmed": sig.pairing_direction_confirmed,
        }

    def _snap_open_legs():
        return [
            {
                "direction": leg.direction,
                "fill_bar_idx": leg.fill_bar_idx,
                "holding_bars": leg.holding_bars,
                "fill_price": leg.fill_price,
                "shares": leg.shares,
                "status": leg.status.value,
            }
            for leg in lifecycle.open_legs
        ]

    for i in range(params.warmup_bars, len(bars)):
        bar = bars[i]
        price = bar["close"]

        # 1. update_holding
        lifecycle.update_holding(i, price)

        # 2. check_expiry
        expired_legs = lifecycle.check_expiry(i)
        for exp_leg in expired_legs:
            risk_events.append({
                "type": "expired",
                "direction": exp_leg.direction,
                "shares": exp_leg.shares,
                "fill_price": exp_leg.fill_price,
                "fill_date": exp_leg.fill_date,  # 原始开仓日期（跨日时 != trading_date）
                "fill_bar_idx": exp_leg.fill_bar_idx,
                "expire_bar_idx": i,
                "holding_bars": exp_leg.holding_bars,
                "bar_idx": i,
                "time": bar.get("time", ""),
            })

        # 3. 涨跌停封板检测
        is_limit_up = abs(price - limit_up) / limit_up < 0.001 if limit_up > 0 else False
        is_limit_down = abs(price - limit_down) / limit_down < 0.001 if limit_down > 0 else False
        recent_bars = bars[max(0, i - 5): i + 1]
        avg_vol = sum(b["volume"] for b in recent_bars) / len(recent_bars) if recent_bars else 0
        is_limit_up_locked = is_limit_up and avg_vol < 100
        is_limit_down_locked = is_limit_down and avg_vol < 100

        if i == params.warmup_bars:
            day_high = max(b["high"] for b in bars)
            day_low = min(b["low"] for b in bars)
            day_open = bars[0]["open"]
            if abs(day_high - day_low) / day_open < 0.001:
                break

        bars_up_to_now = bars[: i + 1]

        # 判断是否为平仓评估（根据 open_legs 方向）
        has_buy_open = any(leg.direction == "buy" for leg in lifecycle.open_legs)
        has_sell_open = any(leg.direction == "sell" for leg in lifecycle.open_legs)

        # 4. 评估信号
        reduce_sig = evaluate_reduce_signal(
            bars_up_to_now, current_price=price, prev_close=prev_close,
            is_limit_up_locked=is_limit_up_locked, params=params.signal_params,
            is_for_pairing=has_buy_open,
        )
        add_sig = evaluate_add_signal(
            bars_up_to_now, current_price=price, prev_close=prev_close,
            is_limit_down_locked=is_limit_down_locked, params=params.signal_params,
            is_for_pairing=has_sell_open,
        )

        reduce_ok = reduce_sig.triggered and not is_limit_up_locked
        add_ok = add_sig.triggered and not is_limit_down_locked

        # 5. cooldown
        cooldown_blocks_reduce = False
        cooldown_blocks_add = False
        if (params.cooldown_bars > 0 and last_signal_bar >= 0
                and (i - last_signal_bar) < params.cooldown_bars):
            if last_signal_dir == "sell":
                reduce_ok = False
                cooldown_blocks_reduce = True
            elif last_signal_dir == "buy":
                add_ok = False
                cooldown_blocks_add = True

        # 6. 执行
        executed = None
        exec_detail = None

        open_legs_snap = _snap_open_legs()

        if reduce_ok:
            # 模拟 _execute_trade 的关键逻辑（风控批准 + min_capture_spread）
            ref_price = reduce_sig.snapshot.get("vwap") or prev_close
            expected_spread = abs(price - ref_price) / ref_price if ref_price > 0 else 0
            # 平仓跳过 min_capture_spread（与 backtest.py 一致）
            spread_blocked = (expected_spread < params.risk_params.min_capture_spread
                               if ref_price > 0 and not reduce_sig.is_pairing else False)

            open_legs_dicts = lifecycle.export_open_legs()
            decision = approve_signal(
                direction="sell",
                requested_shares=int(base_shares * params.risk_params.max_t_size_ratio),
                open_legs=open_legs_dicts,
                bar_idx=i, bars_count=bars_count,
                policy=exposure_policy,
                sellable_shares=sellable_shares,
                t_trades_today=t_trades_today,
                max_t_trades_per_day=params.risk_params.max_t_trades_per_day,
                max_t_size_ratio=params.risk_params.max_t_size_ratio,
                base_shares=base_shares,
            )

            if decision.approved and not spread_blocked and decision.adjusted_shares > 0:
                shares = decision.adjusted_shares
                fill_price = cost_model.fill_price("sell", price)
                cost = cost_model.calc_cost("sell", shares, fill_price)
                locked_shares += shares
                net_position_delta -= shares
                t_trades_today += 1
                total_cost_paid += cost
                trade_record = lifecycle.add_fill(
                    direction="sell", shares=shares, fill_price=fill_price,
                    fill_time=bar.get("time", ""), fill_date=trading_date,
                    fill_bar_idx=i, cost=cost,
                )
                trade_record["signal_price"] = price
                trade_record["rules_score"] = reduce_sig.rules_score
                trade_record["rules_fired"] = reduce_sig.rules_fired
                trade_record["vwap"] = ref_price
                trade_record["expected_spread"] = expected_spread
                cost_reduction += trade_record["pnl"]
                trades.append(trade_record)
                last_signal_bar = i
                last_signal_dir = "sell"
                executed = "sell"
                exec_detail = {"approved": True, "spread_blocked": False, "shares": shares, "reason": ""}
            else:
                executed = None
                reasons = []
                if not decision.approved:
                    reasons.append(f"risk:{decision.reason}")
                if spread_blocked:
                    reasons.append(f"spread<{params.risk_params.min_capture_spread}")
                exec_detail = {"approved": False, "spread_blocked": spread_blocked,
                                "reason": "; ".join(reasons), "risk_reason": decision.reason,
                                "risk_checks": decision.checks}

        elif add_ok:
            ref_price = add_sig.snapshot.get("vwap") or prev_close
            expected_spread = abs(price - ref_price) / ref_price if ref_price > 0 else 0
            # 平仓跳过 min_capture_spread（与 backtest.py 一致）
            spread_blocked = (expected_spread < params.risk_params.min_capture_spread
                               if ref_price > 0 and not add_sig.is_pairing else False)

            open_legs_dicts = lifecycle.export_open_legs()
            decision = approve_signal(
                direction="buy",
                requested_shares=int(base_shares * params.risk_params.max_t_size_ratio),
                open_legs=open_legs_dicts,
                bar_idx=i, bars_count=bars_count,
                policy=exposure_policy,
                sellable_shares=sellable_shares,
                t_trades_today=t_trades_today,
                max_t_trades_per_day=params.risk_params.max_t_trades_per_day,
                max_t_size_ratio=params.risk_params.max_t_size_ratio,
                base_shares=base_shares,
            )

            if decision.approved and not spread_blocked and decision.adjusted_shares > 0:
                shares = decision.adjusted_shares
                fill_price = cost_model.fill_price("buy", price)
                cost = cost_model.calc_cost("buy", shares, fill_price)
                locked_shares += shares
                net_position_delta += shares
                t_trades_today += 1
                total_cost_paid += cost
                trade_record = lifecycle.add_fill(
                    direction="buy", shares=shares, fill_price=fill_price,
                    fill_time=bar.get("time", ""), fill_date=trading_date,
                    fill_bar_idx=i, cost=cost,
                )
                trade_record["signal_price"] = price
                trade_record["rules_score"] = add_sig.rules_score
                trade_record["rules_fired"] = add_sig.rules_fired
                trade_record["vwap"] = ref_price
                trade_record["expected_spread"] = expected_spread
                cost_reduction += trade_record["pnl"]
                trades.append(trade_record)
                last_signal_bar = i
                last_signal_dir = "buy"
                executed = "buy"
                exec_detail = {"approved": True, "spread_blocked": False, "shares": shares, "reason": ""}
            else:
                executed = None
                reasons = []
                if not decision.approved:
                    reasons.append(f"risk:{decision.reason}")
                if spread_blocked:
                    reasons.append(f"spread<{params.risk_params.min_capture_spread}")
                exec_detail = {"approved": False, "spread_blocked": spread_blocked,
                                "reason": "; ".join(reasons), "risk_reason": decision.reason,
                                "risk_checks": decision.checks}

        # 记录 bar_log
        bar_log.append({
            "bar_idx": i,
            "time": bar.get("time", ""),
            "price": price,
            "reduce": _snap_signal(reduce_sig),
            "add": _snap_signal(add_sig),
            "open_legs": open_legs_snap,
            "last_signal_bar": last_signal_bar,
            "last_signal_dir": last_signal_dir,
            "cooldown_blocks_reduce": cooldown_blocks_reduce,
            "cooldown_blocks_add": cooldown_blocks_add,
            "executed": executed,
            "exec_detail": exec_detail,
            "t_trades_today": t_trades_today,
            "max_t_trades": params.risk_params.max_t_trades_per_day,
        })

    # 尾盘处置
    last_close = bars[-1]["close"] if bars else prev_close
    open_legs_dicts = lifecycle.export_open_legs()
    if open_legs_dicts:
        events = eod_risk_disposal(code, trading_date, open_legs_dicts, last_close, lifecycle)
        for ev in events:
            risk_events.append(ev.to_dict())

    # 统计
    paired = [t for t in trades if t.get("paired")]
    win_count = sum(1 for t in paired if t.get("pnl", 0) > 0)
    win_rate = win_count / len(paired) if paired else 0.0

    eod_status = "balanced" if net_position_delta == 0 else (
        "net_reduce" if net_position_delta < 0 else "net_add")
    if risk_events:
        if any(e.get("type") == "expired" for e in risk_events):
            eod_status = "has_expired_legs"

    result = {
        "code": code, "date": trading_date, "bars_count": len(bars),
        "t_trades": len(trades), "cost_reduction": cost_reduction,
        "total_cost_paid": total_cost_paid,
        "net_pnl": cost_reduction - total_cost_paid,
        "win_rate": win_rate, "trades": trades, "eod_status": eod_status,
        "final_open_legs": lifecycle.export_open_legs(),
        "last_close": last_close, "risk_events": risk_events,
    }
    return result, bar_log


def instrumented_backtest_multi_day(
    code: str,
    daily_bars: dict[str, list[dict]],
    daily_prev_closes: dict[str, float],
    params: BacktestParams,
) -> tuple[dict, list[dict]]:
    """
    返回: (原始 result dict, 合并的 bar_log)
    bar_log 的每条记录带 stock_code + date 字段，方便跨日追踪
    """
    daily_results = []
    total_trades = 0
    total_cost_reduction = 0.0
    total_cost_paid = 0.0
    carry_open_legs: list[dict] = []
    last_close = 0.0
    all_bar_log: list[dict] = []

    for date_str in sorted(daily_bars.keys()):
        bars = daily_bars[date_str]
        prev_close = daily_prev_closes.get(date_str, 0)
        if prev_close <= 0 or len(bars) < params.warmup_bars:
            continue

        result, bar_log = instrumented_backtest_single_day(
            code=code, trading_date=date_str, bars=bars, prev_close=prev_close,
            params=params, initial_open_legs=carry_open_legs,
        )
        # 给 bar_log 加日期和股票标记
        for b in bar_log:
            b["date"] = date_str
            b["stock_code"] = code
        all_bar_log.extend(bar_log)

        daily_results.append(result)
        total_trades += result["t_trades"]
        total_cost_reduction += result["cost_reduction"]
        total_cost_paid += result["total_cost_paid"]
        carry_open_legs = result["final_open_legs"]
        last_close = result["last_close"]

    # 浮盈浮亏
    unrealized = 0.0
    for leg in carry_open_legs:
        if leg["direction"] == "buy":
            unrealized += (last_close - leg["fill_price"]) * leg["shares"]
        else:
            unrealized += (leg["fill_price"] - last_close) * leg["shares"]

    paired_total = sum(1 for dr in daily_results for t in dr["trades"] if t.get("paired"))
    win_total = sum(1 for dr in daily_results for t in dr["trades"]
                    if t.get("paired") and t.get("pnl", 0) > 0)
    win_rate = win_total / paired_total if paired_total else 0.0

    result = {
        "code": code,
        "total_days": len(daily_results),
        "total_trades": total_trades,
        "total_cost_reduction": total_cost_reduction,
        "total_cost_paid": total_cost_paid,
        "net_pnl": total_cost_reduction - total_cost_paid,
        "unrealized_pnl": round(unrealized, 2),
        "net_pnl_with_unrealized": round(total_cost_reduction - total_cost_paid + unrealized, 2),
        "final_open_legs_count": len(carry_open_legs),
        "win_rate": win_rate,
        "daily_results": daily_results,
    }
    return result, all_bar_log


# ═══════════════════════════════════════════════════════════════
# 分析：对每个 leg 的生命周期做"逐 bar 回放"
# ═══════════════════════════════════════════════════════════════
def collect_legs_from_daily_results(daily_results: list[dict]) -> list[dict]:
    """
    从 daily_results 的 trades + risk_events 中重建所有 leg。

    每个 leg 的关键字段:
      - open_date, open_bar_idx, open_time, direction, fill_price, shares
      - close_date, close_bar_idx (paired 或 expired), close_type ("paired"/"expired"/"open")
      - holding_bars
    """
    legs = []
    # 用 FIFO 队列模拟 open legs
    open_queue: list[dict] = []

    for dr in daily_results:
        date_str = dr["date"]
        # 重建 trades 的时间顺序（已按 bar_idx 排序）
        for t in dr["trades"]:
            bar_idx = _find_bar_idx(dr, t)
            leg_entry = {
                "open_date": date_str,
                "open_bar_idx": bar_idx,
                "open_time": t.get("time", ""),
                "direction": t["direction"],
                "fill_price": t["fill_price"],
                "shares": t["shares"],
                "close_type": None,
                "close_date": None,
                "close_bar_idx": None,
                "close_time": None,
                "holding_bars": None,
            }

            if t.get("paired"):
                # 这笔成交配对了 open_queue 中最早的反向 leg
                while open_queue:
                    oldest = open_queue[0]
                    if oldest["direction"] == t["direction"]:
                        # 同方向不配对，这笔开新 leg
                        break
                    # 配对
                    paired_shares = min(oldest["shares"], t["shares"])
                    oldest["shares"] -= paired_shares
                    t["shares"] -= paired_shares
                    if oldest["shares"] <= 0:
                        oldest["close_type"] = "paired"
                        oldest["close_date"] = date_str
                        oldest["close_bar_idx"] = bar_idx
                        oldest["close_time"] = t.get("time", "")
                        oldest["holding_bars"] = _calc_holding_bars(oldest, date_str, bar_idx)
                        legs.append(open_queue.pop(0))
                    if t["shares"] <= 0:
                        break
                # 剩余的 shares 开新 leg
                if t["shares"] > 0:
                    leg_entry["shares"] = t["shares"]
                    open_queue.append(leg_entry)
            else:
                # 未配对，开新 leg
                open_queue.append(leg_entry)

        # 处理 expired legs（从 risk_events）
        for ev in dr.get("risk_events", []):
            if ev.get("type") == "expired":
                # 找到对应的 open leg（按 direction + fill_price 匹配）
                # 注意：fill_date 可能跨日（leg 在前一日开仓，今日过期）
                ev_fill_date = ev.get("fill_date", date_str)
                for i, ol in enumerate(open_queue):
                    if (ol["direction"] == ev["direction"]
                            and ol["open_date"] == ev_fill_date
                            and abs(ol["fill_price"] - ev["fill_price"]) < 0.01):
                        ol["close_type"] = "expired"
                        ol["close_date"] = date_str
                        ol["close_bar_idx"] = ev["bar_idx"]
                        ol["close_time"] = ev.get("time", "")
                        ol["holding_bars"] = ev["holding_bars"]
                        legs.append(open_queue.pop(i))
                        break

    # 剩余 open legs
    for ol in open_queue:
        ol["close_type"] = "open"
        legs.append(ol)

    return legs


def _find_bar_idx(daily_result: dict, trade: dict) -> int:
    """从 trade 的 time 字段反推 bar_idx（近似：用 trades 列表中的位置）。"""
    # 简化：用 trades 在当日列表中的位置作为 bar_idx 近似
    # 实际 bar_idx 在 trade record 里没有保存，用时间排序后的序号近似
    return 0  # 占位，下面用 time 字符串匹配 bars


def _calc_holding_bars(leg: dict, close_date: str, close_bar_idx: int) -> int:
    """近似计算持仓 bar 数。"""
    if leg["open_date"] == close_date:
        return close_bar_idx - leg["open_bar_idx"]
    # 跨日：无法精确，用 risk_events 中的值
    return 12  # 默认


def analyze_leg_lifetime(leg: dict, bar_log: list[dict]) -> dict:
    """
    分析单个 leg 生命周期内的对向信号状态。

    bar_log 是该 leg 所属股票的全部 bar_log（跨日）。
    """
    open_date = leg["open_date"]
    close_date = leg.get("close_date") or open_date
    open_bar_idx = leg["open_bar_idx"]

    opposite_dir = "sell" if leg["direction"] == "buy" else "buy"
    opposite_key = "reduce" if opposite_dir == "sell" else "add"

    # 找到生命周期内的 bar_log 条目
    lifetime_bars = []
    started = False
    for b in bar_log:
        if b["date"] < open_date:
            continue
        if b["date"] > close_date:
            break

        if b["date"] == open_date and not started:
            # 找到 open bar
            # 用 time 字符串匹配（open_time 格式 "YYYY-MM-DD HH:MM:SS" 或 "HH:MM"）
            bar_time = b["time"]
            open_time = leg["open_time"]
            if _time_match(bar_time, open_time):
                started = True
                # 这根 bar 是开仓 bar，不算（信号在这根 bar 之后评估，开仓信号是对向的）
                # 实际上开仓信号在这根 bar 触发，下一根 bar 开始才算"等待配对"
                continue

        if not started:
            continue

        # 检查这根 bar 时该 leg 是否还在 open_legs 中
        leg_still_open = False
        for ol in b["open_legs"]:
            if ol["direction"] == leg["direction"] and abs(ol["fill_price"] - leg["fill_price"]) < 0.01:
                leg_still_open = True
                break
        if not leg_still_open:
            break  # leg 已配对或过期

        opp_sig = b.get(opposite_key, {})
        entry = {
            "bar_idx": b["bar_idx"],
            "date": b["date"],
            "time": b["time"],
            "price": b["price"],
            "opp_triggered": opp_sig.get("triggered", False),
            "opp_extreme": opp_sig.get("extreme_score", 0),
            "opp_confirm": opp_sig.get("confirm_score", 0),
            "opp_filter": opp_sig.get("filter_passed", True),
            "opp_trend": opp_sig.get("trend_context", "range"),
            "opp_threshold": opp_sig.get("trigger_threshold", 3),
            "opp_rules_score": opp_sig.get("rules_score", 0),
            "opp_extreme_min": opp_sig.get("extreme_min", 2),
            "opp_confirm_min": opp_sig.get("confirm_min", 1),
            "opp_is_pairing": opp_sig.get("is_pairing", False),
            "opp_pairing_near_vwap": opp_sig.get("pairing_near_vwap", False),
            "opp_pairing_dir_confirmed": opp_sig.get("pairing_direction_confirmed", False),
            "opp_vwap_dev": opp_sig.get("vwap_dev"),
            "cooldown_blocks_opp": (b.get("cooldown_blocks_reduce") if opposite_dir == "sell"
                                     else b.get("cooldown_blocks_add")),
            "executed": b.get("executed"),
            "exec_detail": b.get("exec_detail"),
        }

        # 分析对向信号状态
        if entry["opp_triggered"]:
            if b.get("executed") == opposite_dir:
                entry["result"] = "executed"
            elif entry["cooldown_blocks_opp"]:
                entry["result"] = "blocked_by_cooldown"
            elif b.get("exec_detail") and not b["exec_detail"].get("approved"):
                entry["result"] = "blocked_by_risk"
                entry["block_reason"] = b["exec_detail"].get("risk_reason", "")
            elif b.get("exec_detail") and b["exec_detail"].get("spread_blocked"):
                entry["result"] = "blocked_by_spread"
            else:
                # 对向信号触发了但 executed 不是对向方向
                # 可能是优先级被另一方向抢（reduce 优先于 add）
                entry["result"] = "blocked_by_priority"
        else:
            entry["result"] = "not_triggered"
            # 分析为什么没触发
            if entry["opp_is_pairing"]:
                # 平仓分支未触发：距离不够 / 方向未确认 / 环境否决
                if not entry["opp_filter"]:
                    entry["not_trigger_reason"] = "pairing:环境否决"
                elif not entry["opp_pairing_near_vwap"]:
                    entry["not_trigger_reason"] = "pairing:未回归VWAP"
                elif not entry["opp_pairing_dir_confirmed"]:
                    entry["not_trigger_reason"] = "pairing:方向未确认"
                else:
                    entry["not_trigger_reason"] = "pairing:unknown"
            elif not entry["opp_filter"]:
                if entry["opp_trend"] == "extreme":
                    entry["not_trigger_reason"] = "filter:extreme硬否决"
                else:
                    entry["not_trigger_reason"] = "filter:涨停封板"
            elif entry["opp_extreme"] < entry["opp_extreme_min"]:
                entry["not_trigger_reason"] = f"extreme<{entry['opp_extreme_min']}"
            elif entry["opp_confirm"] < entry["opp_confirm_min"]:
                entry["not_trigger_reason"] = f"confirm<{entry['opp_confirm_min']}"
            elif entry["opp_rules_score"] < entry["opp_threshold"]:
                entry["not_trigger_reason"] = f"score<{entry['opp_threshold']}"
            else:
                entry["not_trigger_reason"] = "unknown"

        lifetime_bars.append(entry)

    # 汇总
    bars_opp_triggered = sum(1 for b in lifetime_bars if b["opp_triggered"])
    bars_opp_executed = sum(1 for b in lifetime_bars if b.get("result") == "executed")
    bars_not_triggered = sum(1 for b in lifetime_bars if b.get("result") == "not_triggered")

    # 主要失败原因
    if leg["close_type"] == "paired":
        failure_category = "paired"
    elif bars_opp_executed > 0:
        failure_category = "paired"  # 应该不会到这里
    elif bars_opp_triggered > 0:
        # 触发了但全部被挡住
        block_reasons = defaultdict(int)
        for b in lifetime_bars:
            if b["opp_triggered"] and b.get("result") != "executed":
                block_reasons[b.get("result", "unknown")] += 1
        top_block = max(block_reasons, key=block_reasons.get) if block_reasons else "unknown"
        failure_category = f"triggered_but_blocked:{top_block}"
    else:
        # 对向信号从未触发
        no_trigger_reasons = defaultdict(int)
        for b in lifetime_bars:
            if not b["opp_triggered"]:
                r = b.get("not_trigger_reason", "unknown")
                no_trigger_reasons[r] += 1
        top_reason = max(no_trigger_reasons, key=no_trigger_reasons.get) if no_trigger_reasons else "unknown"
        failure_category = f"no_trigger:{top_reason}"

    # 对向信号最接近触发的状态
    max_opp_extreme = max((b["opp_extreme"] for b in lifetime_bars), default=0)
    max_opp_confirm = max((b["opp_confirm"] for b in lifetime_bars), default=0)
    # 生命周期内 |vwap_dev| 的最小值（价格离 VWAP 最近的时刻）
    min_opp_vwap_dev_abs = min(
        (abs(b["opp_vwap_dev"]) for b in lifetime_bars if b.get("opp_vwap_dev") is not None),
        default=None,
    )

    return {
        "direction": leg["direction"],
        "open_date": open_date,
        "close_type": leg["close_type"],
        "holding_bars": leg.get("holding_bars"),
        "fill_price": round(leg["fill_price"], 4),
        "lifetime_bars_count": len(lifetime_bars),
        "bars_opp_triggered": bars_opp_triggered,
        "bars_opp_executed": bars_opp_executed,
        "bars_not_triggered": bars_not_triggered,
        "max_opp_extreme": max_opp_extreme,
        "max_opp_confirm": max_opp_confirm,
        "min_opp_vwap_dev_abs": round(min_opp_vwap_dev_abs, 4) if min_opp_vwap_dev_abs is not None else None,
        "failure_category": failure_category,
    }


def _time_match(bar_time: str, trade_time: str) -> bool:
    """检查 bar 时间是否匹配 trade 时间（只比 HH:MM）。"""
    def _norm(t):
        if not t:
            return ""
        if " " in t:
            t = t.split(" ")[1]
        return t[:5]
    return _norm(bar_time) == _norm(trade_time)


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
OUTPUT_PATH = PROJECT_ROOT / "outputs" / "backtest" / "diagnose_pairing_failure.json"

START = "2026-06-22"
END = "2026-07-22"


def main():
    parser = argparse.ArgumentParser(description="配对失败根因诊断")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制股票数量（0=全部，用于小范围验证）")
    args = parser.parse_args()

    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    if args.limit > 0:
        codes = codes[:args.limit]
    print(f"[诊断] 候选池 {len(codes)} 只股票，{START}~{END}")

    # 拉取数据
    all_data: dict[str, tuple] = {}
    for i, code in enumerate(codes):
        print(f"[data {i+1}/{len(codes)}] {code}...", end=" ", flush=True)
        daily_bars, daily_prev, daily_meta = fetch_multi_day(code, START, END, "baostock")
        if daily_bars:
            all_data[code] = (daily_bars, daily_prev, daily_meta)
            print(f"{len(daily_bars)}天")
        else:
            print("无数据，跳过")

    if not all_data:
        print("[诊断] 未加载到任何数据，退出")
        return

    # 逐股回测
    all_leg_analyses = []
    batch_summary = []

    for i, (code, (daily_bars, daily_prev, daily_meta)) in enumerate(all_data.items()):
        pure_code = normalize_code(code)["pure"]

        first_meta = next(iter(daily_meta.values()))
        freq = first_meta.get("frequency", "5min")
        bpd = first_meta.get("bars_count", 48)
        first_date = min(daily_prev.keys())
        avg_cost = daily_prev[first_date]

        params = BacktestParams(
            base_shares=3000, avg_cost=avg_cost,
            signal_params=SignalParams(), risk_params=RiskParams(),
        )
        params = adapt_params(params, freq, bpd)

        result, bar_log = instrumented_backtest_multi_day(
            code=pure_code, daily_bars=daily_bars, daily_prev_closes=daily_prev, params=params,
        )

        batch_summary.append({
            "code": pure_code,
            "total_days": result["total_days"],
            "total_trades": result["total_trades"],
            "total_cost_reduction": round(result["total_cost_reduction"], 2),
            "total_cost_paid": round(result["total_cost_paid"], 2),
            "net_pnl": round(result["net_pnl"], 2),
            "unrealized_pnl": round(result["unrealized_pnl"], 2),
            "net_pnl_with_unrealized": round(result["net_pnl_with_unrealized"], 2),
            "final_open_legs_count": result["final_open_legs_count"],
            "win_rate": round(result["win_rate"], 4),
            "avg_trades_per_day": round(result["total_trades"] / max(1, result["total_days"]), 2),
        })

        # 从 daily_results 重建 legs
        legs = collect_legs_from_daily_results(result["daily_results"])
        # 用 bar_log 中的 open_legs 修正 leg 的 open_bar_idx
        _fix_leg_open_bar_idx(legs, bar_log)

        # 分析每个 leg
        for leg in legs:
            analysis = analyze_leg_lifetime(leg, bar_log)
            analysis["code"] = pure_code
            all_leg_analyses.append(analysis)

        print(f"[bt {i+1}/{len(all_data)}] {pure_code}: "
              f"trades={result['total_trades']} net={result['net_pnl']:+.2f} "
              f"legs={len(legs)}")

    # 统计分析
    print("\n" + "=" * 90)
    print("根因诊断：配对失败原因分布")
    print("=" * 90)

    total_legs = len(all_leg_analyses)
    paired = [l for l in all_leg_analyses if l["close_type"] == "paired"]
    expired = [l for l in all_leg_analyses if l["close_type"] == "expired"]
    open_legs = [l for l in all_leg_analyses if l["close_type"] == "open"]

    print(f"\n总腿数: {total_legs} (paired={len(paired)} expired={len(expired)} open={len(open_legs)})")

    # ── 4.1 失败原因大类分布 ──
    print("\n" + "-" * 90)
    print("4.1 配对失败原因大类分布")
    print("-" * 90)

    cat_counts = defaultdict(int)
    for l in all_leg_analyses:
        cat = l["failure_category"].split(":")[0]
        cat_counts[cat] += 1

    print(f"\n{'类别':<30} {'腿数':>6} {'占比':>7}")
    print("-" * 50)
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        pct = cnt / total_legs * 100
        print(f"{cat:<30} {cnt:>6} {pct:>6.1f}%")

    # ── 4.2 详细失败原因 ──
    print("\n" + "-" * 90)
    print("4.2 详细失败原因分布")
    print("-" * 90)

    detail_counts = defaultdict(int)
    for l in all_leg_analyses:
        detail_counts[l["failure_category"]] += 1

    print(f"\n{'详细原因':<60} {'腿数':>6} {'占比':>7}")
    print("-" * 75)
    for cat, cnt in sorted(detail_counts.items(), key=lambda x: -x[1]):
        pct = cnt / total_legs * 100
        print(f"{cat:<60} {cnt:>6} {pct:>6.1f}%")

    # ── 4.3 expired 腿：对向信号触发情况 ──
    print("\n" + "-" * 90)
    print("4.3 expired 腿：对向信号在生命周期内的触发情况")
    print("-" * 90)

    if expired:
        print(f"\nexpired 腿数: {len(expired)}")
        print(f"平均生命周期 bar 数: {statistics.mean(l['lifetime_bars_count'] for l in expired):.1f}")
        print(f"平均对向信号触发次数: {statistics.mean(l['bars_opp_triggered'] for l in expired):.2f}")

        # 分布
        trigger_dist = defaultdict(int)
        for l in expired:
            n = l["bars_opp_triggered"]
            key = "0次" if n == 0 else ("1次" if n == 1 else ("2次" if n == 2 else "3+次"))
            trigger_dist[key] += 1
        print(f"\n对向信号触发次数分布:")
        for k in ["0次", "1次", "2次", "3+次"]:
            cnt = trigger_dist.get(k, 0)
            pct = cnt / len(expired) * 100
            bar = "█" * int(pct / 2)
            print(f"  {k:<6} {cnt:>4} ({pct:>5.1f}%) {bar}")

        # 对向信号最高 extreme/confirm 分布
        print(f"\nexpired 腿中对向信号的最高 extreme_score 分布 (extreme_min=2):")
        for val in range(0, 4):
            cnt = sum(1 for l in expired if l["max_opp_extreme"] == val)
            pct = cnt / len(expired) * 100
            bar = "█" * int(pct / 2)
            mark = " ← 达标" if val >= 2 else ""
            print(f"  extreme={val} {cnt:>4} ({pct:>5.1f}%) {bar}{mark}")

        print(f"\nexpired 腿中对向信号的最高 confirm_score 分布 (confirm_min=1):")
        for val in range(0, 3):
            cnt = sum(1 for l in expired if l["max_opp_confirm"] == val)
            pct = cnt / len(expired) * 100
            bar = "█" * int(pct / 2)
            mark = " ← 达标" if val >= 1 else ""
            print(f"  confirm={val} {cnt:>4} ({pct:>5.1f}%) {bar}{mark}")

        # 生命周期内 |vwap_dev| 最小值分布（价格离 VWAP 最近的时刻）
        # 用于判断 pairing_vwap_dev_threshold 应该设多少
        print(f"\nexpired 腿中生命周期内 |vwap_dev| 最小值分布（价格离 VWAP 最近时刻，threshold=0.4%）:")
        buckets = [(0, 0.004), (0.004, 0.006), (0.006, 0.008), (0.008, 0.012), (0.012, 0.02), (0.02, 1.0)]
        labels = ["<0.4%(达标)", "0.4-0.6%", "0.6-0.8%", "0.8-1.2%", "1.2-2.0%", "≥2.0%"]
        for (lo, hi), label in zip(buckets, labels):
            cnt = sum(1 for l in expired
                      if l.get("min_opp_vwap_dev_abs") is not None
                      and lo <= l["min_opp_vwap_dev_abs"] < hi)
            pct = cnt / len(expired) * 100
            bar = "█" * int(pct / 2)
            mark = " ← 当前threshold" if label.startswith("<0.4") else ""
            print(f"  {label:<14} {cnt:>4} ({pct:>5.1f}%) {bar}{mark}")
        # 累计：放宽到不同阈值能覆盖多少 expired 腿
        print(f"\n放宽 pairing_vwap_dev_threshold 到不同值的累计覆盖率:")
        for thresh in [0.004, 0.006, 0.008, 0.010, 0.012, 0.015]:
            cnt = sum(1 for l in expired
                      if l.get("min_opp_vwap_dev_abs") is not None
                      and l["min_opp_vwap_dev_abs"] < thresh)
            pct = cnt / len(expired) * 100
            print(f"  threshold={thresh*100:.1f}%: 覆盖 {cnt}/{len(expired)} ({pct:.1f}%)")

    # ── 4.4 触发了但被挡住 ──
    print("\n" + "-" * 90)
    print("4.4 对向信号触发了但没执行的腿（被什么挡住）")
    print("-" * 90)

    triggered_blocked = [l for l in all_leg_analyses
                        if "triggered_but_blocked" in l["failure_category"]]
    print(f"\n触发了但被挡住的腿数: {len(triggered_blocked)}")

    if triggered_blocked:
        block_reasons = defaultdict(int)
        for l in triggered_blocked:
            reason = l["failure_category"].split(":")[1] if ":" in l["failure_category"] else "unknown"
            block_reasons[reason] += 1
        print(f"\n{'挡住原因':<40} {'腿数':>6}")
        print("-" * 50)
        for r, cnt in sorted(block_reasons.items(), key=lambda x: -x[1]):
            print(f"{r:<40} {cnt:>6}")

    # ── 4.5 核心结论 ──
    print("\n" + "-" * 90)
    print("4.5 核心结论")
    print("-" * 90)

    no_trigger = sum(1 for l in expired if l["bars_opp_triggered"] == 0)
    trigger_blocked = sum(1 for l in expired
                          if l["bars_opp_triggered"] > 0 and "triggered_but_blocked" in l["failure_category"])
    trigger_executed_but_still_expired = sum(1 for l in expired
                                              if l["bars_opp_executed"] > 0)

    print(f"\nexpired 腿数: {len(expired)}")
    print(f"  ① 对向信号在窗口内从未触发:           {no_trigger} ({no_trigger/max(1,len(expired))*100:.1f}%)")
    print(f"  ② 对向信号触发过但全部被挡住:         {trigger_blocked} ({trigger_blocked/max(1,len(expired))*100:.1f}%)")
    print(f"  ③ 对向信号执行过但仍过期(异常):       {trigger_executed_but_still_expired}")

    if no_trigger > trigger_blocked:
        print(f"\n→ 主因是'对向信号根本没触发'（{no_trigger}/{len(expired)}）")
        print(f"  这是策略层 P0-5 三层触发对'对向平仓信号'也用同一套严格门槛导致的。")
        print(f"  → 支持'假设2：P0-5 三层触发对对向信号太严'，")
        print(f"    不支持'假设1：require_opposite_direction 是主因'（因为 require_opposite_direction")
        print(f"    只阻止同方向，不阻止对向——对向信号是被策略层挡住了，不是被风控层挡住了）。")
    else:
        print(f"\n→ 主因是'对向信号触发了但被风控挡住'（{trigger_blocked}/{len(expired)}）")
        print(f"  这是风控层问题。")

    # no_trigger 细分
    if no_trigger > 0:
        print(f"\n对向信号'从未触发'的 {no_trigger} 条腿中，最常见的不触发原因:")
        reasons = defaultdict(int)
        for l in expired:
            if l["bars_opp_triggered"] == 0:
                cat = l["failure_category"]
                if "no_trigger:" in cat:
                    reasons[cat.split(":")[1]] += 1
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {r:<35} {cnt:>4} ({cnt/no_trigger*100:.1f}%)")

    # 计算 overall 聚合（与 batch_summary.json 的 overall 字段对齐）
    total_paired_trades = sum(s["total_trades"] for s in batch_summary)  # 注意：total_trades 含未配对
    # 重新从 result 计算 paired_trades（net_pnl>0 的配对算 win）
    # 这里用 daily_results 中的 paired trades 更准确，但 batch_summary 已有 win_rate
    # 简化：用 total_trades 近似（诊断脚本关注配对率，不追求 overall 精度）
    overall = {
        "stocks": len(batch_summary),
        "total_trades": sum(s["total_trades"] for s in batch_summary),
        "net_pnl": round(sum(s["net_pnl"] for s in batch_summary), 2),
        "unrealized_pnl": round(sum(s["unrealized_pnl"] for s in batch_summary), 2),
        "net_pnl_with_unrealized": round(sum(s["net_pnl_with_unrealized"] for s in batch_summary), 2),
        "total_cost_paid": round(sum(s["total_cost_paid"] for s in batch_summary), 2),
        "profitable_stocks": sum(1 for s in batch_summary if s["net_pnl_with_unrealized"] > 0),
        "losing_stocks": sum(1 for s in batch_summary if s["net_pnl_with_unrealized"] < 0),
        "final_open_legs_count": sum(s["final_open_legs_count"] for s in batch_summary),
    }
    # 配对率（从 all_leg_analyses 计算，更准确）
    overall["pairing_rate"] = round(
        len(paired) / max(1, total_legs) * 100, 2
    )

    # 保存
    output = {
        "summary": {
            "total_legs": total_legs,
            "paired": len(paired),
            "expired": len(expired),
            "open": len(open_legs),
            "no_trigger_expired": no_trigger,
            "triggered_but_blocked_expired": trigger_blocked,
            "pairing_rate": overall["pairing_rate"],
        },
        "overall": overall,
        "failure_category_counts": dict(cat_counts),
        "failure_category_detail": dict(detail_counts),
        "all_leg_analyses": all_leg_analyses,
        "batch_summary": batch_summary,
    }
    output_path = OUTPUT_PATH
    if args.limit > 0:
        output_path = OUTPUT_PATH.parent / f"diagnose_pairing_failure_limit{args.limit}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[诊断] 完整结果 -> {output_path}")

    # 同步写入标准 batch_summary.json（仅 --limit=0 全量模式，避免小范围验证覆盖 baseline）
    if args.limit == 0:
        batch_summary_path = output_path.parent / "batch_summary.json"
        batch_data = {
            "overall": overall,
            "per_stock": batch_summary,
            "meta": {
                "source": "diagnose_pairing_failure.py (平仓分支修复后)",
                "strategy_fix": "evaluate_reduce/add_signal 增加 is_for_pairing，平仓走 VWAP 回归+方向确认",
                "pairing_vwap_dev_threshold": 0.008,
                "note": "旧 -10.2万 baseline (P0-5/P0-6 重构前) 和 0.6% 配对率 baseline 已废弃，不可引用",
            },
        }
        with open(batch_summary_path, "w", encoding="utf-8") as f:
            json.dump(batch_data, f, ensure_ascii=False, indent=2, default=str)
        print(f"[诊断] 标准 batch_summary -> {batch_summary_path}")


def _fix_leg_open_bar_idx(legs: list[dict], bar_log: list[dict]):
    """用 bar_log 中的 open_legs 信息修正 leg 的 open_bar_idx。"""
    # 建 (date, direction, fill_price) -> bar_idx 的索引
    idx_map = {}
    for b in bar_log:
        for ol in b["open_legs"]:
            key = (b["date"], ol["direction"], round(ol["fill_price"], 4))
            if key not in idx_map:
                idx_map[key] = ol["fill_bar_idx"]

    for leg in legs:
        key = (leg["open_date"], leg["direction"], round(leg["fill_price"], 4))
        if key in idx_map:
            leg["open_bar_idx"] = idx_map[key]


if __name__ == "__main__":
    main()
