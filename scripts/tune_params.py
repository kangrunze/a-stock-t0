#!/usr/bin/env python3
"""
L5 参数调优脚本
================
在多种日内形态的合成数据上跑回测，对比不同参数组合的表现，
找出对各种形态都稳健的参数集。

调优维度:
  1. vwap_dev_atr_multiplier: 0.6 / 0.8 / 1.0
  2. rsi_overbought / oversold: 65/35 / 70/30 / 75/25
  3. min_capture_spread: 0.004 / 0.006 / 0.008
  4. max_t_size_ratio: 0.3 / 0.5

评估指标:
  - 净盈亏（扣成本后）
  - 胜率
  - 日均T次数
  - 最差形态的净盈亏（稳健性）

输出:
  - 控制台表格
  - outputs/backtest/param_tuning_report.json
"""

from __future__ import annotations

import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_data_generator import PATTERN_GENERATORS, gen_pattern
from t_signal_engine import SignalParams
from t_risk_guard import RiskParams
from backtest_t_strategy import BacktestParams, backtest_single_day


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
# 网格搜索
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
    parser.add_argument("--quick", action="store_true",
                        help="快速模式（缩小参数空间）")
    args = parser.parse_args()

    if args.quick:
        # 快速模式：只测核心参数
        PARAM_GRID["vwap_dev_atr_multiplier"] = [0.8]
        PARAM_GRID["rsi_overbought"] = [70.0]
        PARAM_GRID["rsi_oversold"] = [30.0]
        PARAM_GRID["min_capture_spread"] = [0.006]
        PARAM_GRID["max_t_size_ratio"] = [0.3, 0.5]

    results = grid_search(verbose=True)
    print_top_results(results, top_n=10)

    report_path = OUTPUT_DIR / "param_tuning_report.json"
    save_report(results, report_path)
    print(f"\n[OK] 完整报告已保存至: {report_path}")
