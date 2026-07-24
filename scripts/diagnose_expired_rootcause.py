#!/usr/bin/env python3
"""
Expired 腿根因分析
==================
对 98 条 expired 腿，从 bar_log 中提取生命周期内每根 bar 的平仓信号状态，
回答：为什么平仓信号没触发？

分类根因：
  - open_too_deep:  开仓时 |vwap_dev| 过深（>1.5%），12 根窗口内价格来不及回归
  - price_not_reverted:  生命周期内 |vwap_dev| 最小值仍 > 0.8%（pairing_near_vwap 始终 False）
  - direction_not_confirmed:  pairing_near_vwap=True 但 pairing_direction_confirmed=False
  - env_blocked:  pairing_near_vwap=True 且 direction_confirmed=True 但 filter_passed=False
  - unknown:  三层都通过但信号没触发（应为代码 bug）

输出: outputs/backtest/diagnose_expired_rootcause.json + 控制台打印
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
from at0.risk import RiskParams

from diagnose_pairing_failure import (
    instrumented_backtest_multi_day,
    collect_legs_from_daily_results,
    adapt_params,
    POOL_PATH, START, END,
)


def _classify_rootcause(lifetime_bars: list[dict], open_vwap_dev: float | None) -> str:
    """
    根据生命周期内平仓信号状态分类根因。

    lifetime_bars: 该 expired 腿生命周期内所有 bar 的平仓方向信号 snapshot
    open_vwap_dev: 开仓时 vwap_dev
    """
    if not lifetime_bars:
        return "no_lifetime_bars"

    # 开仓太深：open |vwap_dev| > 1.5%
    if open_vwap_dev is not None and abs(open_vwap_dev) > 0.015:
        # 进一步看 min |vwap_dev|
        min_abs = min(
            (abs(b["vwap_dev"]) for b in lifetime_bars if b.get("vwap_dev") is not None),
            default=None,
        )
        if min_abs is not None and min_abs > 0.008:
            return "open_too_deep"
        # 开仓深但曾经回归到 0.8% 以内，看为什么没触发
        near_vwap_bars = [b for b in lifetime_bars if b.get("pairing_near_vwap")]
        if not near_vwap_bars:
            return "open_too_deep"
        # 曾经 near_vwap，看 direction_confirmed
        dir_confirmed_bars = [b for b in near_vwap_bars if b.get("pairing_direction_confirmed")]
        if not dir_confirmed_bars:
            return "direction_not_confirmed"
        # direction 也确认了，看 filter
        filter_passed_bars = [b for b in dir_confirmed_bars if b.get("filter_passed")]
        if not filter_passed_bars:
            return "env_blocked"
        return "unknown_bug"

    # 开仓不深，看价格是否回归
    near_vwap_bars = [b for b in lifetime_bars if b.get("pairing_near_vwap")]
    if not near_vwap_bars:
        # pairing_near_vwap 始终 False = 价格没回归到 0.8% 以内
        min_abs = min(
            (abs(b["vwap_dev"]) for b in lifetime_bars if b.get("vwap_dev") is not None),
            default=None,
        )
        if min_abs is not None and min_abs > 0.012:
            return "price_not_reverted_far"
        return "price_not_reverted"

    # 曾经 near_vwap，看 direction_confirmed
    dir_confirmed_bars = [b for b in near_vwap_bars if b.get("pairing_direction_confirmed")]
    if not dir_confirmed_bars:
        return "direction_not_confirmed"
    # direction 也确认了，看 filter
    filter_passed_bars = [b for b in dir_confirmed_bars if b.get("filter_passed")]
    if not filter_passed_bars:
        return "env_blocked"
    return "unknown_bug"


def main():
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    print(f"[诊断] 候选池 {len(codes)} 只股票，{START}~{END}")
    print(f"[诊断] 目标：分析 expired 腿根因（为什么平仓信号没触发）\n")

    all_expired_analysis = []

    for i, code in enumerate(codes):
        daily_bars, daily_prev, daily_meta = fetch_multi_day(code, START, END, "baostock")
        if not daily_bars:
            print(f"[data {i+1}/{len(codes)}] {code}: 无数据，跳过")
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
            open_date = leg["open_date"]
            close_date = leg.get("close_date", "")

            # 平仓方向：buy leg 用 reduce 平仓，sell leg 用 add 平仓
            close_signal_key = "reduce" if direction == "buy" else "add"

            # 用 (direction, fill_price) 在 bar_log 的 open_legs 中匹配，找到生命周期内 bar
            lifetime_bars = []
            open_vwap_dev = None
            min_vwap_dev_abs = None
            min_vwap_dev_bar = None

            for b in bar_log:
                # 检查这根 bar 的 open_legs 是否包含目标 leg
                in_open = any(
                    ol["direction"] == direction
                    and abs(ol["fill_price"] - fill_price) < 0.01
                    for ol in b.get("open_legs", [])
                )
                if not in_open:
                    continue

                # 这根 bar 在生命周期内
                sig = b.get(close_signal_key, {})
                vwap_dev = sig.get("vwap_dev")
                trend_ctx = sig.get("trend_context")
                pairing_near = sig.get("pairing_near_vwap")
                pairing_dir = sig.get("pairing_direction_confirmed")
                filter_pass = sig.get("filter_passed")
                is_pairing = sig.get("is_pairing")

                bar_info = {
                    "date": b.get("date"),
                    "time": b.get("time"),
                    "price": b.get("price"),
                    "vwap_dev": vwap_dev,
                    "trend_context": trend_ctx,
                    "is_pairing": is_pairing,
                    "pairing_near_vwap": pairing_near,
                    "pairing_direction_confirmed": pairing_dir,
                    "filter_passed": filter_pass,
                }
                lifetime_bars.append(bar_info)

                # 开仓时 vwap_dev（第一根 bar）
                if open_vwap_dev is None:
                    open_vwap_dev = vwap_dev

                # |vwap_dev| 最小值
                if vwap_dev is not None:
                    abs_dev = abs(vwap_dev)
                    if min_vwap_dev_abs is None or abs_dev < min_vwap_dev_abs:
                        min_vwap_dev_abs = abs_dev
                        min_vwap_dev_bar = bar_info

            # 分类根因
            rootcause = _classify_rootcause(lifetime_bars, open_vwap_dev)

            # 统计 pairing_near_vwap=True 的 bar 数
            near_vwap_count = sum(1 for b in lifetime_bars if b.get("pairing_near_vwap"))
            dir_confirmed_count = sum(1 for b in lifetime_bars if b.get("pairing_direction_confirmed"))
            filter_passed_count = sum(1 for b in lifetime_bars if b.get("filter_passed"))

            # trend_context 分布
            trend_ctxs = [b.get("trend_context") for b in lifetime_bars if b.get("trend_context")]
            trend_counter = Counter(trend_ctxs)

            all_expired_analysis.append({
                "code": pure_code,
                "direction": direction,
                "fill_price": fill_price,
                "open_date": open_date,
                "close_date": close_date,
                "holding_bars": leg.get("holding_bars"),
                "lifetime_bars_count": len(lifetime_bars),
                "open_vwap_dev": round(open_vwap_dev * 100, 3) if open_vwap_dev is not None else None,
                "min_vwap_dev_abs": round(min_vwap_dev_abs * 100, 3) if min_vwap_dev_abs is not None else None,
                "min_vwap_dev_bar": min_vwap_dev_bar,
                "near_vwap_bars": near_vwap_count,
                "dir_confirmed_bars": dir_confirmed_count,
                "filter_passed_bars": filter_passed_count,
                "trend_context_dist": dict(trend_counter),
                "rootcause": rootcause,
            })

        print(f"[{i+1}/{len(codes)}] {pure_code}: expired={len(expired_legs)}")

    # 汇总
    print("\n" + "=" * 90)
    print("Expired 腿根因分布")
    print("=" * 90)

    total = len(all_expired_analysis)
    rootcause_counter = Counter(l["rootcause"] for l in all_expired_analysis)

    print(f"\n总数: {total} 条 expired 腿")
    print(f"\n根因分类:")
    for rc, count in rootcause_counter.most_common():
        pct = count / total * 100
        print(f"  {rc}: {count} ({pct:.1f}%)")

    # 开仓 vwap_dev 分布
    open_devs = [l["open_vwap_dev"] for l in all_expired_analysis if l["open_vwap_dev"] is not None]
    if open_devs:
        print(f"\n开仓时 |vwap_dev| 分布（%）:")
        abs_devs = [abs(d) for d in open_devs]
        sp = sorted(abs_devs)
        print(f"  均值: {statistics.mean(abs_devs):.3f}%")
        print(f"  中位数: {statistics.median(abs_devs):.3f}%")
        print(f"  P25: {sp[int(len(sp)*0.25)]:.3f}%")
        print(f"  P75: {sp[int(len(sp)*0.75)]:.3f}%")
        print(f"  范围: [{sp[0]:.3f}%, {sp[-1]:.3f}%]")

    # min |vwap_dev| 分布
    min_devs = [l["min_vwap_dev_abs"] for l in all_expired_analysis if l["min_vwap_dev_abs"] is not None]
    if min_devs:
        print(f"\n生命周期内 min|vwap_dev| 分布（%）:")
        sp = sorted(min_devs)
        print(f"  均值: {statistics.mean(min_devs):.3f}%")
        print(f"  中位数: {statistics.median(min_devs):.3f}%")
        print(f"  P25: {sp[int(len(sp)*0.25)]:.3f}%")
        print(f"  P75: {sp[int(len(sp)*0.75)]:.3f}%")
        print(f"  范围: [{sp[0]:.3f}%, {sp[-1]:.3f}%]")

    # 各根因的 min|vwap_dev| 中位数
    print(f"\n各根因的 min|vwap_dev| 中位数:")
    for rc in rootcause_counter:
        rc_legs = [l for l in all_expired_analysis if l["rootcause"] == rc]
        rc_mins = [l["min_vwap_dev_abs"] for l in rc_legs if l["min_vwap_dev_abs"] is not None]
        if rc_mins:
            print(f"  {rc}: median={statistics.median(rc_mins):.3f}% (n={len(rc_legs)})")

    # min_vwap_dev_bar 的平仓信号状态
    print(f"\nmin|vwap_dev| 时刻的平仓信号状态（价格离 VWAP 最近的时刻）:")
    near_vwap_at_min = sum(
        1 for l in all_expired_analysis
        if l.get("min_vwap_dev_bar", {}).get("pairing_near_vwap")
    )
    dir_conf_at_min = sum(
        1 for l in all_expired_analysis
        if l.get("min_vwap_dev_bar", {}).get("pairing_direction_confirmed")
    )
    filter_at_min = sum(
        1 for l in all_expired_analysis
        if l.get("min_vwap_dev_bar", {}).get("filter_passed")
    )
    print(f"  pairing_near_vwap=True: {near_vwap_at_min}/{total}")
    print(f"  direction_confirmed=True: {dir_conf_at_min}/{total}")
    print(f"  filter_passed=True: {filter_at_min}/{total}")

    # 保存
    output = {
        "summary": {
            "total_expired": total,
            "rootcause_distribution": dict(rootcause_counter),
        },
        "all_expired_analysis": all_expired_analysis,
    }
    output_path = Path(__file__).resolve().parent.parent / "outputs" / "backtest" / "diagnose_expired_rootcause.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[诊断] 完整结果 -> {output_path}")


if __name__ == "__main__":
    main()
