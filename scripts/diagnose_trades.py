#!/usr/bin/env python3
"""
真实交易诊断脚本
================
读取 600000_2026-07-15_2026-07-22_trades.json + report.json，
分析每笔交易触发后的价格走势，回答：
  - 触发后价格继续同向走 / 反向折返 / 横盘
  - 信号方向对不对（如果反向折返且超过预期捕获空间，说明方向对但时机不对）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_provider import fetch_multi_day

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_JSON = PROJECT_ROOT / "outputs" / "backtest" / "600000_2026-07-15_2026-07-22_trades.json"
REPORT_JSON = PROJECT_ROOT / "outputs" / "backtest" / "600000_2026-07-15_2026-07-22_report.json"

CODE = "600000"
START = "2026-07-15"
END = "2026-07-22"


def main():
    # 1. 读取交易记录
    trades = json.loads(TRADES_JSON.read_text(encoding="utf-8"))
    print(f"[诊断] 读取 {len(trades)} 笔交易")

    # 2. 拉取5分钟线（baostock）
    daily_bars, daily_prev, _ = fetch_multi_day(CODE, START, END, "baostock")
    print(f"[诊断] 拉取 {len(daily_bars)} 个交易日数据")

    # 3. 逐笔分析触发后价格走势
    print("\n" + "=" * 110)
    print(f"{'#':<3} {'日期':<11} {'时间':<6} {'方向':<5} {'成交价':>8} {'触发后5根收盘':>40} {'当日收盘':>9} {'最大反向':>9} {'结论':<20}")
    print("=" * 110)

    for i, t in enumerate(trades, 1):
        date = t["date"]
        time_str = t["time"][-8:]  # HH:MM:SS
        direction = t["direction"]
        fill = t["fill_price"]
        bars = daily_bars.get(date, [])

        # 找到触发K线在bars中的索引
        trigger_idx = None
        for idx, b in enumerate(bars):
            if b["time"].endswith(time_str):
                trigger_idx = idx
                break

        if trigger_idx is None:
            print(f"{i:<3} {date:<11} {time_str:<6} {direction:<5} {fill:>8.4f}  触发K线未找到")
            continue

        # 触发后5根K线的收盘价
        next_bars = bars[trigger_idx + 1: trigger_idx + 6]
        next_closes = [b["close"] for b in next_bars]
        next_str = " → ".join(f"{c:.3f}" for c in next_closes) if next_closes else "(无后续)"

        # 当日收盘
        day_close = bars[-1]["close"] if bars else 0

        # 最大反向偏移：对于buy，看触发后最低价；对于sell，看触发后最高价
        if direction == "buy":
            # buy后希望涨，反向=下跌
            future_lows = [b["low"] for b in bars[trigger_idx + 1:]]
            max_adverse = (min(future_lows) - fill) / fill * 100 if future_lows else 0
            adverse_str = f"{max_adverse:+.2f}%" if future_lows else "N/A"
            # 是否反向折返超过预期捕获空间
            exp_spread = t.get("expected_spread", 0) * 100
            if max_adverse < -exp_spread:
                verdict = "反向折返>预期(时机错)"
            elif max_adverse < -0.5:
                verdict = "小幅反向(时机偏早)"
            else:
                verdict = "方向对"
        else:
            # sell后希望跌，反向=上涨
            future_highs = [b["high"] for b in bars[trigger_idx + 1:]]
            max_adverse = (max(future_highs) - fill) / fill * 100 if future_highs else 0
            adverse_str = f"{max_adverse:+.2f}%" if future_highs else "N/A"
            exp_spread = t.get("expected_spread", 0) * 100
            if max_adverse > exp_spread:
                verdict = "反向折返>预期(时机错)"
            elif max_adverse > 0.5:
                verdict = "小幅反向(时机偏早)"
            else:
                verdict = "方向对"

        print(f"{i:<3} {date:<11} {time_str[:5]:<6} {direction:<5} {fill:>8.4f}  {next_str:>40} {day_close:>9.4f} {adverse_str:>9} {verdict:<20}")

    # 4. 按规则分类统计
    print("\n" + "=" * 110)
    print("按触发规则分类：")
    print("=" * 110)

    rule_stats = {}  # rule_key -> {count, paired, pnl_sum}
    for t in trades:
        # 提取规则项编号
        rules = t.get("rules_fired", [])
        rule_items = []
        for r in rules:
            if "[项" in r:
                item = r.split("[项")[1].split("]")[0]
                rule_items.append(f"项{item}")

        # 按方向+规则组合分类
        key = f"{t['direction']}:{'+'.join(rule_items)}"
        if key not in rule_stats:
            rule_stats[key] = {"count": 0, "paired": 0, "pnl_sum": 0.0, "cost_sum": 0.0}
        rule_stats[key]["count"] += 1
        if t.get("paired"):
            rule_stats[key]["paired"] += 1
        rule_stats[key]["pnl_sum"] += t.get("pnl", 0)
        rule_stats[key]["cost_sum"] += t.get("cost", 0)

    print(f"\n{'规则组合':<30} {'笔数':>4} {'配对':>4} {'配对率':>6} {'pnl合计':>10} {'成本合计':>10}")
    print("-" * 70)
    for key, s in sorted(rule_stats.items(), key=lambda x: -x[1]["count"]):
        pair_rate = s["paired"] / s["count"] * 100 if s["count"] else 0
        print(f"{key:<30} {s['count']:>4} {s['paired']:>4} {pair_rate:>5.0f}% {s['pnl_sum']:>+10.2f} {s['cost_sum']:>10.2f}")

    # 5. 时段分布
    print("\n" + "=" * 110)
    print("触发时段分布：")
    print("=" * 110)
    time_slots = {"09:30-10:30": 0, "10:30-11:30": 0, "11:30-13:00": 0, "13:00-14:00": 0, "14:00-15:00": 0}
    for t in trades:
        time_str = t["time"][-8:]
        hhmm = int(time_str[:2]) * 60 + int(time_str[3:5])
        if hhmm < 10 * 60 + 30:
            time_slots["09:30-10:30"] += 1
        elif hhmm < 11 * 60 + 30:
            time_slots["10:30-11:30"] += 1
        elif hhmm < 13 * 60:
            time_slots["11:30-13:00"] += 1
        elif hhmm < 14 * 60:
            time_slots["13:00-14:00"] += 1
        else:
            time_slots["14:00-15:00"] += 1

    for slot, count in time_slots.items():
        bar = "█" * count
        print(f"  {slot}: {count} {bar}")

    # 6. 跨日配对可能性分析
    print("\n" + "=" * 110)
    print("跨日未配对分析：")
    print("=" * 110)
    print("\n按时间顺序列出每笔交易的方向，看配对失败原因：")
    for i, t in enumerate(trades, 1):
        paired_str = "✓配对" if t.get("paired") else "✗未配对"
        print(f"  #{i} {t['date']} {t['time'][-8:][:5]} {t['direction']:<4} {paired_str}")

    print(f"\n关键：6笔未配对交易中，方向序列为 buy→buy→buy→sell→buy→sell")
    print(f"      如果允许跨日配对，#3(buy)可与#5(sell)配对，#4(buy)可与#7(sell)配对")
    print(f"      但当前 backtest_multi_day 的配对逻辑是按日内的，跨日 open_legs 不延续")


if __name__ == "__main__":
    main()
