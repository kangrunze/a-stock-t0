#!/usr/bin/env python3
"""
真实数据模拟交易
================
使用 sh510300_5min_2026-07-15_2026-07-22.csv（沪深300ETF 5分钟K线）
进行模拟买卖交易，记录完整过程并评估当前策略可靠性。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_t_strategy import BacktestParams, backtest_single_day
from config_loader import load_signal_params, load_risk_params

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "sh510300_5min_2026-07-15_2026-07-22.csv"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "simulate_trades"
CODE = "510300.SH"
BASE_SHARES = 10000
AVG_COST = 4.830
WARMUP_BARS = 6


def parse_time(raw: str) -> str:
    """20260715093500000 → 09:35:00"""
    return f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}"


def load_5min_csv(path: Path) -> dict[str, list[dict]]:
    """加载 5 分钟 CSV，按日期分组返回 bars dict。"""
    daily_bars: dict[str, list[dict]] = {}
    for row in csv.DictReader(open(path, "r", encoding="utf-8")):
        bar = {
            "time": parse_time(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(float(row["volume"])),
            "amount": float(row["amount"]),
        }
        if bar["close"] > 0:
            daily_bars.setdefault(row["date"], []).append(bar)
    return daily_bars


def find_pairs(trades: list[dict]) -> list[tuple[dict, dict, float]]:
    """
    从交易列表中找出配对（卖出→买回 或 买入→卖出），返回 [(leg1, leg2, pnl), ...]。
    使用 FIFO 配对逻辑，与 backtest_t_strategy 一致。
    """
    pairs = []
    open_legs = []
    for t in trades:
        paired = False
        for leg in open_legs:
            if leg["direction"] != t["direction"]:
                pairs.append((leg, t, t["pnl"]))
                open_legs.remove(leg)
                paired = True
                break
        if not paired:
            open_legs.append(t)
    return pairs


def save_trades(trades_log: list[dict], results: list[dict]) -> None:
    """保存交易记录到 CSV 和 JSON。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # CSV
    csv_path = OUTPUT_DIR / "trades.csv"
    fieldnames = [
        "date", "time", "direction", "shares", "signal_price", "fill_price",
        "cost", "pnl", "paired", "rules_score", "rules_fired",
        "vwap", "expected_spread",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(trades_log)
    print(f"\n[保存] CSV: {csv_path}")

    # JSON
    json_path = OUTPUT_DIR / "trades.json"
    output = {
        "code": CODE,
        "data_file": DATA_FILE.name,
        "base_shares": BASE_SHARES,
        "avg_cost": AVG_COST,
        "trades": trades_log,
        "daily_summary": [
            {k: r[k] for k in ("date", "t_trades", "cost_reduction",
                                "total_cost_paid", "net_pnl", "win_rate", "eod_status")}
            for r in results
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[保存] JSON: {json_path}")


def build_index(daily_bars: dict[str, list[dict]]) -> dict[tuple[str, str], int]:
    """构建 (date, time) → 全局连续K线索引 的映射，午休时段自然跳过（数据中不存在）。"""
    idx_map = {}
    i = 0
    for date in sorted(daily_bars.keys()):
        for b in daily_bars[date]:
            idx_map[(date, b["time"])] = i
            i += 1
    return idx_map


def _time_ticks(bars: list[dict], offset: int = 0, step: int = 12) -> tuple[list[int], list[str]]:
    """生成 x 轴刻度位置和标签（每隔 step 根标一次时间）。"""
    positions = []
    labels = []
    for i, b in enumerate(bars):
        if i % step == 0:
            positions.append(i + offset)
            labels.append(b["time"][:5])  # "09:35:00" → "09:35"
    return positions, labels


def _plot_day(ax, date: str, bars: list[dict], day_trades: list[dict]) -> None:
    """在单个子图上绘制一天的价格走势和交易点（仅交易时段，无午休间隙）。"""
    xs = list(range(len(bars)))
    prices = [b["close"] for b in bars]

    ax.plot(xs, prices, color="#4A90D9", linewidth=1, label="5min收盘价")

    # 买卖点
    time_to_x = {b["time"]: i for i, b in enumerate(bars)}
    for t in day_trades:
        x = time_to_x.get(t["time"])
        if x is None:
            continue
        if t["direction"] == "buy":
            ax.scatter(x, t["fill_price"], marker="^", color="#27AE60", s=150, zorder=5)
        else:
            ax.scatter(x, t["fill_price"], marker="v", color="#E74C3C", s=150, zorder=5)

    # 配对连线
    for leg1, leg2, pnl in find_pairs(day_trades):
        x1 = time_to_x.get(leg1["time"])
        x2 = time_to_x.get(leg2["time"])
        if x1 is None or x2 is None:
            continue
        color = "#E67E22" if pnl >= 0 else "#95A5A6"
        ax.plot([x1, x2], [leg1["fill_price"], leg2["fill_price"]],
                color=color, linestyle="--", linewidth=1.5, alpha=0.8)
        ax.annotate(f"{pnl:+.0f}", xy=((x1 + x2) / 2,
                                       (leg1["fill_price"] + leg2["fill_price"]) / 2),
                    fontsize=8, color=color, ha="center", fontweight="bold")

    # x 轴时间刻度
    positions, labels = _time_ticks(bars, step=12)
    ax.set_xticks(positions)
    ax.set_xticklabels(labels, fontsize=7)

    # 汇总标题
    net_pnl = sum(t["pnl"] - t["cost"] for t in day_trades)
    ax.set_title(f"{date}  |  {len(day_trades)}笔  |  净{net_pnl:+.2f}元",
                 fontsize=10, loc="left")
    ax.set_ylabel("价格", fontsize=9)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)


def plot_trades(daily_bars: dict[str, list[dict]], trades_log: list[dict]) -> None:
    """生成交易图表（仅交易时段，无午休间隙）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dates = sorted(daily_bars.keys())
    trades_by_date: dict[str, list[dict]] = {}
    for t in trades_log:
        trades_by_date.setdefault(t["date"], []).append(t)
    idx_map = build_index(daily_bars)

    # ── 总览图 ──
    fig, ax = plt.subplots(figsize=(16, 6))
    day_boundaries = []
    offset = 0
    for d in dates:
        bars = daily_bars[d]
        xs = [idx_map[(d, b["time"])] for b in bars]
        prices = [b["close"] for b in bars]
        ax.plot(xs, prices, color="#4A90D9", linewidth=0.8, alpha=0.7)
        day_boundaries.append(offset)
        offset += len(bars)

    for bd in day_boundaries[1:]:
        ax.axvline(x=bd, color="#CCCCCC", linestyle="--", linewidth=0.5)

    # 买卖点
    buy_x, buy_p, sell_x, sell_p = [], [], [], []
    for t in trades_log:
        x = idx_map.get((t["date"], t["time"]))
        if x is None:
            continue
        if t["direction"] == "buy":
            buy_x.append(x)
            buy_p.append(t["fill_price"])
        else:
            sell_x.append(x)
            sell_p.append(t["fill_price"])
    ax.scatter(sell_x, sell_p, marker="v", color="#E74C3C", s=100, zorder=5,
               label=f"卖出 ({len(sell_x)})")
    ax.scatter(buy_x, buy_p, marker="^", color="#27AE60", s=100, zorder=5,
               label=f"买入 ({len(buy_x)})")

    # 配对连线
    for date, day_trades in trades_by_date.items():
        for leg1, leg2, pnl in find_pairs(day_trades):
            x1 = idx_map.get((date, leg1["time"]))
            x2 = idx_map.get((date, leg2["time"]))
            if x1 is None or x2 is None:
                continue
            color = "#E67E22" if pnl >= 0 else "#95A5A6"
            ax.plot([x1, x2], [leg1["fill_price"], leg2["fill_price"]],
                    color=color, linestyle="--", linewidth=1.2, alpha=0.8)
            ax.annotate(f"{pnl:+.0f}",
                        xy=((x1 + x2) / 2,
                            (leg1["fill_price"] + leg2["fill_price"]) / 2),
                        fontsize=7, color=color, ha="center")

    # 总览图 x 轴：标日期分界 + 每天几个时间点
    tick_positions = []
    tick_labels = []
    offset = 0
    for d in dates:
        bars = daily_bars[d]
        positions, labels = _time_ticks(bars, offset=offset, step=24)
        tick_positions.extend(positions)
        tick_labels.extend([f"{d[5:]}\n{lbl}" for lbl in labels])
        offset += len(bars)
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, fontsize=7)

    ax.set_title(f"510300.SH 模拟交易总览（{dates[0]} ~ {dates[-1]}）", fontsize=14)
    ax.set_xlabel("时间", fontsize=11)
    ax.set_ylabel("价格（元）", fontsize=11)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    overview_path = OUTPUT_DIR / "trades_overview.png"
    fig.savefig(overview_path, dpi=150)
    plt.close(fig)
    print(f"[图表] 总览图: {overview_path}")

    # ── 按日详细图 ──
    n_days = len(dates)
    fig, axes = plt.subplots(n_days, 1, figsize=(14, 3 * n_days))
    if n_days == 1:
        axes = [axes]
    for idx, date in enumerate(dates):
        _plot_day(axes[idx], date, daily_bars[date], trades_by_date.get(date, []))
    fig.suptitle("510300.SH 逐日交易明细", fontsize=14, y=1.01)
    fig.tight_layout()
    daily_path = OUTPUT_DIR / "trades_daily.png"
    fig.savefig(daily_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[图表] 按日详细图: {daily_path}")


def main():
    print("=" * 72)
    print("  真实数据模拟交易 — sh510300（沪深300ETF）5分钟K线")
    print("=" * 72)

    # 1. 加载数据
    daily_bars = load_5min_csv(DATA_FILE)
    dates = sorted(daily_bars.keys())
    print(f"\n[数据] {len(dates)} 个交易日, {sum(len(v) for v in daily_bars.values())} 根K线")

    # 2. 加载参数
    bt_params = BacktestParams(
        base_shares=BASE_SHARES,
        avg_cost=AVG_COST,
        warmup_bars=WARMUP_BARS,
        signal_params=load_signal_params(),
        risk_params=load_risk_params(),
    )
    print(f"[参数] cooldown={bt_params.cooldown_bars}, "
          f"require_opposite={bt_params.require_opposite_direction}")

    # 3. 逐日回测
    print("\n" + "=" * 72)
    print("  逐日模拟交易")
    print("=" * 72)

    all_results = []
    all_trades = []
    prev_close = AVG_COST

    for date in dates:
        bars = daily_bars[date]
        result = backtest_single_day(CODE, date, bars, prev_close, bt_params)
        all_results.append(result)

        print(f"\n{'─' * 60}")
        print(f"  {date}  | prev_close={prev_close:.4f}")
        if result["trades"]:
            print(f"  {'时间':<10} {'方向':<6} {'股数':>6} {'成交价':>10} {'PnL':>8} {'配对':>4}")
            for t in result["trades"]:
                print(f"  {t['time']:<10} {t['direction']:<6} {t['shares']:>6} "
                      f"{t['fill_price']:>10.4f} {t['pnl']:>8.2f} "
                      f"{'是' if t['paired'] else '否':>4}")
                all_trades.append({"date": date, **t})
        else:
            print("  无交易")
        print(f"  → T{result['t_trades']}笔, 净{result['net_pnl']:+.2f}元, {result['eod_status']}")
        prev_close = bars[-1]["close"]

    # 4. 汇总
    print("\n" + "=" * 72)
    print("  汇总统计")
    print("=" * 72)

    total = len(all_trades)
    paired = sum(1 for t in all_trades if t.get("paired"))
    winning = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
    total_pnl = sum(r["net_pnl"] for r in all_results)
    total_cost = sum(r["total_cost_paid"] for r in all_results)
    total_reduction = sum(r["cost_reduction"] for r in all_results)

    print(f"\n  交易日:   {len(all_results)}")
    print(f"  总交易:   {total}笔（配对{paired}, 未配对{total-paired}）")
    print(f"  盈利笔数: {winning}")
    print(f"  配对收益: {total_reduction:.2f}元")
    print(f"  交易成本: {total_cost:.2f}元")
    print(f"  净盈亏:   {total_pnl:+.2f}元")
    if total > 0:
        print(f"  胜率:     {winning/total*100:.1f}%")
    print(f"  收益率:   {total_pnl/(BASE_SHARES*AVG_COST)*100:.3f}%")

    print(f"\n  逐日:")
    for r in all_results:
        print(f"    {r['date']}: T{r['t_trades']}笔, 净{r['net_pnl']:+.2f}元, {r['eod_status']}")

    # 5. 保存和绘图
    save_trades(all_trades, all_results)
    plot_trades(daily_bars, all_trades)


if __name__ == "__main__":
    main()
