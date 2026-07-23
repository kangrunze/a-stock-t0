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
from backtest_t_strategy import BacktestParams, backtest_single_day, backtest_multi_day


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
# 真实数据模式（real）
# ═══════════════════════════════════════════════════════════════
REAL_POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
MIN_TRADES_THRESHOLD = 10  # 触发次数下限


def load_real_data(
    codes: list[str],
    start: str,
    end: str,
    source: str = "baostock",
) -> dict:
    """
    拉取真实多股票多日数据，缓存到内存（避免网格搜索时重复拉取）。

    :return: {code: {daily_bars, daily_prev_closes, daily_meta, all_dates}}
    """
    from data_provider import fetch_multi_day

    cache = {}
    for i, code in enumerate(codes):
        print(f"  [load_real_data {i+1}/{len(codes)}] {code}...", end=" ", flush=True)
        daily_bars, daily_prev_closes, daily_meta = fetch_multi_day(
            code, start, end, source
        )
        if daily_bars:
            all_dates = sorted(daily_bars.keys())
            cache[code] = {
                "daily_bars": daily_bars,
                "daily_prev_closes": daily_prev_closes,
                "daily_meta": daily_meta,
                "all_dates": all_dates,
            }
            print(f"{len(all_dates)}天")
        else:
            print("无数据，跳过")
    return cache


def evaluate_param_set_real(
    signal_p: SignalParams,
    risk_p: RiskParams,
    real_data: dict,
    target_dates: set[str],
) -> dict:
    """
    用真实数据在指定日期集合上评估一组参数。
    P3-1: 使用含浮盈浮亏的净盈亏(net_pnl_with_unrealized)作为排名指标。

    :param target_dates: 只回测这些日期（用于训练/验证集切分）
    """
    from run_backtest import adapt_params_by_frequency, normalize_code

    total_gross = 0.0
    total_cost = 0.0
    total_trades = 0
    total_paired = 0
    total_wins = 0
    total_unrealized = 0.0  # P3-1: 未配对敞口浮盈浮亏
    total_final_legs = 0

    for code, data in real_data.items():
        # 按目标日期过滤
        daily_bars = {
            d: b for d, b in data["daily_bars"].items() if d in target_dates
        }
        daily_prev = {
            d: p for d, p in data["daily_prev_closes"].items() if d in target_dates
        }
        if not daily_bars:
            continue

        first_meta = next(iter(data["daily_meta"].values()))
        freq = first_meta.get("frequency", "5min")
        bpd = first_meta.get("bars_count", 48)

        avg_cost = min(daily_prev.values())
        bp = BacktestParams(
            base_shares=3000,
            avg_cost=avg_cost,
            signal_params=signal_p,
            risk_params=risk_p,
        )
        bp = adapt_params_by_frequency(bp, freq, bpd)

        result = backtest_multi_day(
            code=normalize_code(code)["pure"],
            daily_bars=daily_bars,
            daily_prev_closes=daily_prev,
            params=bp,
        )

        for dr in result.get("daily_results", []):
            for t in dr.get("trades", []):
                total_trades += 1
                total_cost += t.get("cost", 0)
                if t.get("paired"):
                    total_paired += 1
                    if t.get("pnl", 0) > 0:
                        total_wins += 1
                total_gross += t.get("pnl", 0)

        # P3-1: 累计未配对敞口浮盈浮亏
        total_unrealized += result.get("unrealized_pnl", 0.0)
        total_final_legs += result.get("final_open_legs_count", 0)

    net_pnl = total_gross - total_cost
    return {
        "total_net_pnl": round(net_pnl + total_unrealized, 2),  # P3-1: 改用含浮盈口径排名
        "realized_net_pnl": round(net_pnl, 2),  # 已实现（不含浮盈）
        "unrealized_pnl": round(total_unrealized, 2),
        "gross_pnl": round(total_gross, 2),
        "total_cost": round(total_cost, 2),
        "total_trades": total_trades,
        "paired_trades": total_paired,
        "win_trades": total_wins,
        "win_rate": round(total_wins / total_paired, 4) if total_paired else 0.0,
        "final_open_legs_count": total_final_legs,
        "insufficient_sample": total_trades < MIN_TRADES_THRESHOLD,
    }


def rolling_out_of_sample(
    real_data: dict,
    verbose: bool = True,
) -> dict:
    """
    滚动样本外验证：前 2/3 交易日选参数，后 1/3 验证。

    1. 训练集上网格搜索所有参数组合
    2. 过滤触发次数 < MIN_TRADES_THRESHOLD 的组合（标注"样本不足"）
    3. 取训练集 Top 5 参数在验证集上验证
    4. 只有验证集仍稳健的参数才算数
    """
    # 收集所有交易日
    all_dates_set = set()
    for data in real_data.values():
        all_dates_set.update(data["all_dates"])
    all_dates = sorted(all_dates_set)

    split = int(len(all_dates) * 2 / 3)
    train_dates = set(all_dates[:split])
    val_dates = set(all_dates[split:])

    if verbose:
        print(f"\n交易日总数: {len(all_dates)}")
        print(f"训练集: {len(train_dates)}天 ({all_dates[0]} ~ {all_dates[split-1]})")
        print(f"验证集: {len(val_dates)}天 ({all_dates[split]} ~ {all_dates[-1]})")
        print(f"参数组合数: {len(list(itertools.product(*PARAM_GRID.values())))}")
        print("=" * 100)

    # 1. 训练集网格搜索
    keys = list(PARAM_GRID.keys())
    value_lists = [PARAM_GRID[k] for k in keys]
    combinations = list(itertools.product(*value_lists))

    train_results = []
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
        eval_result = evaluate_param_set_real(
            signal_p, risk_p, real_data, train_dates
        )
        eval_result["params"] = params_dict
        train_results.append(eval_result)

        if verbose and (idx + 1) % 20 == 0:
            valid_so_far = sum(1 for r in train_results if not r["insufficient_sample"])
            print(f"  训练集进度: {idx+1}/{len(combinations)} "
                  f"(有效{valid_so_far} 样本不足{idx+1-valid_so_far})")

    # 2. 分离样本不足的
    valid = [r for r in train_results if not r["insufficient_sample"]]
    insufficient = [r for r in train_results if r["insufficient_sample"]]
    valid.sort(key=lambda x: x["total_net_pnl"], reverse=True)

    if verbose:
        print(f"\n训练集结果: 有效{len(valid)} 样本不足{len(insufficient)}")
        if insufficient:
            print(f"  [警告] {len(insufficient)} 组参数触发次数 < {MIN_TRADES_THRESHOLD}，已标注为样本不足")

    # 3. Top 5 在验证集上验证
    top_n = min(5, len(valid))
    val_results = []
    for i, r in enumerate(valid[:top_n]):
        params_dict = r["params"]
        signal_p = SignalParams(
            vwap_dev_atr_multiplier=params_dict["vwap_dev_atr_multiplier"],
            rsi_overbought=params_dict["rsi_overbought"],
            rsi_oversold=params_dict["rsi_oversold"],
        )
        risk_p = RiskParams(
            min_capture_spread=params_dict["min_capture_spread"],
            max_t_size_ratio=params_dict["max_t_size_ratio"],
        )
        val_eval = evaluate_param_set_real(
            signal_p, risk_p, real_data, val_dates
        )
        val_eval["params"] = params_dict
        val_eval["train_net_pnl"] = r["total_net_pnl"]
        val_eval["train_win_rate"] = r["win_rate"]
        val_eval["train_trades"] = r["total_trades"]
        val_results.append(val_eval)

        if verbose:
            print(f"  验证 Top{i+1}: 训练净{r['total_net_pnl']:+.2f} "
                  f"→ 验证净{val_eval['total_net_pnl']:+.2f} "
                  f"胜率{val_eval['win_rate']*100:.1f}% "
                  f"交易{val_eval['total_trades']}笔")

    return {
        "all_dates": all_dates,
        "train_dates": sorted(train_dates),
        "val_dates": sorted(val_dates),
        "train_results_valid": valid,
        "train_results_insufficient": insufficient,
        "val_results": val_results,
    }


def print_real_results(roos: dict):
    """打印滚动样本外验证结果。"""
    print(f"\n{'='*120}")
    print("滚动样本外验证结果")
    print(f"{'='*120}")
    print(f"训练集: {len(roos['train_dates'])}天  验证集: {len(roos['val_dates'])}天")
    print(f"有效参数组合: {len(roos['train_results_valid'])}  "
          f"样本不足: {len(roos['train_results_insufficient'])}")

    print(f"\n{'='*120}")
    print("训练集 Top 5 参数 → 验证集表现")
    print(f"{'='*120}")
    print(f"{'排名':<4} {'训练净盈亏':>12} {'验证净盈亏':>12} {'训练胜率':>8} {'验证胜率':>8} "
          f"{'训练笔数':>8} {'验证笔数':>8} {'VWAP×ATR':>8} {'RSI高':>6} {'RSI低':>6} {'价差':>6} {'仓位':>6}")
    print("-" * 120)
    for i, r in enumerate(roos["val_results"]):
        p = r["params"]
        train_wr = f"{r['train_win_rate']*100:.1f}%"
        val_wr = f"{r['win_rate']*100:.1f}%" if r["paired_trades"] else "N/A"
        print(f"{i+1:<4} {r['train_net_pnl']:>12.2f} {r['total_net_pnl']:>12.2f} "
              f"{train_wr:>8} {val_wr:>8} "
              f"{r['train_trades']:>8} {r['total_trades']:>8} "
              f"{p['vwap_dev_atr_multiplier']:>8} {p['rsi_overbought']:>6.0f} "
              f"{p['rsi_oversold']:>6.0f} {p['min_capture_spread']*100:>5.1f}% "
              f"{p['max_t_size_ratio']*100:>5.0f}%")

    if roos["train_results_insufficient"]:
        print(f"\n[样本不足] 以下 {len(roos['train_results_insufficient'])} 组参数触发次数 < {MIN_TRADES_THRESHOLD}，不参与排名:")
        for r in roos["train_results_insufficient"][:5]:
            p = r["params"]
            print(f"  交易{r['total_trades']}笔  VWAP×ATR={p['vwap_dev_atr_multiplier']} "
                  f"RSI={p['rsi_overbought']}/{p['rsi_oversold']}")


def save_real_report(roos: dict, output_path: Path):
    """保存滚动样本外验证报告。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": "real_rolling_out_of_sample",
        "min_trades_threshold": MIN_TRADES_THRESHOLD,
        "train_dates": roos["train_dates"],
        "val_dates": roos["val_dates"],
        "train_valid_count": len(roos["train_results_valid"]),
        "train_insufficient_count": len(roos["train_results_insufficient"]),
        "val_top5": roos["val_results"],
        "train_top10": roos["train_results_valid"][:10],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# 网格搜索（合成数据模式）
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
    parser.add_argument("--data-source", default="synthetic",
                        choices=["synthetic", "real"],
                        help="数据源: synthetic=合成数据(默认), real=真实多股票数据")
    parser.add_argument("--quick", action="store_true",
                        help="快速模式（缩小参数空间）")
    parser.add_argument("--start", default="2026-06-22",
                        help="real 模式起始日期")
    parser.add_argument("--end", default="2026-07-22",
                        help="real 模式结束日期")
    parser.add_argument("--source", default="baostock",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"],
                        help="real 模式数据源")
    parser.add_argument("--max-codes", type=int, default=5,
                        help="real 模式最多用多少只股票（默认5，避免太慢）")
    args = parser.parse_args()

    if args.quick:
        # 快速模式：只测核心参数
        PARAM_GRID["vwap_dev_atr_multiplier"] = [0.8]
        PARAM_GRID["rsi_overbought"] = [70.0]
        PARAM_GRID["rsi_oversold"] = [30.0]
        PARAM_GRID["min_capture_spread"] = [0.006]
        PARAM_GRID["max_t_size_ratio"] = [0.3, 0.5]

    if args.data_source == "real":
        # ── 真实数据模式：滚动样本外验证 ──
        if not REAL_POOL_PATH.exists():
            print(f"[tune] 候选池不存在: {REAL_POOL_PATH}")
            print("[tune] 先跑: python scripts/gen_candidate_pool.py")
            sys.exit(1)

        with open(REAL_POOL_PATH, "r", encoding="utf-8") as f:
            pool = json.load(f)
        codes = [c["code"] for c in pool["candidates"][:args.max_codes]]
        print(f"[tune] real 模式: {len(codes)} 只股票, {args.start}~{args.end}, source={args.source}")
        print(f"[tune] 加载数据...")
        real_data = load_real_data(codes, args.start, args.end, args.source)
        if not real_data:
            print("[tune] 未加载到任何数据，退出")
            sys.exit(1)
        print(f"[tune] 成功加载 {len(real_data)} 只股票数据")

        roos = rolling_out_of_sample(real_data, verbose=True)
        print_real_results(roos)

        report_path = OUTPUT_DIR / "param_tuning_real_report.json"
        save_real_report(roos, report_path)
        print(f"\n[OK] 真实数据调优报告已保存至: {report_path}")
    else:
        # ── 合成数据模式（原有逻辑） ──
        results = grid_search(verbose=True)
        print_top_results(results, top_n=10)

        report_path = OUTPUT_DIR / "param_tuning_report.json"
        save_report(results, report_path)
        print(f"\n[OK] 完整报告已保存至: {report_path}")
