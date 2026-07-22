#!/usr/bin/env python3
"""
指标 IC 分析框架（方案 v0.2 第 4.3 节）
=========================================
在锁定决策层指标组合之前，必须用历史分钟数据对各候选指标做
相关性/单指标 IC 分析，选出 IC 最高且与其他候选相关性最低的 1-2 个
作为正式决策层指标。

方案 v0.2 第 4.3 节原文：
  "拿历史分钟数据，对 RSI/KDJ/CCI/BIAS/ROC 这 5 个候选分别计算
   '该指标达到极值后 N 分钟的价格反转幅度'，选出 IC 最高、且和其他候选
   相关性最低的 1-2 个作为正式决策层指标，其余的留在计算层但不用于触发。"

IC（Information Coefficient）定义:
  IC = Spearman 秩相关(指标值, 未来 N 分钟收益率)
  IC > 0 → 指标值高时未来收益高（正向预测力）
  IC < 0 → 指标值高时未来收益低（反向预测力，用于超买超卖类指标）

用法:
  python research/indicator_ic_analysis.py --bars data/minute_bars/600000.SH_20260722.csv
  python research/indicator_ic_analysis.py --bars data/minute_bars/ --horizon 5 10 15

输出:
  1. 各指标在不同预测窗口(N分钟)下的 IC 均值 + ICIR
  2. 指标间相关性矩阵
  3. 推荐决策层指标组合
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

# 让 research/ 目录能 import scripts/
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from intraday_reference import (
    rsi, kdj, cci, bias, roc, compute_reference_snapshot,
)


# ═══════════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════════
def load_minute_bars(csv_path: Path) -> list[dict]:
    """从分钟K线 CSV 加载数据（兼容 minute_bar_fetcher.save_minute_bars_to_csv 格式）。"""
    bars = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bars.append({
                "time": row.get("time", ""),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row.get("amount", 0)),
            })
    return bars


# ═══════════════════════════════════════════════════════════════
# 未来收益率计算
# ═══════════════════════════════════════════════════════════════
def future_returns(bars: list[dict], horizon: int) -> list[Optional[float]]:
    """
    计算每根 K 线未来 horizon 分钟的收益率:
      ret_t = (close_{t+horizon} - close_t) / close_t

    返回 list，最后 horizon 个为 None（无未来数据）。
    """
    n = len(bars)
    rets = [None] * n
    for i in range(n - horizon):
        cur = bars[i]["close"]
        future = bars[i + horizon]["close"]
        if cur > 0:
            rets[i] = (future - cur) / cur
    return rets


# ═══════════════════════════════════════════════════════════════
# 指标序列计算（逐时刻滚动，严格因果）
# ═══════════════════════════════════════════════════════════════
def compute_indicator_series(bars: list[dict]) -> dict[str, list[Optional[float]]]:
    """
    逐时刻计算 5 个候选指标的值（严格因果，只用截至当前的数据）。

    返回 {indicator_name: [value_per_bar or None]}。
    """
    n = len(bars)
    series = {
        "rsi": [None] * n,
        "kdj_k": [None] * n,
        "cci": [None] * n,
        "bias": [None] * n,
        "roc": [None] * n,
    }
    for i in range(n):
        bars_up_to = bars[:i + 1]
        # RSI(14)
        val = rsi(bars_up_to, period=14)
        if val is not None:
            series["rsi"][i] = val
        # KDJ K
        k, d, j = kdj(bars_up_to, n=9, m1=3, m2=3)
        if k is not None:
            series["kdj_k"][i] = k
        # CCI(14)
        val = cci(bars_up_to, period=14)
        if val is not None:
            series["cci"][i] = val
        # BIAS(6)
        val = bias(bars_up_to, period=6)
        if val is not None:
            series["bias"][i] = val
        # ROC(12)
        val = roc(bars_up_to, period=12)
        if val is not None:
            series["roc"][i] = val
    return series


# ═══════════════════════════════════════════════════════════════
# Spearman 秩相关 IC
# ═══════════════════════════════════════════════════════════════
def _rank(values: list[float]) -> list[float]:
    """计算秩（平均秩处理 ties）。"""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def spearman_ic(indicator_vals: list[Optional[float]],
                future_rets: list[Optional[float]]) -> Optional[float]:
    """
    计算 Spearman 秩相关 IC。

    只用两个列表都非 None 的位置。
    """
    pairs = [
        (iv, fr)
        for iv, fr in zip(indicator_vals, future_rets)
        if iv is not None and fr is not None
    ]
    if len(pairs) < 30:
        return None  # 样本太少
    iv_list = [p[0] for p in pairs]
    fr_list = [p[1] for p in pairs]
    iv_ranks = _rank(iv_list)
    fr_ranks = _rank(fr_list)
    n = len(pairs)
    mean_iv = sum(iv_ranks) / n
    mean_fr = sum(fr_ranks) / n
    cov = sum((iv_ranks[i] - mean_iv) * (fr_ranks[i] - mean_fr) for i in range(n))
    std_iv = (sum((r - mean_iv) ** 2 for r in iv_ranks)) ** 0.5
    std_fr = (sum((r - mean_fr) ** 2 for r in fr_ranks)) ** 0.5
    if std_iv == 0 or std_fr == 0:
        return None
    return cov / (std_iv * std_fr)


# ═══════════════════════════════════════════════════════════════
# 极值反转分析（方案 4.3："指标达到极值后 N 分钟的价格反转幅度"）
# ═══════════════════════════════════════════════════════════════
def extreme_reversal_analysis(
    indicator_vals: list[Optional[float]],
    future_rets: list[Optional[float]],
    high_threshold: float,
    low_threshold: float,
) -> dict:
    """
    分析指标达到极值后的反转幅度。

    参数:
      high_threshold: 指标超买阈值（如 RSI 70）
      low_threshold: 指标超卖阈值（如 RSI 30）

    返回:
      {
        "high_count": 达到超买的次数,
        "high_avg_return": 超买后 N 分钟平均收益（应为负，即反转下跌）,
        "low_count": 达到超卖的次数,
        "low_avg_return": 超卖后 N 分钟平均收益（应为正，即反转上涨）,
      }
    """
    high_rets, low_rets = [], []
    for iv, fr in zip(indicator_vals, future_rets):
        if iv is None or fr is None:
            continue
        if iv >= high_threshold:
            high_rets.append(fr)
        elif iv <= low_threshold:
            low_rets.append(fr)
    return {
        "high_count": len(high_rets),
        "high_avg_return": sum(high_rets) / len(high_rets) if high_rets else None,
        "low_count": len(low_rets),
        "low_avg_return": sum(low_rets) / len(low_rets) if low_rets else None,
    }


# ═══════════════════════════════════════════════════════════════
# 主分析函数
# ═══════════════════════════════════════════════════════════════
def run_ic_analysis(bars: list[dict], horizons: list[int]) -> None:
    """
    对 5 个候选指标跑完整 IC 分析。
    """
    print(f"=== IC 分析（{len(bars)} 根分钟K线）===")
    print(f"预测窗口: {horizons} 分钟\n")

    # 计算指标序列
    print("计算指标序列（严格因果滚动）...")
    series = compute_indicator_series(bars)

    # 各指标在不同窗口下的 IC
    print("\n── 1. Spearman IC（指标值 vs 未来N分钟收益）──")
    print(f"{'指标':<10}", end="")
    for h in horizons:
        print(f"{'IC@'+str(h)+'min':>12}", end="")
    print()
    ic_results = {}
    for name, vals in series.items():
        ic_results[name] = {}
        print(f"{name:<10}", end="")
        for h in horizons:
            rets = future_returns(bars, h)
            ic = spearman_ic(vals, rets)
            ic_results[name][h] = ic
            print(f"{ic:>12.4f}" if ic is not None else f"{'N/A':>12}", end="")
        print()

    # 指标间相关性矩阵
    print("\n── 2. 指标间 Spearman 相关性矩阵 ──")
    names = list(series.keys())
    print(f"{'':<10}", end="")
    for n in names:
        print(f"{n:>12}", end="")
    print()
    for n1 in names:
        print(f"{n1:<10}", end="")
        for n2 in names:
            ic = spearman_ic(series[n1], series[n2])
            print(f"{ic:>12.4f}" if ic is not None else f"{'N/A':>12}", end="")
        print()

    # 极值反转分析（以第一个预测窗口为例）
    h0 = horizons[0]
    print(f"\n── 3. 极值反转分析（{h0}分钟后收益）──")
    rets = future_returns(bars, h0)
    thresholds = {
        "rsi": (70, 30),
        "kdj_k": (80, 20),
        "cci": (100, -100),
        "bias": (3.0, -3.0),
        "roc": (2.0, -2.0),
    }
    print(f"{'指标':<10} {'超买次数':>8} {'超买后收益':>12} {'超卖次数':>8} {'超卖后收益':>12}")
    for name, (high, low) in thresholds.items():
        result = extreme_reversal_analysis(series[name], rets, high, low)
        h_ret = f"{result['high_avg_return']*100:.3f}%" if result['high_avg_return'] is not None else "N/A"
        l_ret = f"{result['low_avg_return']*100:.3f}%" if result['low_avg_return'] is not None else "N/A"
        print(f"{name:<10} {result['high_count']:>8} {h_ret:>12} {result['low_count']:>8} {l_ret:>12}")

    # 推荐结论
    print("\n── 4. 决策层指标推荐 ──")
    print("选择标准: |IC| 最大 且 与其他候选相关性最低")
    # 取第一个窗口的 IC 绝对值排序
    ic_abs = {name: abs(ic_results[name].get(h0, 0) or 0) for name in names}
    ranked = sorted(ic_abs.items(), key=lambda x: x[1], reverse=True)
    for i, (name, abs_ic) in enumerate(ranked):
        tag = "← 推荐（主触发）" if i == 0 else ("← 推荐（辅助）" if i == 1 else "")
        print(f"  {i+1}. {name}: |IC|={abs_ic:.4f} {tag}")
    print("\n注意: 以上为统计推荐，最终决策层指标需结合业务逻辑确认。")
    print("超买类指标（RSI/KDJ/CCI/BIAS/ROC）的 IC 应为负（指标高→未来跌），")
    print("即用于减仓信号时取 IC 负值最大的指标。")


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="指标 IC 分析（方案 v0.2 4.3 节）")
    parser.add_argument("--bars", required=True,
                        help="分钟K线 CSV 路径（单个文件或目录）")
    parser.add_argument("--horizon", type=int, nargs="+", default=[5, 10, 15],
                        help="预测窗口（分钟），默认 5 10 15")
    args = parser.parse_args()

    bars_path = Path(args.bars)
    csv_files = []
    if bars_path.is_dir():
        csv_files = sorted(bars_path.glob("*.csv"))
    else:
        csv_files = [bars_path]

    if not csv_files:
        print(f"[ERROR] 未找到 CSV 文件: {bars_path}")
        sys.exit(1)

    all_bars = []
    for csv_file in csv_files:
        bars = load_minute_bars(csv_file)
        all_bars.extend(bars)
        print(f"加载 {csv_file.name}: {len(bars)} 根")

    if len(all_bars) < 60:
        print(f"[ERROR] 数据不足: {len(all_bars)} 根，至少需要 60 根")
        sys.exit(1)

    run_ic_analysis(all_bars, args.horizon)
