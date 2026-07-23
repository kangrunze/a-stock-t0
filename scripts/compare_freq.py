#!/usr/bin/env python3
"""
1分钟线 vs 5分钟线 信号密度对比
================================
mootdx 连不通，退化为：
  - 5分钟线：baostock 历史6天（2026-07-15~2026-07-22）
  - 1分钟线：eastmoney 当日截至当前（2026-07-23 上午）

对比口径：每小时信号触发次数（归一化，消除时长差异）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from at0.data import fetch_multi_day, fetch_minute_bars
from at0.backtest import BacktestParams, backtest_single_day
from at0.strategy import SignalParams
from at0.risk import RiskParams
from at0.cli import adapt_params_by_frequency

CODE = "600000"
START_5MIN = "2026-07-15"
END_5MIN = "2026-07-22"
TODAY_1MIN = "2026-07-23"


def main():
    print("=" * 80)
    print("  1分钟线 vs 5分钟线 信号密度对比")
    print("=" * 80)

    # ── 5分钟线（baostock 历史6天）──
    print("\n[1] 5分钟线 (baostock, 2026-07-15~2026-07-22)...")
    daily_bars_5m, daily_prev_5m, daily_meta_5m = fetch_multi_day(
        CODE, START_5MIN, END_5MIN, "baostock"
    )

    params_5m = BacktestParams(
        base_shares=3000, avg_cost=8.95,
        signal_params=SignalParams(), risk_params=RiskParams(),
    )
    params_5m = adapt_params_by_frequency(params_5m, "5min", 48)

    total_trades_5m = 0
    total_bars_5m = 0
    total_hours_5m = 0
    print(f"\n  {'日期':<12} {'K线数':>5} {'交易数':>6} {'每小时触发':>10}")
    print("  " + "-" * 40)
    for date in sorted(daily_bars_5m.keys()):
        bars = daily_bars_5m[date]
        prev = daily_prev_5m[date]
        result = backtest_single_day(CODE, date, bars, prev, params_5m)
        n_trades = len(result["trades"])
        n_bars = len(bars)
        hours = n_bars * 5 / 60  # 5分钟线，每根5分钟
        rate = n_trades / hours if hours > 0 else 0
        total_trades_5m += n_trades
        total_bars_5m += n_bars
        total_hours_5m += hours
        print(f"  {date:<12} {n_bars:>5} {n_trades:>6} {rate:>9.2f}/h")

    avg_rate_5m = total_trades_5m / total_hours_5m if total_hours_5m > 0 else 0
    print(f"\n  5分钟线汇总: {total_trades_5m}笔 / {total_hours_5m:.1f}h = {avg_rate_5m:.2f}笔/小时")

    # ── 1分钟线（eastmoney 当日）──
    print(f"\n[2] 1分钟线 (eastmoney, {TODAY_1MIN} 截至当前)...")
    bars_1m, prev_1m, meta_1m = fetch_minute_bars(CODE, TODAY_1MIN, "eastmoney")
    print(f"  拉取 {len(bars_1m)} 根1分钟线 (source={meta_1m['source']})")

    if not bars_1m or prev_1m == 0:
        print("  [!] 1分钟线数据不足，无法对比")
        return

    params_1m = BacktestParams(
        base_shares=3000, avg_cost=prev_1m,
        signal_params=SignalParams(), risk_params=RiskParams(),
    )
    params_1m = adapt_params_by_frequency(params_1m, "1min", len(bars_1m))

    result_1m = backtest_single_day(CODE, TODAY_1MIN, bars_1m, prev_1m, params_1m)
    n_trades_1m = len(result_1m["trades"])
    hours_1m = len(bars_1m) * 1 / 60  # 1分钟线，每根1分钟
    rate_1m = n_trades_1m / hours_1m if hours_1m > 0 else 0

    print(f"  1分钟线: {n_trades_1m}笔 / {hours_1m:.1f}h = {rate_1m:.2f}笔/小时")

    if n_trades_1m > 0:
        print(f"\n  1分钟线交易明细:")
        for t in result_1m["trades"]:
            print(f"    {t['time'][-8:]} {t['direction']:<5} fill={t['fill_price']:.4f} "
                  f"score={t['rules_score']} rules={t['rules_fired']}")

    # ── 对比 ──
    print("\n" + "=" * 80)
    print("  对比结论")
    print("=" * 80)
    ratio = rate_1m / avg_rate_5m if avg_rate_5m > 0 else 0
    print(f"\n  5分钟线: {avg_rate_5m:.2f} 笔/小时 (历史6天平均)")
    print(f"  1分钟线: {rate_1m:.2f} 笔/小时 (当日半天)")
    print(f"  倍数:    1分钟线是5分钟线的 {ratio:.1f}x")

    if ratio > 2:
        print("\n  ⚠ 1分钟线信号密度显著高于5分钟线（>2x）")
        print("    说明当前阈值（尤其 vol_ratio_lookback=5/baseline=20）在1分钟粒度下")
        print("    窗口含义变化（5min×5=25分钟 vs 1min×5=5分钟），触发条件变宽松")
        print("    → 需要按数据粒度重新设计窗口参数，不能直接套用同一组参数")
    elif ratio < 0.5:
        print("\n  ⚠ 1分钟线信号密度显著低于5分钟线（<0.5x）")
        print("    可能原因：1分钟线噪声更多，指标更频繁地在阈值附近震荡，反而不满足≥3项规则")
    else:
        print(f"\n  信号密度差异在合理范围（{ratio:.1f}x），窗口参数暂不需按粒度调整")


if __name__ == "__main__":
    main()
