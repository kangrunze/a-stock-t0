#!/usr/bin/env python3
"""
数据诊断：分析51只股票5min K线特征，判断均值回归策略可行性。
纯手动计算，不依赖 features 层。
"""
import sys
import os
import json
import statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from at0.data import fetch_multi_day, normalize_code

# 加载51只回测结果
with open("outputs/backtest/cached_51_summary.json", encoding="utf-8") as f:
    bt_results = json.load(f)
bt_map = {s['code']: s for s in bt_results['per_stock']}

cache_dir = Path("data/multi_day_cache")
cached_codes = []
for f in os.listdir(cache_dir):
    if f.endswith(".json") and "2025-07-24" in f:
        pure = f.split("_")[0]
        cached_codes.append((pure, f"sh.{pure}" if pure.startswith("6") else f"sz.{pure}"))

results = []

for pure, code in sorted(cached_codes):
    try:
        daily_bars, daily_prev, daily_meta = fetch_multi_day(
            code, '2025-07-24', '2026-07-22', 'baostock', use_cache=True)
        if not daily_bars:
            continue

        bt = bt_map.get(pure, {})
        net_w_u = bt.get('net_pnl_with_unrealized', 0)

        deviation_events = []
        vol_amplitudes = []
        total_bars = 0
        trend_bar_count = 0  # 价格连续同方向走（简易趋势判断）

        for date, bars in daily_bars.items():
            if len(bars) < 30:
                continue
            total_bars += len(bars)

            # 逐根计算累计VWAP
            cum_vol = 0
            cum_pv = 0
            vwap_series = []
            for i, bar in enumerate(bars):
                typ_price = (bar['high'] + bar['low'] + bar['close']) / 3
                cum_vol += bar['volume']
                cum_pv += typ_price * bar['volume']
                vwap = cum_pv / cum_vol if cum_vol > 0 else bar['close']
                vwap_dev = (bar['close'] - vwap) / vwap if vwap > 0 else 0
                vwap_series.append((vwap, vwap_dev))

                # 振幅
                amp = (bar['high'] - bar['low']) / bar['close'] if bar['close'] > 0 else 0
                vol_amplitudes.append(amp)

            # 偏离事件分析：|dev| > 0.8% 后12根K线内是否回归到 |dev| < 0.3%
            for i in range(len(vwap_series)):
                vwap, dev = vwap_series[i]
                if abs(dev) > 0.008 and i < len(vwap_series) - 12:
                    reverted = False
                    revert_bars = 0
                    # 检查后续是否继续同方向偏离（趋势延续）
                    continued_same_dir = False
                    for j in range(i+1, min(i+13, len(vwap_series))):
                        _, dev_j = vwap_series[j]
                        if abs(dev_j) < 0.003:
                            reverted = True
                            revert_bars = j - i
                            break
                        # 价格继续朝同方向走（偏离更大）
                        if (dev > 0 and dev_j > dev) or (dev < 0 and dev_j < dev):
                            continued_same_dir = True
                    deviation_events.append({
                        'dev': dev,
                        'reverted': reverted,
                        'revert_bars': revert_bars,
                        'continued': continued_same_dir,
                    })

            # 简易趋势占比：5min收盘价连续3根同方向的比例
            closes = [b['close'] for b in bars]
            for i in range(2, len(closes)):
                if (closes[i] > closes[i-1] > closes[i-2]) or (closes[i] < closes[i-1] < closes[i-2]):
                    trend_bar_count += 1

        # 汇总
        dev_total = len(deviation_events)
        dev_reverted = sum(1 for e in deviation_events if e['reverted'])
        dev_continued = sum(1 for e in deviation_events if e['continued'])
        revert_rate = dev_reverted / dev_total if dev_total > 0 else 0
        continue_rate = dev_continued / dev_total if dev_total > 0 else 0
        avg_revert_bars = statistics.mean([e['revert_bars'] for e in deviation_events if e['reverted']]) if dev_reverted > 0 else 0
        trend_pct = trend_bar_count / total_bars if total_bars > 0 else 0
        avg_vol = statistics.mean(vol_amplitudes) if vol_amplitudes else 0

        results.append({
            'code': pure,
            'net_w_u': net_w_u,
            'total_bars': total_bars,
            'dev_events': dev_total,
            'revert_rate': revert_rate,
            'continue_rate': continue_rate,
            'avg_revert_bars': avg_revert_bars,
            'trend_pct': trend_pct,
            'avg_vol': avg_vol,
        })
    except Exception as e:
        print(f"{pure} ERROR: {e}")

# 输出
results.sort(key=lambda x: x['net_w_u'], reverse=True)
print(f"\n=== 51只股票5min K线特征诊断 ===\n")
print(f"{'股票':>8} {'net_w_u':>10} {'偏离事件':>8} {'回归率':>7} {'延续率':>7} {'回归K线':>8} {'趋势占比':>7} {'振幅':>6}")
print("-" * 72)
for r in results:
    print(f"{r['code']:>8} {r['net_w_u']:>+10.0f} {r['dev_events']:>8} "
          f"{r['revert_rate']*100:>6.1f}% {r['continue_rate']*100:>6.1f}% "
          f"{r['avg_revert_bars']:>8.1f} {r['trend_pct']*100:>6.1f}% "
          f"{r['avg_vol']*100:>5.2f}%")

# 整体统计
avg_revert = statistics.mean([r['revert_rate'] for r in results])
avg_continue = statistics.mean([r['continue_rate'] for r in results])
avg_trend = statistics.mean([r['trend_pct'] for r in results])
avg_vol = statistics.mean([r['avg_vol'] for r in results])
total_dev = sum(r['dev_events'] for r in results)

profitable = [r for r in results if r['net_w_u'] > 0]
losing = [r for r in results if r['net_w_u'] <= 0]

print(f"\n=== 整体统计 ===")
print(f"总偏离事件: {total_dev}")
print(f"平均回归率: {avg_revert*100:.1f}%  （偏离>0.8%后12根K线内回归到<0.3%的比例）")
print(f"平均延续率: {avg_continue*100:.1f}%  （偏离继续同方向扩大的比例）")
print(f"平均趋势占比: {avg_trend*100:.1f}%  （3根连续同方向的比例）")
print(f"平均5min振幅: {avg_vol*100:.2f}%")

print(f"\n=== 盈利({len(profitable)}) vs 亏损({len(losing)}) 特征对比 ===")
if profitable:
    print(f"盈利组: 回归率={statistics.mean([r['revert_rate'] for r in profitable])*100:.1f}% "
          f"延续率={statistics.mean([r['continue_rate'] for r in profitable])*100:.1f}% "
          f"趋势占比={statistics.mean([r['trend_pct'] for r in profitable])*100:.1f}% "
          f"振幅={statistics.mean([r['avg_vol'] for r in profitable])*100:.2f}%")
if losing:
    print(f"亏损组: 回归率={statistics.mean([r['revert_rate'] for r in losing])*100:.1f}% "
          f"延续率={statistics.mean([r['continue_rate'] for r in losing])*100:.1f}% "
          f"趋势占比={statistics.mean([r['trend_pct'] for r in losing])*100:.1f}% "
          f"振幅={statistics.mean([r['avg_vol'] for r in losing])*100:.2f}%")

with open("outputs/backtest/data_diagnosis_51.json", "w", encoding="utf-8") as f:
    json.dump({"results": results, "summary": {
        "total_dev": total_dev, "avg_revert_rate": avg_revert,
        "avg_continue_rate": avg_continue, "avg_trend_pct": avg_trend,
        "avg_vol": avg_vol,
    }}, f, ensure_ascii=False, indent=2)
print(f"\n已保存: outputs/backtest/data_diagnosis_51.json")
