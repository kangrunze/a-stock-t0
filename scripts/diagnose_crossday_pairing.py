#!/usr/bin/env python3
"""
跨日配对诊断：对比日内配对 vs 跨日配对，统计等待天数分布。
用于回答验收问题1：538笔未配对交易属于可能性A（策略特征）还是可能性B（风控缺失）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADES_DIR = PROJECT_ROOT / "outputs" / "backtest"


def load_all_trades() -> dict[str, list[dict]]:
    """加载所有股票的 trades。"""
    all_trades = {}
    for f in sorted(TRADES_DIR.glob("*_trades.json")):
        code = f.name.split("_")[0]
        with open(f, "r", encoding="utf-8") as fp:
            trades = json.load(fp)
        if trades:
            all_trades[code] = trades
    return all_trades


def simulate_intraday_pairing(trades: list[dict]) -> dict:
    """模拟旧版日内配对（每日重置 open_legs）。"""
    open_legs = []
    paired_count = 0
    unpaired_count = 0
    current_date = None

    for t in trades:
        if t["date"] != current_date:
            # 新交易日：重置 open_legs（旧版bug）
            unpaired_count += len(open_legs)
            open_legs = []
            current_date = t["date"]

        if t["direction"] == "buy":
            # 买腿：尝试与卖腿配对
            if open_legs and open_legs[0]["direction"] == "sell":
                open_legs.pop(0)
                paired_count += 1
            else:
                open_legs.append(t)
        else:  # sell
            if open_legs and open_legs[0]["direction"] == "buy":
                open_legs.pop(0)
                paired_count += 1
            else:
                open_legs.append(t)

    unpaired_count += len(open_legs)
    return {"paired": paired_count, "unpaired": unpaired_count, "total": len(trades)}


def simulate_crossday_pairing(trades: list[dict]) -> dict:
    """模拟跨日连续配对，统计等待天数分布。"""
    open_legs = []  # 不按日重置
    paired_count = 0
    wait_days_dist = {0: 0, 1: 0, 2: 0, 3: 0, "4_7": 0, "8_plus": 0, "never": 0}
    crossday_paired = 0
    same_day_paired = 0
    never_paired = 0

    from datetime import datetime

    for t in trades:
        if t["direction"] == "buy":
            if open_legs and open_legs[0]["direction"] == "sell":
                leg = open_legs.pop(0)
                paired_count += 1
                d1 = datetime.strptime(leg["date"], "%Y-%m-%d")
                d2 = datetime.strptime(t["date"], "%Y-%m-%d")
                wait = (d2 - d1).days
                if wait == 0:
                    same_day_paired += 1
                    wait_days_dist[0] += 1
                else:
                    crossday_paired += 1
                    if wait == 1:
                        wait_days_dist[1] += 1
                    elif wait == 2:
                        wait_days_dist[2] += 1
                    elif wait == 3:
                        wait_days_dist[3] += 1
                    elif wait <= 7:
                        wait_days_dist["4_7"] += 1
                    else:
                        wait_days_dist["8_plus"] += 1
            else:
                open_legs.append(t)
        else:  # sell
            if open_legs and open_legs[0]["direction"] == "buy":
                leg = open_legs.pop(0)
                paired_count += 1
                d1 = datetime.strptime(leg["date"], "%Y-%m-%d")
                d2 = datetime.strptime(t["date"], "%Y-%m-%d")
                wait = (d2 - d1).days
                if wait == 0:
                    same_day_paired += 1
                    wait_days_dist[0] += 1
                else:
                    crossday_paired += 1
                    if wait == 1:
                        wait_days_dist[1] += 1
                    elif wait == 2:
                        wait_days_dist[2] += 1
                    elif wait == 3:
                        wait_days_dist[3] += 1
                    elif wait <= 7:
                        wait_days_dist["4_7"] += 1
                    else:
                        wait_days_dist["8_plus"] += 1
            else:
                open_legs.append(t)

    never_paired = len(open_legs)
    wait_days_dist["never"] = never_paired

    return {
        "paired": paired_count,
        "same_day_paired": same_day_paired,
        "crossday_paired": crossday_paired,
        "never_paired": never_paired,
        "total": len(trades),
        "wait_days_dist": wait_days_dist,
    }


def main():
    all_trades = load_all_trades()
    if not all_trades:
        print("[ERROR] 未找到 trades 文件")
        sys.exit(1)

    print(f"加载 {len(all_trades)} 只股票的 trades")
    print("=" * 80)

    # 汇总
    total_intraday = {"paired": 0, "unpaired": 0, "total": 0}
    total_crossday = {"paired": 0, "same_day_paired": 0, "crossday_paired": 0,
                      "never_paired": 0, "total": 0,
                      "wait_days_dist": {0: 0, 1: 0, 2: 0, 3: 0, "4_7": 0, "8_plus": 0, "never": 0}}

    per_stock_results = []
    for code, trades in sorted(all_trades.items()):
        intra = simulate_intraday_pairing(trades)
        cross = simulate_crossday_pairing(trades)

        for k in total_intraday:
            total_intraday[k] += intra[k]
        total_crossday["paired"] += cross["paired"]
        total_crossday["same_day_paired"] += cross["same_day_paired"]
        total_crossday["crossday_paired"] += cross["crossday_paired"]
        total_crossday["never_paired"] += cross["never_paired"]
        total_crossday["total"] += cross["total"]
        for k in cross["wait_days_dist"]:
            total_crossday["wait_days_dist"][k] += cross["wait_days_dist"][k]

        per_stock_results.append({
            "code": code,
            "total": intra["total"],
            "intra_paired": intra["paired"],
            "intra_unpaired": intra["unpaired"],
            "cross_paired": cross["paired"],
            "cross_same_day": cross["same_day_paired"],
            "cross_crossday": cross["crossday_paired"],
            "cross_never": cross["never_paired"],
        })

    # 打印汇总
    print("\n" + "=" * 80)
    print("一、日内配对（旧版bug）vs 跨日配对（修复后）对比")
    print("=" * 80)
    print(f"  总交易腿数:        {total_intraday['total']}")
    print(f"  日内配对成功:      {total_intraday['paired']} ({total_intraday['paired']/total_intraday['total']*100:.1f}%)")
    print(f"  日内未配对(旧bug): {total_intraday['unpaired']} ({total_intraday['unpaired']/total_intraday['total']*100:.1f}%)")
    print()
    print(f"  跨日配对成功:      {total_crossday['paired']} ({total_crossday['paired']/total_crossday['total']*100:.1f}%)")
    print(f"    其中同日配对:    {total_crossday['same_day_paired']}")
    print(f"    其中跨日配对:    {total_crossday['crossday_paired']}")
    print(f"  跨日仍未配对:      {total_crossday['never_paired']} ({total_crossday['never_paired']/total_crossday['total']*100:.1f}%)")

    old_unpaired_legs = total_intraday["unpaired"]  # 腿数
    still_never_legs = total_crossday["never_paired"]  # 腿数
    rescued_legs = old_unpaired_legs - still_never_legs  # 日内未配对但跨日能配上的腿数
    # 注意: paired_count 统计的是"对数"(每对=2腿), unpaired/never 统计的是"腿数"
    # 所以这里统一用腿数计算

    print("\n" + "=" * 80)
    print("二、旧版未配对交易的归属分析（可能性A vs B）")
    print("=" * 80)
    print(f"  旧版日内未配对(腿): {old_unpaired_legs}")
    print(f"  其中跨日能配上(A): {rescued_legs} 腿 ({rescued_legs/old_unpaired_legs*100:.1f}%)")
    print(f"  其中永远配不上(B): {still_never_legs} 腿 ({still_never_legs/old_unpaired_legs*100:.1f}%)")

    print("\n" + "=" * 80)
    print("三、跨日配对交易的等待天数分布（按对数计，每对=2腿）")
    print("=" * 80)
    wd = total_crossday["wait_days_dist"]
    total_pairs = total_crossday["paired"]
    total_legs = total_crossday["total"]
    print(f"  同日配对(0天):     {wd[0]:4d} 对 / {wd[0]*2:4d} 腿 ({wd[0]*2/total_legs*100:.1f}%)")
    print(f"  等待1天:           {wd[1]:4d} 对 / {wd[1]*2:4d} 腿 ({wd[1]*2/total_legs*100:.1f}%)")
    print(f"  等待2天:           {wd[2]:4d} 对 / {wd[2]*2:4d} 腿 ({wd[2]*2/total_legs*100:.1f}%)")
    print(f"  等待3天:           {wd[3]:4d} 对 / {wd[3]*2:4d} 腿 ({wd[3]*2/total_legs*100:.1f}%)")
    print(f"  等待4-7天:         {wd['4_7']:4d} 对 / {wd['4_7']*2:4d} 腿 ({wd['4_7']*2/total_legs*100:.1f}%)")
    print(f"  等待8天以上:       {wd['8_plus']:4d} 对 / {wd['8_plus']*2:4d} 腿 ({wd['8_plus']*2/total_legs*100:.1f}%)")
    print(f"  永远未配对:        {'--':>4}     / {wd['never']:4d} 腿 ({wd['never']/total_legs*100:.1f}%)")

    # A/B 归类（统一用腿数）
    fast_legs = (wd[0] + wd[1] + wd[2] + wd[3]) * 2  # 0-3天配对，每对2腿
    slow_legs = (wd["4_7"] + wd["8_plus"]) * 2  # 4天以上才配对
    never_legs = wd["never"]

    print("\n" + "=" * 80)
    print("四、可能性A vs B 归类（按腿数）")
    print("=" * 80)
    print(f"  可能性A（策略特征，0-3天内配对）: {fast_legs} 腿 ({fast_legs/total_legs*100:.1f}%)")
    print(f"  可能性B（敞口失控，4天+或永远未配）: {slow_legs + never_legs} 腿 ({(slow_legs+never_legs)/total_legs*100:.1f}%)")
    print(f"    其中4天以上才配对: {slow_legs} 腿")
    print(f"    其中永远未配对:    {never_legs} 腿")

    print("\n" + "=" * 80)
    print("五、按股票拆分")
    print("=" * 80)
    print(f"{'代码':<10} {'总腿数':>6} {'日内配对':>8} {'日内未配':>8} {'跨日配对':>8} {'同日':>6} {'跨日':>6} {'从未配':>6}")
    print("-" * 70)
    for r in per_stock_results:
        print(f"{r['code']:<10} {r['total']:>6} {r['intra_paired']:>8} {r['intra_unpaired']:>8} "
              f"{r['cross_paired']:>8} {r['cross_same_day']:>6} {r['cross_crossday']:>6} {r['cross_never']:>6}")


if __name__ == "__main__":
    main()
