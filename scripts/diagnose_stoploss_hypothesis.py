#!/usr/bin/env python3
"""
任务1诊断：验证"没有止损导致赢小亏大"假设
==================================================
不修改任何 strategy/risk/execution 代码。通过 monkey-patch TradeLifecycle
捕获所有腿（含 max_favorable/max_adverse），再用 features.detect_market_regime
给每笔交易开仓时刻打 regime 标签。

修复 v2：
  - 修复跨日 leg 重复计数：只取 all closed_legs + 最后一个 lifecycle 的 open_legs
  - 修复 avg_cost：用首日 prev_close（与 CLI 一致），不用 min(daily_prev)
  - 修复 batch_unrealized 拼写错误
  - 记录 P0-6 趋势过滤对 5min 数据无效的事实（min_bars_for_trend=60 > 48 bars/day）
  - 尝试用前一日全量 bars + 当日 entry 前 bars 拼接做 regime 检测
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import BacktestParams, backtest_multi_day
from at0.strategy import SignalParams
from at0.risk import RiskParams
from at0.execution import TradeLifecycle
from at0.features import compute_reference_snapshot, detect_market_regime

# ═══════════════════════════════════════════════════════════════
# Monkey-patch: 注册所有 TradeLifecycle 实例
# ═══════════════════════════════════════════════════════════════
_all_lifecycles: list[TradeLifecycle] = []
_orig_init = TradeLifecycle.__init__


def _patched_init(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    _all_lifecycles.append(self)


TradeLifecycle.__init__ = _patched_init


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
# 时间匹配辅助
# ═══════════════════════════════════════════════════════════════
def _normalize_time(t: str) -> str:
    """归一化时间到 HH:MM。"""
    if not t:
        return ""
    if " " in t:
        t = t.split(" ")[1]
    return t[:5]


# ═══════════════════════════════════════════════════════════════
# 收集 unique legs（修复跨日重复计数）
# ═══════════════════════════════════════════════════════════════
def collect_unique_legs(daily_bars: dict, daily_prev: dict) -> tuple[list[dict], list[dict]]:
    """
    从 _all_lifecycles 收集去重后的 legs。

    跨日 leg 会在多个 lifecycle 中出现（每天 import/export），
    但只会进入某一个 lifecycle 的 closed_legs 一次。
    最后一个 lifecycle 的 open_legs 包含所有仍未配对的腿（最新状态）。

    返回: (closed_legs, open_legs) — 去重后的两条列表
    """
    # 收集所有 closed_legs（每个 leg 只在一个 lifecycle 中被关闭）
    all_closed: list = []
    for lc in _all_lifecycles:
        all_closed.extend(lc.closed_legs)

    # 最后一个 lifecycle 的 open_legs 是最新的未配对腿
    last_open: list = []
    if _all_lifecycles:
        last_open = list(_all_lifecycles[-1].open_legs)

    # 去重 closed_legs：理论上不应该重复，但防御性去重
    seen_keys = set()
    unique_closed = []
    for leg in all_closed:
        key = (leg.fill_date, leg.fill_time, leg.direction, leg.fill_price, leg.shares)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_closed.append(leg)

    # 从 open_legs 中排除已经出现在 closed_legs 中的（防止边界情况）
    open_legs_deduped = []
    for leg in last_open:
        key = (leg.fill_date, leg.fill_time, leg.direction, leg.fill_price, leg.shares)
        if key not in seen_keys:
            open_legs_deduped.append(leg)

    return unique_closed, open_legs_deduped


# ═══════════════════════════════════════════════════════════════
# 计算 regime（尝试用前一日 bars 拼接，绕过 min_bars_for_trend=60 限制）
# ═══════════════════════════════════════════════════════════════
def compute_regime_at_entry(
    leg,
    daily_bars: dict,
    daily_prev: dict,
    sorted_dates: list,
) -> str:
    """
    给一笔 leg 的开仓时刻计算 regime。

    5min 数据每天只有 48 根 bars，detect_market_regime 需要 60 根才判定趋势。
    解法：拼接前一日完整 bars + 当日 entry 前 bars，凑够 60+ 根。

    注意：VWAP/ATR 会变成 2 日累计值，与实际回测中的 1 日值不同，
    但 ADX/DMI（用于趋势判定）在 2 日窗口下仍然有意义。
    """
    fill_date = leg.fill_date
    fill_time = _normalize_time(leg.fill_time)

    if fill_date not in daily_bars:
        return "range"

    leg_bars = daily_bars[fill_date]
    entry_idx = None
    for idx, b in enumerate(leg_bars):
        if _normalize_time(b.get("time", "")) == fill_time:
            entry_idx = idx
            break

    if entry_idx is None:
        return "range"

    # 尝试拼接前一日 bars
    date_idx = sorted_dates.index(fill_date) if fill_date in sorted_dates else -1
    if date_idx <= 0:
        # 无前一日数据
        bars_up_to_entry = leg_bars[: entry_idx + 1]
    else:
        prev_date = sorted_dates[date_idx - 1]
        prev_bars = daily_bars.get(prev_date, [])
        # 拼接：前一日全量 + 当日至 entry
        bars_up_to_entry = prev_bars + leg_bars[: entry_idx + 1]

    if len(bars_up_to_entry) < 60:
        return "range"  # 数据仍然不足

    prev_c = daily_prev.get(fill_date, 0)
    # 用前一日的 prev_close 作为 prev_close（不是前二日的）
    # 这是近似——实际回测中用的是 fill_date 的 prev_close
    snap = compute_reference_snapshot(
        bars_up_to_entry,
        current_price=leg_bars[entry_idx]["close"],
        prev_close=prev_c,
    )
    if not snap:
        return "range"

    return detect_market_regime(snap)


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
BATCH_SUMMARY_PATH = PROJECT_ROOT / "outputs" / "backtest" / "batch_summary.json"
OUTPUT_PATH = PROJECT_ROOT / "outputs" / "backtest" / "diagnose_stoploss_hypothesis.json"

START = "2026-06-22"
END = "2026-07-22"


def main():
    # 1. 加载候选池
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    print(f"[诊断] 候选池 {len(codes)} 只股票，{START}~{END}")

    # 加载原始 batch_summary 用于对比
    original_summary = None
    if BATCH_SUMMARY_PATH.exists():
        with open(BATCH_SUMMARY_PATH, "r", encoding="utf-8") as f:
            original_summary = json.load(f)
        ov = original_summary.get('overall', {})
        print(f"[对比] 当前 batch_summary: net_pnl={ov.get('net_pnl', 0):.2f}, "
              f"trades={ov.get('total_trades', 0)}, "
              f"pairing_rate={ov.get('pairing_rate', 0)}%")
        meta = original_summary.get('meta', {})
        if meta.get('note'):
            print(f"       meta: {meta.get('source', '')}")

    # 2. 拉取数据
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

    # 3. 逐股回测，收集所有腿
    all_legs: list[dict] = []
    batch_pnl_summary = []
    for i, (code, (daily_bars, daily_prev, daily_meta)) in enumerate(all_data.items()):
        _all_lifecycles.clear()

        first_meta = next(iter(daily_meta.values()))
        freq = first_meta.get("frequency", "5min")
        bpd = first_meta.get("bars_count", 48)

        # 修复：用首日 prev_close 作为 avg_cost（与 CLI 一致）
        first_date = min(daily_prev.keys())
        avg_cost = daily_prev[first_date]

        params = BacktestParams(
            base_shares=3000,
            avg_cost=avg_cost,
            signal_params=SignalParams(),
            risk_params=RiskParams(),
        )
        params = adapt_params(params, freq, bpd)

        result = backtest_multi_day(
            code=normalize_code(code)["pure"],
            daily_bars=daily_bars,
            daily_prev_closes=daily_prev,
            params=params,
        )

        pure_code = normalize_code(code)["pure"]
        batch_pnl_summary.append({
            "code": pure_code,
            "total_trades": result["total_trades"],
            "net_pnl": result["net_pnl"],
            "unrealized_pnl": result["unrealized_pnl"],
            "net_pnl_with_unrealized": result["net_pnl_with_unrealized"],
        })

        # 修复：去重收集 legs
        sorted_dates = sorted(daily_bars.keys())
        closed_legs, open_legs = collect_unique_legs(daily_bars, daily_prev)

        # 收集所有腿，计算 regime
        last_close = result.get("daily_results", [{}])[-1].get("last_close", avg_cost)
        for leg in closed_legs + open_legs:
            regime = compute_regime_at_entry(leg, daily_bars, daily_prev, sorted_dates)

            # 浮盈浮亏（open legs）
            if leg.status.value == "open":
                if leg.direction == "buy":
                    unrealized = (last_close - leg.fill_price) * leg.shares
                else:
                    unrealized = (leg.fill_price - last_close) * leg.shares
            else:
                unrealized = 0.0

            fp = leg.fill_price if leg.fill_price > 0 else 1.0
            all_legs.append({
                "code": pure_code,
                "direction": leg.direction,
                "shares": leg.shares,
                "fill_price": round(leg.fill_price, 4),
                "fill_date": leg.fill_date,
                "fill_time": leg.fill_time,
                "status": leg.status.value,
                "paired_pnl": round(leg.paired_pnl, 2),
                "unrealized_pnl": round(unrealized, 2),
                "total_pnl": round(leg.paired_pnl + unrealized, 2),
                "holding_bars": leg.holding_bars,
                "max_favorable": round(leg.max_favorable, 4),
                "max_adverse": round(leg.max_adverse, 4),
                "max_favorable_pct": round(leg.max_favorable / fp * 100, 3),
                "max_adverse_pct": round(leg.max_adverse / fp * 100, 3),
                "regime": regime,
            })

        print(f"[bt {i+1}/{len(all_data)}] {pure_code}: "
              f"trades={result['total_trades']} net={result['net_pnl']:+.2f} "
              f"unrealized={result['unrealized_pnl']:+.2f} "
              f"closed_legs={len(closed_legs)} open_legs={len(open_legs)}")

    # 4. 统计分析
    print("\n" + "=" * 80)
    print("任务1诊断：'没有止损导致赢小亏大'假设验证")
    print("=" * 80)

    # 4.1 总览
    total_legs = len(all_legs)
    paired = [l for l in all_legs if l["status"] == "paired"]
    expired = [l for l in all_legs if l["status"] == "expired"]
    open_legs = [l for l in all_legs if l["status"] == "open"]
    stopped = [l for l in all_legs if l["status"] == "stopped"]
    print(f"\n总腿数（去重）: {total_legs} "
          f"(paired={len(paired)} expired={len(expired)} open={len(open_legs)} stopped={len(stopped)})")

    batch_net = sum(s["net_pnl"] for s in batch_pnl_summary)
    batch_unreal = sum(s["unrealized_pnl"] for s in batch_pnl_summary)
    print(f"批量已实现净盈亏: {batch_net:+.2f}")
    print(f"批量含浮盈净盈亏: {batch_net + batch_unreal:+.2f}")

    if original_summary:
        o = original_summary["overall"]
        print(f"\n对比当前 batch_summary（平仓分支修复后）:")
        print(f"  baseline: trades={o.get('total_trades', 0)} "
              f"net={o.get('net_pnl', 0):+.2f} pairing_rate={o.get('pairing_rate', 0)}%")
        print(f"  当前: trades={sum(s['total_trades'] for s in batch_pnl_summary)} "
              f"legs={total_legs} net={batch_net:+.2f}")

    # 4.2 max_favorable vs max_adverse 分布
    print("\n" + "-" * 80)
    print("4.2 max_favorable vs max_adverse 分布（百分比，跨股票可比）")
    print("-" * 80)

    # 只对有实际价格偏移的腿做统计（排除 fill_price=0 的异常）
    valid_legs = [l for l in all_legs if l["max_favorable_pct"] > 0 or l["max_adverse_pct"] > 0]
    mf_pct = [l["max_favorable_pct"] for l in valid_legs]
    ma_pct = [l["max_adverse_pct"] for l in valid_legs]

    min_capture = 0.6
    print(f"\n止盈目标参考: min_capture_spread ≈ {min_capture}%")
    print(f"有效腿数: {len(valid_legs)} (排除无偏移腿)")
    print(f"\n{'指标':<20} {'均值':>8} {'中位数':>8} {'P25':>8} {'P75':>8} {'P90':>8} {'最大':>8}")
    print("-" * 80)

    def _stats(vals):
        if not vals:
            return {"mean": 0, "median": 0, "p25": 0, "p75": 0, "p90": 0, "max": 0}
        sv = sorted(vals)
        n = len(sv)
        return {
            "mean": statistics.mean(sv),
            "median": statistics.median(sv),
            "p25": sv[int(n * 0.25)],
            "p75": sv[int(n * 0.75)],
            "p90": sv[int(n * 0.90)],
            "max": sv[-1],
        }

    mf_s = _stats(mf_pct)
    ma_s = _stats(ma_pct)
    print(f"{'max_favorable(%)':<20} {mf_s['mean']:>8.3f} {mf_s['median']:>8.3f} "
          f"{mf_s['p25']:>8.3f} {mf_s['p75']:>8.3f} {mf_s['p90']:>8.3f} {mf_s['max']:>8.3f}")
    print(f"{'max_adverse(%)':<20} {ma_s['mean']:>8.3f} {ma_s['median']:>8.3f} "
          f"{ma_s['p25']:>8.3f} {ma_s['p75']:>8.3f} {ma_s['p90']:>8.3f} {ma_s['max']:>8.3f}")
    ratio = ma_s['mean'] / mf_s['mean'] if mf_s['mean'] > 0 else 0
    print(f"{'不利/有利 均值比':<20} {ratio:>8.2f}x")

    # 分桶统计
    print(f"\n分桶统计（不利偏移 max_adverse_pct 分布）:")
    buckets = [0, 0.3, 0.6, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 999]
    bucket_labels = ["<0.3%", "0.3-0.6%", "0.6-1.0%", "1.0-1.5%", "1.5-2.0%",
                     "2.0-3.0%", "3.0-5.0%", "5.0-10%", ">10%"]
    for j in range(len(buckets) - 1):
        cnt = sum(1 for v in ma_pct if buckets[j] <= v < buckets[j + 1])
        bar = "█" * int(cnt / max(1, len(ma_pct)) * 50)
        print(f"  {bucket_labels[j]:<12} {cnt:>4} ({cnt/max(1,len(ma_pct))*100:>5.1f}%) {bar}")

    print(f"\n分桶统计（有利偏移 max_favorable_pct 分布）:")
    for j in range(len(buckets) - 1):
        cnt = sum(1 for v in mf_pct if buckets[j] <= v < buckets[j + 1])
        bar = "█" * int(cnt / max(1, len(mf_pct)) * 50)
        print(f"  {bucket_labels[j]:<12} {cnt:>4} ({cnt/max(1,len(mf_pct))*100:>5.1f}%) {bar}")

    # 4.3 按 regime 分组统计
    print("\n" + "-" * 80)
    print("4.3 按 regime 分组统计（开仓时刻的市场状态）")
    print("-" * 80)
    print("注意：5min 数据每天 48 根，detect_market_regime 需 60 根，")
    print("      已用前一日 bars 拼接尝试绕过，但结果仍可能偏向 'range'。")

    regime_groups = defaultdict(list)
    for l in all_legs:
        regime_groups[l["regime"]].append(l)

    print(f"\n{'regime':<12} {'腿数':>6} {'净盈亏':>10} {'胜腿':>6} {'亏腿':>6} "
          f"{'胜率':>7} {'均盈':>8} {'均亏':>8} {'盈亏比':>7} {'均不利%':>8}")
    print("-" * 90)

    regime_stats = {}
    for regime in ["range", "trend_up", "trend_down", "extreme"]:
        legs = regime_groups.get(regime, [])
        if not legs:
            print(f"{regime:<12} {0:>6} {0:>10.2f} {0:>6} {0:>6} {0:>6.1f}% {0:>8.2f} {0:>8.2f} {0:>7.2f} {0:>8.3f}")
            regime_stats[regime] = {"count": 0}
            continue

        pnls = [l["total_pnl"] for l in legs]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        net = sum(pnls)
        wr = len(wins) / len(pnls) * 100 if pnls else 0
        avg_win = statistics.mean(wins) if wins else 0
        avg_loss = statistics.mean(losses) if losses else 0
        pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
        avg_adverse = statistics.mean([l["max_adverse_pct"] for l in legs])

        print(f"{regime:<12} {len(legs):>6} {net:>+10.2f} {len(wins):>6} {len(losses):>6} "
              f"{wr:>6.1f}% {avg_win:>+8.2f} {avg_loss:>+8.2f} {pl_ratio:>7.2f} {avg_adverse:>8.3f}")
        regime_stats[regime] = {
            "count": len(legs),
            "net_pnl": round(net, 2),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(wr, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio != float("inf") else None,
            "avg_max_adverse_pct": round(avg_adverse, 3),
        }

    # 4.4 按 status 分组统计（新增）
    print("\n" + "-" * 80)
    print("4.4 按腿状态分组统计（paired / expired / open）")
    print("-" * 80)

    status_stats = {}
    for status_name, status_legs in [("paired", paired), ("expired", expired), ("open", open_legs)]:
        if not status_legs:
            status_stats[status_name] = {"count": 0}
            continue
        pnls = [l["total_pnl"] for l in status_legs]
        mf_vals = [l["max_favorable_pct"] for l in status_legs]
        ma_vals = [l["max_adverse_pct"] for l in status_legs]
        net = sum(pnls)
        avg_mf = statistics.mean(mf_vals) if mf_vals else 0
        avg_ma = statistics.mean(ma_vals) if ma_vals else 0
        avg_hold = statistics.mean([l["holding_bars"] for l in status_legs])

        print(f"  {status_name:<10} 腿数={len(status_legs):>4} 净盈亏={net:>+10.2f} "
              f"均有利={avg_mf:>6.3f}% 均不利={avg_ma:>6.3f}% 均持仓={avg_hold:>5.1f}根")
        status_stats[status_name] = {
            "count": len(status_legs),
            "net_pnl": round(net, 2),
            "avg_max_favorable_pct": round(avg_mf, 3),
            "avg_max_adverse_pct": round(avg_ma, 3),
            "avg_holding_bars": round(avg_hold, 1),
        }

    # 4.5 假设验证结论
    print("\n" + "-" * 80)
    print("4.5 假设验证结论")
    print("-" * 80)

    # 假设A: max_adverse 分布是否明显大于 max_favorable
    adverse_gt_favorable_mean = ma_s["mean"] > mf_s["mean"] * 1.3
    adverse_gt_capture = sum(1 for v in ma_pct if v > min_capture) / max(1, len(ma_pct))
    favorable_within_capture = sum(1 for v in mf_pct if v <= min_capture * 1.5) / max(1, len(mf_pct))

    print(f"\n假设A: 亏损方向偏移 >> 盈利方向偏移（赢小亏大）")
    print(f"  max_adverse 均值 {ma_s['mean']:.3f}% vs max_favorable 均值 {mf_s['mean']:.3f}%")
    print(f"  不利/有利均值比: {ratio:.2f}x")
    print(f"  max_adverse 超过止盈目标({min_capture}%)的腿占比: {adverse_gt_capture*100:.1f}%")
    print(f"  max_favorable 在止盈目标1.5倍以内的腿占比: {favorable_within_capture*100:.1f}%")
    print(f"  → {'支持' if adverse_gt_favorable_mean else '不支持'}假设A (阈值: 不利/有利 > 1.3x)")

    # 假设B: 亏损是否集中在 trend/extreme regime
    trend_legs = regime_groups.get("trend_up", []) + regime_groups.get("trend_down", []) + regime_groups.get("extreme", [])
    range_legs = regime_groups.get("range", [])
    trend_net = sum(l["total_pnl"] for l in trend_legs)
    range_net = sum(l["total_pnl"] for l in range_legs)

    print(f"\n假设B: 亏损集中在趋势/极端市")
    print(f"  range 状态: {len(range_legs)} 腿, 净盈亏 {range_net:+.2f}")
    print(f"  trend+extreme: {len(trend_legs)} 腿, 净盈亏 {trend_net:+.2f}")
    if trend_legs:
        print(f"  → {'支持' if trend_net < range_net else '不支持'}假设B")
    else:
        print(f"  → 无法判定（trend+extreme 腿数为 0，可能因 5min 数据 regime 检测限制）")

    # 额外发现：expired 占比
    expired_ratio = len(expired) / max(1, total_legs) * 100
    print(f"\n额外发现：expired 腿占比 {expired_ratio:.1f}%")
    print(f"  这些腿超时后只标记状态不产生真实平仓成交（eod_risk_disposal 明确不强制平仓）")
    print(f"  → 即使没有'赢小亏大'模式，高 expired 率 + 无止损 = 敞口失控风险")

    # 5. 保存完整结果
    output = {
        "summary": {
            "total_legs": total_legs,
            "paired": len(paired),
            "expired": len(expired),
            "open": len(open_legs),
            "stopped": len(stopped),
            "batch_realized_net_pnl": round(batch_net, 2),
            "batch_unrealized_pnl": round(batch_unreal, 2),
            "batch_net_with_unrealized": round(batch_net + batch_unreal, 2),
        },
        "original_batch_summary_comparison": {
            "baseline_net_pnl": original_summary["overall"].get("net_pnl") if original_summary else None,
            "baseline_total_trades": original_summary["overall"].get("total_trades") if original_summary else None,
            "baseline_pairing_rate": original_summary["overall"].get("pairing_rate") if original_summary else None,
            "current_net_pnl": round(batch_net, 2),
            "current_total_trades": sum(s["total_trades"] for s in batch_pnl_summary),
            "note": "baseline 为平仓分支修复后的 batch_summary",
        },
        "max_favorable_stats": {k: round(v, 3) for k, v in mf_s.items()},
        "max_adverse_stats": {k: round(v, 3) for k, v in ma_s.items()},
        "regime_stats": regime_stats,
        "status_stats": status_stats,
        "hypothesis_a": {
            "supported": adverse_gt_favorable_mean,
            "adverse_to_favorable_ratio": round(ratio, 2),
            "adverse_exceeds_capture_pct": round(adverse_gt_capture * 100, 1),
        },
        "hypothesis_b": {
            "supported": bool(trend_legs and trend_net < range_net),
            "trend_extreme_net": round(trend_net, 2),
            "range_net": round(range_net, 2),
            "trend_extreme_count": len(trend_legs),
            "note": "5min 数据 regime 检测受 min_bars_for_trend=60 限制",
        },
        "code_level_findings": {
            "stopped_status_defined_but_never_set": True,
            "no_price_based_stop_loss": True,
            "expired_does_not_generate_fill": True,
            "max_favorable_max_adverse_tracked_but_unused": True,
            "p0_6_trend_filter_nonfunctional_for_5min": True,
            "p0_6_reason": "detect_market_regime 要求 min_bars_for_trend=60，5min 每日仅 48 根",
        },
        "all_legs": all_legs,
        "batch_pnl_summary": batch_pnl_summary,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[诊断] 完整结果 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
