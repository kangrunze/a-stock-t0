#!/usr/bin/env python3
"""
方案C 离线回放验证：对比两个动态平仓阈值公式
================================================
对 98 条 expired 腿，用真实 bar 价格回放，回答两个问题：
  1. 每个公式能让多少条腿从"expire"变成"能配对"
  2. 这些"新增配对腿"的真实盈亏总和（用穿越阈值那一刻的 bar 价格 + 成本）

公式C1（距离锚定成本，推荐）:
  threshold = max(0.8%, |open_vwap_dev| - min_capture_spread)
  语义：价格回归到"仍能覆盖成本并赚一点"的位置就平仓

公式C2（开仓深度×0.5，对照）:
  threshold = max(0.8%, |open_vwap_dev| × 0.5)
  语义：价格回归开仓深度的一半就平仓

平仓触发条件（与 strategy.py 平仓分支一致）:
  |vwap_dev| <= threshold
  AND pairing_direction_confirmed=True（最近N根不创新极值）
  AND filter_passed=True（未涨停/跌停、非极端趋势）

关键：用穿越阈值那一刻的 bar 收盘价计算真实盈亏（含滑点+成本），
      不是假设"穿越阈值=盈利"。

输出: outputs/backtest/diagnose_formula_replay.json + 控制台打印
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import BacktestParams
from at0.strategy import SignalParams
from at0.risk import RiskParams, CostModel

from diagnose_pairing_failure import (
    instrumented_backtest_multi_day,
    collect_legs_from_daily_results,
    adapt_params,
    POOL_PATH, START, END,
)


# ═══════════════════════════════════════════════════════════════
# 公式定义
# ═══════════════════════════════════════════════════════════════
MIN_CAPTURE_SPREAD = 0.006   # 0.6%，来自 risk.py RiskParams.min_capture_spread
PAIRING_VWAP_DEV_FLOOR = 0.008  # 0.8%，当前 strategy.py 中的下限


def formula_c1_cost_anchored(open_vwap_dev_abs: float) -> float:
    """公式C1：距离锚定成本。threshold = max(floor, |open_dev| - min_capture_spread)"""
    return max(PAIRING_VWAP_DEV_FLOOR, open_vwap_dev_abs - MIN_CAPTURE_SPREAD)


def formula_c2_half_depth(open_vwap_dev_abs: float) -> float:
    """公式C2：开仓深度×0.5。threshold = max(floor, |open_dev| × 0.5)"""
    return max(PAIRING_VWAP_DEV_FLOOR, open_vwap_dev_abs * 0.5)


# ═══════════════════════════════════════════════════════════════
# 单条 expired 腿回放
# ═══════════════════════════════════════════════════════════════
def replay_leg(
    leg: dict,
    lifetime_bars: list[dict],
    cost_model: CostModel,
) -> dict:
    """
    对单条 expired 腿回放两个公式的效果。

    lifetime_bars: 该腿生命周期内所有 bar 的快照
                    每条含 (date, bar_idx, price, vwap_dev, trend_context,
                            pairing_direction_confirmed, filter_passed, is_pairing)

    返回:
      {
        "direction": ..., "fill_price": ..., "open_vwap_dev": ...,
        "min_vwap_dev_abs": ...,
        "c1": {threshold, triggered, trigger_bar, real_pnl, close_cost},
        "c2": {threshold, triggered, trigger_bar, real_pnl, close_cost},
        "expire_real_pnl": ...,  # 原 expire 时刻的真实盈亏（对照）
      }
    """
    direction = leg["direction"]
    fill_price = leg["fill_price"]
    shares = leg["shares"]

    # 开仓时 |vwap_dev|
    open_vwap_dev_abs = abs(lifetime_bars[0]["vwap_dev"]) if lifetime_bars and lifetime_bars[0].get("vwap_dev") is not None else None

    # min |vwap_dev|（价格最接近 VWAP 的时刻）
    min_vwap_dev_abs = min(
        (abs(b["vwap_dev"]) for b in lifetime_bars if b.get("vwap_dev") is not None),
        default=None,
    )

    result = {
        "direction": direction,
        "fill_price": round(fill_price, 4),
        "shares": shares,
        "open_vwap_dev_abs": round(open_vwap_dev_abs * 100, 3) if open_vwap_dev_abs is not None else None,
        "min_vwap_dev_abs": round(min_vwap_dev_abs * 100, 3) if min_vwap_dev_abs is not None else None,
        "lifetime_bars_count": len(lifetime_bars),
    }

    # 两个公式分别回放
    for name, formula_fn in [("c1_cost_anchored", formula_c1_cost_anchored),
                              ("c2_half_depth", formula_c2_half_depth)]:
        if open_vwap_dev_abs is None:
            result[name] = {"threshold": None, "triggered": False, "reason": "no_open_vwap_dev"}
            continue

        threshold = formula_fn(open_vwap_dev_abs)
        trigger_bar = None

        # 找到第一根满足全部平仓条件的 bar
        for b in lifetime_bars:
            vwap_dev_abs = abs(b["vwap_dev"]) if b.get("vwap_dev") is not None else None
            if vwap_dev_abs is None:
                continue
            if vwap_dev_abs > threshold:
                continue
            # 平仓需要方向确认 + 环境通过
            if not b.get("pairing_direction_confirmed"):
                continue
            if not b.get("filter_passed"):
                continue
            trigger_bar = b
            break

        if trigger_bar is None:
            result[name] = {
                "threshold_pct": round(threshold * 100, 3),
                "triggered": False,
                "reason": "no_bar_crossed_threshold",
            }
            continue

        # 用穿越时刻的 bar 价格计算真实盈亏
        trigger_price = trigger_bar["price"]
        close_dir = "sell" if direction == "buy" else "buy"
        close_fill_price = cost_model.fill_price(close_dir, trigger_price)
        close_cost = cost_model.calc_cost(close_dir, shares, close_fill_price)

        if direction == "buy":
            gross_pnl = (close_fill_price - fill_price) * shares
        else:
            gross_pnl = (fill_price - close_fill_price) * shares
        real_pnl = gross_pnl - close_cost

        result[name] = {
            "threshold_pct": round(threshold * 100, 3),
            "triggered": True,
            "trigger_date": trigger_bar.get("date"),
            "trigger_time": trigger_bar.get("time"),
            "trigger_price": round(trigger_price, 4),
            "trigger_vwap_dev": round(trigger_bar.get("vwap_dev", 0) * 100, 3),
            "holding_bars_to_trigger": trigger_bar.get("bar_idx_in_lifetime", 0),
            "real_pnl": round(real_pnl, 2),
            "close_cost": round(close_cost, 2),
            "gross_pnl": round(gross_pnl, 2),
        }

    # 原 expire 时刻真实盈亏（对照）
    expire_bar = lifetime_bars[-1] if lifetime_bars else None
    if expire_bar and expire_bar.get("price") is not None:
        expire_price = expire_bar["price"]
        close_dir = "sell" if direction == "buy" else "buy"
        close_fill_price = cost_model.fill_price(close_dir, expire_price)
        close_cost = cost_model.calc_cost(close_dir, shares, close_fill_price)
        if direction == "buy":
            gross_pnl = (close_fill_price - fill_price) * shares
        else:
            gross_pnl = (fill_price - close_fill_price) * shares
        result["expire_real_pnl"] = round(gross_pnl - close_cost, 2)
    else:
        result["expire_real_pnl"] = None

    return result


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def main():
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    print(f"[回放验证] 候选池 {len(codes)} 只股票，{START}~{END}")
    print(f"[回放验证] 目标：对比 C1(距离锚定成本) vs C2(开仓深度×0.5) 两个公式的效果\n")

    cost_model = CostModel.base()
    all_replays = []

    for i, code in enumerate(codes):
        daily_bars, daily_prev, daily_meta = fetch_multi_day(code, START, END, "baostock")
        if not daily_bars:
            continue

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

        legs = collect_legs_from_daily_results(result["daily_results"])
        expired_legs = [l for l in legs if l["close_type"] == "expired"]

        for leg in expired_legs:
            direction = leg["direction"]
            fill_price = leg["fill_price"]

            # 平仓方向：buy leg 用 reduce 平仓，sell leg 用 add 平仓
            close_signal_key = "reduce" if direction == "buy" else "add"

            # 收集生命周期内所有 bar
            lifetime_bars = []
            bar_idx_in_lifetime = 0
            for b in bar_log:
                in_open = any(
                    ol["direction"] == direction
                    and abs(ol["fill_price"] - fill_price) < 0.01
                    for ol in b.get("open_legs", [])
                )
                if not in_open:
                    continue

                sig = b.get(close_signal_key, {})
                vwap_dev = sig.get("vwap_dev")
                bar_info = {
                    "date": b.get("date"),
                    "time": b.get("time"),
                    "bar_idx": b.get("bar_idx"),
                    "bar_idx_in_lifetime": bar_idx_in_lifetime,
                    "price": b.get("price"),
                    "vwap_dev": vwap_dev,
                    "trend_context": sig.get("trend_context"),
                    "pairing_near_vwap": sig.get("pairing_near_vwap"),
                    "pairing_direction_confirmed": sig.get("pairing_direction_confirmed"),
                    "filter_passed": sig.get("filter_passed"),
                    "is_pairing": sig.get("is_pairing"),
                }
                lifetime_bars.append(bar_info)
                bar_idx_in_lifetime += 1

            if not lifetime_bars:
                continue

            replay = replay_leg(leg, lifetime_bars, cost_model)
            replay["code"] = pure_code
            replay["open_date"] = leg["open_date"]
            replay["close_date"] = leg.get("close_date", "")
            all_replays.append(replay)

        print(f"[{i+1}/{len(codes)}] {pure_code}: expired={len(expired_legs)}")

    # ═══════════════════════════════════════════════════════════════
    # 汇总对比
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "=" * 90)
    print("方案C 离线回放验证结果（98 条 expired 腿）")
    print("=" * 90)

    total = len(all_replays)
    print(f"\n总数: {total} 条 expired 腿")

    # 原 expire 真实盈亏（baseline）
    expire_pnls = [r["expire_real_pnl"] for r in all_replays if r.get("expire_real_pnl") is not None]
    expire_total = sum(expire_pnls)
    print(f"\n[对照] 原 expire 时刻真实盈亏总和: {expire_total:+.2f}")
    print(f"       盈利腿数: {sum(1 for p in expire_pnls if p > 0)} / {len(expire_pnls)}")

    # 两个公式对比
    for name, label in [("c1_cost_anchored", "C1 距离锚定成本 (open_dev - 0.6%)"),
                         ("c2_half_depth", "C2 开仓深度×0.5")]:
        triggered = [r for r in all_replays if r.get(name, {}).get("triggered")]
        not_triggered = [r for r in all_replays if not r.get(name, {}).get("triggered")]
        triggered_pnls = [r[name]["real_pnl"] for r in triggered]
        triggered_total = sum(triggered_pnls) if triggered_pnls else 0
        wins = sum(1 for p in triggered_pnls if p > 0)
        losses = sum(1 for p in triggered_pnls if p < 0)

        # 未触发的腿仍按 expire 处理
        not_triggered_expire_pnls = [
            r["expire_real_pnl"] for r in not_triggered
            if r.get("expire_real_pnl") is not None
        ]
        not_triggered_total = sum(not_triggered_expire_pnls) if not_triggered_expire_pnls else 0

        # 新总盈亏 = 触发的真实盈亏 + 未触发的 expire 真实盈亏
        new_total = triggered_total + not_triggered_total

        print(f"\n[{label}]")
        print(f"  触发平仓的腿数: {len(triggered)} / {total} ({len(triggered)/total*100:.1f}%)")
        if triggered:
            print(f"  触发腿真实盈亏总和: {triggered_total:+.2f}")
            print(f"    盈利: {wins} ({wins/len(triggered)*100:.1f}%)")
            print(f"    亏损: {losses} ({losses/len(triggered)*100:.1f}%)")
            print(f"    均盈: {statistics.mean([p for p in triggered_pnls if p > 0]):+.2f}" if any(p > 0 for p in triggered_pnls) else "    均盈: N/A")
            print(f"    均亏: {statistics.mean([p for p in triggered_pnls if p < 0]):+.2f}" if any(p < 0 for p in triggered_pnls) else "    均亏: N/A")
            print(f"  触发腿平均持仓 bar: {statistics.mean([r[name]['holding_bars_to_trigger'] for r in triggered]):.1f}")
        print(f"  未触发腿(仍 expire)真实盈亏: {not_triggered_total:+.2f}")
        print(f"  新总盈亏(触发+未触发): {new_total:+.2f}")
        delta_vs_expire = new_total - expire_total
        print(f"  相对原 expire 总盈亏变化: {delta_vs_expire:+.2f}")

    # 详细对比：两个公式各自触发的腿
    c1_triggered = set(r["code"] + r["open_date"] + r.get("direction","") for r in all_replays if r.get("c1_cost_anchored", {}).get("triggered"))
    c2_triggered = set(r["code"] + r["open_date"] + r.get("direction","") for r in all_replays if r.get("c2_half_depth", {}).get("triggered"))
    print(f"\n两公式触发腿集合对比:")
    print(f"  C1 触发: {len(c1_triggered)} 条")
    print(f"  C2 触发: {len(c2_triggered)} 条")
    print(f"  C1 ∩ C2: {len(c1_triggered & c2_triggered)} 条")
    print(f"  C1 - C2: {len(c1_triggered - c2_triggered)} 条 (C1 触发但 C2 不触发)")
    print(f"  C2 - C1: {len(c2_triggered - c1_triggered)} 条 (C2 触发但 C1 不触发)")

    # C1 触发腿的 threshold vs open_dev vs min_dev 详细
    print(f"\nC1 触发腿详细（前10条）:")
    c1_triggered_legs = [r for r in all_replays if r.get("c1_cost_anchored", {}).get("triggered")]
    c1_triggered_legs.sort(key=lambda r: r["open_vwap_dev_abs"] or 0)
    for r in c1_triggered_legs[:10]:
        c1 = r["c1_cost_anchored"]
        print(f"  {r['code']} {r['open_date']} {r['direction']}: "
              f"open_dev={r['open_vwap_dev_abs']:.3f}% "
              f"thr={c1['threshold_pct']:.3f}% "
              f"trigger_dev={c1.get('trigger_vwap_dev', 0):.3f}% "
              f"pnl={c1['real_pnl']:+.2f} "
              f"hold={c1.get('holding_bars_to_trigger', 0)}bar")

    # 保存
    output = {
        "summary": {
            "total_expired_legs": total,
            "expire_baseline_total_pnl": round(expire_total, 2),
            "formulas": {
                "c1_cost_anchored": {
                    "formula": "max(0.8%, |open_vwap_dev| - 0.6%)",
                    "triggered_count": sum(1 for r in all_replays if r.get("c1_cost_anchored", {}).get("triggered")),
                    "triggered_total_pnl": round(sum(r["c1_cost_anchored"]["real_pnl"] for r in all_replays if r.get("c1_cost_anchored", {}).get("triggered")), 2),
                },
                "c2_half_depth": {
                    "formula": "max(0.8%, |open_vwap_dev| × 0.5)",
                    "triggered_count": sum(1 for r in all_replays if r.get("c2_half_depth", {}).get("triggered")),
                    "triggered_total_pnl": round(sum(r["c2_half_depth"]["real_pnl"] for r in all_replays if r.get("c2_half_depth", {}).get("triggered")), 2),
                },
            },
        },
        "all_replays": all_replays,
    }
    output_path = Path(__file__).resolve().parent.parent / "outputs" / "backtest" / "diagnose_formula_replay.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[回放验证] 完整结果 -> {output_path}")


if __name__ == "__main__":
    main()
