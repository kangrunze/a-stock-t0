#!/usr/bin/env python3
"""
真实数据模拟交易
================
使用 sh510300_5min_2026-07-15_2026-07-22.csv（沪深300ETF 5分钟K线）
进行模拟买卖交易，记录完整过程并评估当前策略可靠性。

注意：
  1. 数据为 5 分钟 K 线（非 1 分钟），warmup_bars 已按比例调整（30→6，保持30分钟预热）
  2. 510300 是 ETF，实际支持 T+0，但本引擎按 T+1 实现（locked_shares），模拟中保留此约束
  3. 当前 thresholds.yaml 参数仅基于合成数据调优，本测试即为 P1-4 所述"真实数据验证"的初步尝试
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyArrowPatch

# 中文字体配置（Windows）
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_t_strategy import BacktestParams, backtest_single_day
from config_loader import load_signal_params, load_risk_params
from t_signal_engine import SignalParams
from t_risk_guard import RiskParams


# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "sh510300_5min_2026-07-15_2026-07-22.csv"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "simulate_trades"
CODE = "510300.SH"
BASE_SHARES = 10000        # 底仓 1 万股（约 4.8 万元）
AVG_COST = 4.830           # 底仓成本（取首日开盘价附近）
WARMUP_BARS = 6            # 5分钟线 × 6 = 30分钟预热（等价于 1分钟线 × 30）


def parse_time(raw: str) -> str:
    """20260715093500000 → 09:35:00"""
    # 格式: YYYYMMDDHHMMSSmmm
    hh = raw[8:10]
    mm = raw[10:12]
    ss = raw[12:14]
    return f"{hh}:{mm}:{ss}"


def load_5min_csv(path: Path) -> dict[str, list[dict]]:
    """加载 5 分钟 CSV，按日期分组返回 bars dict。"""
    rows = list(csv.DictReader(open(path, "r", encoding="utf-8")))
    daily_bars: dict[str, list[dict]] = {}
    for row in rows:
        date = row["date"]
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
            daily_bars.setdefault(date, []).append(bar)
    return daily_bars


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def main():
    print("=" * 72)
    print("  真实数据模拟交易 — sh510300（沪深300ETF）5分钟K线")
    print("  数据: 2026-07-15 ~ 2026-07-22（6个交易日）")
    print("=" * 72)

    # 1. 加载数据
    daily_bars = load_5min_csv(DATA_FILE)
    dates = sorted(daily_bars.keys())
    print(f"\n[数据] {len(dates)} 个交易日, 共 {sum(len(v) for v in daily_bars.values())} 根K线")
    for d in dates:
        bars = daily_bars[d]
        print(f"  {d}: {len(bars)} 根, 开盘={bars[0]['open']:.4f}, "
              f"收盘={bars[-1]['close']:.4f}, "
              f"最高={max(b['high'] for b in bars):.4f}, "
              f"最低={min(b['low'] for b in bars):.4f}")

    # 2. 加载配置参数（从 thresholds.yaml）
    signal_params = load_signal_params()
    risk_params = load_risk_params()
    print(f"\n[参数] min_rules_to_trigger={signal_params.min_rules_to_trigger}, "
          f"vwap_dev_atr×={signal_params.vwap_dev_atr_multiplier}, "
          f"rsi={signal_params.rsi_overbought}/{signal_params.rsi_oversold}, "
          f"kdj={signal_params.kdj_overbought}/{signal_params.kdj_oversold}")
    print(f"[参数] min_capture_spread={risk_params.min_capture_spread}, "
          f"max_t_size={risk_params.max_t_size_ratio}, "
          f"max_t_trades={risk_params.max_t_trades_per_day}")

    # 3. 构建回测参数
    bt_params = BacktestParams(
        base_shares=BASE_SHARES,
        avg_cost=AVG_COST,
        warmup_bars=WARMUP_BARS,
        signal_params=signal_params,
        risk_params=risk_params,
    )
    print(f"[参数] base_shares={BASE_SHARES}, avg_cost={AVG_COST}, warmup_bars={WARMUP_BARS}")
    print(f"[参数] cooldown_bars={bt_params.cooldown_bars}, "
          f"require_opposite_direction={bt_params.require_opposite_direction}")

    # 4. 逐日回测
    print("\n" + "=" * 72)
    print("  逐日模拟交易")
    print("=" * 72)

    all_results = []
    all_trades_log = []
    prev_close = AVG_COST  # 首日 prev_close 用底仓成本

    for date in dates:
        bars = daily_bars[date]
        print(f"\n{'─' * 72}")
        print(f"  日期: {date}  | prev_close={prev_close:.4f}  | bars={len(bars)}")
        print(f"{'─' * 72}")

        result = backtest_single_day(
            code=CODE,
            trading_date=date,
            bars=bars,
            prev_close=prev_close,
            params=bt_params,
        )
        all_results.append(result)

        # 打印当日交易明细
        if result["trades"]:
            print(f"\n  当日交易: {result['t_trades']} 笔")
            print(f"  {'时间':<10} {'方向':<6} {'股数':>8} {'信号价':>10} {'成交价':>10} "
                  f"{'成本':>8} {'配对PnL':>10} {'配对':>6} {'score':>6}")
            for t in result["trades"]:
                print(f"  {t['time']:<10} {t['direction']:<6} {t['shares']:>8} "
                      f"{t['signal_price']:>10.4f} {t['fill_price']:>10.4f} "
                      f"{t['cost']:>8.2f} {t['pnl']:>10.2f} {'是' if t['paired'] else '否':>6} "
                      f"{t['rules_score']:>6}")
                all_trades_log.append({"date": date, **t})
        else:
            print(f"\n  当日交易: 0 笔（无信号触发）")

        print(f"\n  当日汇总: T次数={result['t_trades']}, "
              f"配对收益={result['cost_reduction']:.2f}, "
              f"总成本={result['total_cost_paid']:.2f}, "
              f"净盈亏={result['net_pnl']:.2f}, "
              f"胜率={result['win_rate']*100:.0f}%, "
              f"尾盘={result['eod_status']}")

        # 打印 open_legs（未配对的腿）
        # 注意：backtest_single_day 返回值不含 open_legs，但从 trades 可推断
        unpaired = [t for t in result["trades"] if not t.get("paired")]
        if unpaired:
            print(f"  未配对腿: {len(unpaired)} 笔（当日未完成T闭环）")

        # 更新 prev_close
        prev_close = bars[-1]["close"]

    # 5. 汇总统计
    print("\n" + "=" * 72)
    print("  汇总统计")
    print("=" * 72)

    total_trades = sum(r["t_trades"] for r in all_results)
    total_cost_reduction = sum(r["cost_reduction"] for r in all_results)
    total_cost_paid = sum(r["total_cost_paid"] for r in all_results)
    total_net_pnl = sum(r["net_pnl"] for r in all_results)
    paired_trades = sum(1 for t in all_trades_log if t.get("paired"))
    winning_trades = sum(1 for t in all_trades_log if t.get("pnl", 0) > 0)
    losing_trades = sum(1 for t in all_trades_log if t.get("pnl", 0) < 0)
    zero_trades = sum(1 for t in all_trades_log if t.get("pnl", 0) == 0)

    print(f"\n  交易日数:     {len(all_results)}")
    print(f"  总交易笔数:   {total_trades}")
    print(f"  已配对笔数:   {paired_trades}")
    print(f"  未配对笔数:   {total_trades - paired_trades}")
    print(f"  盈利笔数:     {winning_trades}")
    print(f"  亏损笔数:     {losing_trades}")
    print(f"  零盈亏笔数:   {zero_trades}")
    print(f"  配对总收益:   {total_cost_reduction:.2f} 元")
    print(f"  总交易成本:   {total_cost_paid:.2f} 元")
    print(f"  净盈亏:       {total_net_pnl:.2f} 元")
    if total_trades > 0:
        print(f"  胜率:         {winning_trades/total_trades*100:.1f}%")
    print(f"  底仓市值:     {BASE_SHARES * AVG_COST:.2f} 元")
    print(f"  净盈亏/市值:  {total_net_pnl/(BASE_SHARES * AVG_COST)*100:.3f}%")

    # 6. 逐日净盈亏
    print(f"\n  逐日净盈亏:")
    for r in all_results:
        print(f"    {r['date']}: T{r['t_trades']}笔, 净{r['net_pnl']:+.2f}元, "
              f"胜率{r['win_rate']*100:.0f}%, {r['eod_status']}")

    # 7. 策略可靠性评估
    print("\n" + "=" * 72)
    print("  策略可靠性评估")
    print("=" * 72)

    no_trade_days = sum(1 for r in all_results if r["t_trades"] == 0)
    print(f"\n  无交易日数:   {no_trade_days}/{len(all_results)}")
    print(f"  有交易日数:   {len(all_results) - no_trade_days}/{len(all_results)}")

    if total_net_pnl > 0:
        print(f"  整体盈亏:     盈利 ✓")
    elif total_net_pnl < 0:
        print(f"  整体盈亏:     亏损 ✗")
    else:
        print(f"  整体盈亏:     持平")

    # 诊断
    print("\n  诊断:")
    if total_trades == 0:
        print("    ⚠ 全部6个交易日无任何信号触发 — 当前阈值对5分钟ETF数据过于保守")
        print("      可能原因: ETF波动率远低于个股, ATR偏小导致 VWAP偏离度阈值难达")
    if total_trades > 0 and paired_trades < total_trades:
        print(f"    ⚠ {total_trades - paired_trades} 笔交易未完成配对（当日未买回/卖出）")
        print("      这些腿的盈亏未计入 cost_reduction，尾盘标记为 net_reduce/net_add")
    if total_cost_paid > 0 and total_cost_reduction == 0:
        print("    ⚠ 有交易成本但无配对收益 — 所有交易都是单腿（未闭环）")
    if losing_trades > winning_trades and total_net_pnl < 0:
        print(f"    ⚠ 亏损笔数({losing_trades}) > 盈利笔数({winning_trades})，策略方向性可能有问题")

    print("\n  ⚠ 注意: 当前 thresholds.yaml 参数基于合成1分钟数据调优，")
    print("    本测试使用真实5分钟ETF数据，结果仅供参考，不构成参数有效性判断。")
    print("    需积累更多股票+交易日数据后才能得出可靠结论。")

    # 8. 保存交易记录
    save_trades(all_trades_log, all_results, daily_bars)

    # 9. 生成图表
    plot_trades(daily_bars, all_trades_log)


# ═══════════════════════════════════════════════════════════════
# 交易记录保存
# ═══════════════════════════════════════════════════════════════
def save_trades(trades_log: list[dict], results: list[dict], daily_bars: dict) -> None:
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
        for t in trades_log:
            writer.writerow(t)
    print(f"\n[保存] 交易记录 CSV: {csv_path}")

    # JSON（含每日汇总）
    json_path = OUTPUT_DIR / "trades.json"
    output = {
        "code": CODE,
        "data_file": str(DATA_FILE.name),
        "base_shares": BASE_SHARES,
        "avg_cost": AVG_COST,
        "trades": trades_log,
        "daily_summary": [
            {
                "date": r["date"],
                "t_trades": r["t_trades"],
                "cost_reduction": r["cost_reduction"],
                "total_cost_paid": r["total_cost_paid"],
                "net_pnl": r["net_pnl"],
                "win_rate": r["win_rate"],
                "eod_status": r["eod_status"],
            }
            for r in results
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[保存] 交易记录 JSON: {json_path}")


# ═══════════════════════════════════════════════════════════════
# 图表生成
# ═══════════════════════════════════════════════════════════════
def plot_trades(daily_bars: dict[str, list[dict]], trades_log: list[dict]) -> None:
    """
    生成交易图表:
      - 总览图: 6天连续价格折线 + 买卖点标记 + 配对连线
      - 按日详细图: 每天一个子图
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dates = sorted(daily_bars.keys())

    # ── 图1: 总览图 ──
    fig, ax = plt.subplots(figsize=(16, 6))

    all_times = []
    all_prices = []
    day_boundaries = []

    for d in dates:
        bars = daily_bars[d]
        for b in bars:
            dt = datetime.strptime(f"{d} {b['time']}", "%Y-%m-%d %H:%M:%S")
            all_times.append(dt)
            all_prices.append(b["close"])
        day_boundaries.append(datetime.strptime(f"{d} 09:35:00", "%Y-%m-%d %H:%M:%S"))

    ax.plot(all_times, all_prices, color="#4A90D9", linewidth=0.8, alpha=0.7, label="5min收盘价")

    # 日期分界线
    for bd in day_boundaries[1:]:
        ax.axvline(x=bd, color="#CCCCCC", linestyle="--", linewidth=0.5)

    # 买卖点标记
    buy_times, buy_prices = [], []
    sell_times, sell_prices = [], []
    for t in trades_log:
        dt = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M:%S")
        if t["direction"] == "buy":
            buy_times.append(dt)
            buy_prices.append(t["fill_price"])
        else:
            sell_times.append(dt)
            sell_prices.append(t["fill_price"])

    ax.scatter(sell_times, sell_prices, marker="v", color="#E74C3C", s=100, zorder=5, label=f"卖出 ({len(sell_times)})")
    ax.scatter(buy_times, buy_prices, marker="^", color="#27AE60", s=100, zorder=5, label=f"买入 ({len(buy_times)})")

    # 配对连线（卖出→买回）
    # 按 date 分组，找配对的交易对
    trades_by_date: dict[str, list[dict]] = {}
    for t in trades_log:
        trades_by_date.setdefault(t["date"], []).append(t)

    for date, day_trades in trades_by_date.items():
        open_legs = []  # 待配对的腿
        for t in day_trades:
            dt = datetime.strptime(f"{date} {t['time']}", "%Y-%m-%d %H:%M:%S")
            # 尝试配对
            paired = False
            for leg in open_legs:
                if leg["direction"] != t["direction"]:
                    # 配对成功，画连线
                    leg_dt = datetime.strptime(f"{date} {leg['time']}", "%Y-%m-%d %H:%M:%S")
                    color = "#E67E22" if t["pnl"] >= 0 else "#95A5A6"
                    ax.plot([leg_dt, dt], [leg["fill_price"], t["fill_price"]],
                            color=color, linestyle="--", linewidth=1.2, alpha=0.8)
                    # 标注盈亏
                    mid_dt = leg_dt + (dt - leg_dt) / 2
                    mid_price = (leg["fill_price"] + t["fill_price"]) / 2
                    ax.annotate(f"{t['pnl']:+.0f}", xy=(mid_dt, mid_price),
                                fontsize=7, color=color, ha="center")
                    paired = True
                    open_legs.remove(leg)
                    break
            if not paired:
                open_legs.append(t)

    ax.set_title(f"510300.SH 模拟交易总览（{dates[0]} ~ {dates[-1]}）", fontsize=14)
    ax.set_xlabel("时间", fontsize=11)
    ax.set_ylabel("价格（元）", fontsize=11)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())

    fig.tight_layout()
    overview_path = OUTPUT_DIR / "trades_overview.png"
    fig.savefig(overview_path, dpi=150)
    plt.close(fig)
    print(f"[图表] 总览图: {overview_path}")

    # ── 图2: 按日详细图 ──
    n_days = len(dates)
    fig, axes = plt.subplots(n_days, 1, figsize=(14, 3 * n_days), sharex=False)
    if n_days == 1:
        axes = [axes]

    for idx, date in enumerate(dates):
        ax = axes[idx]
        bars = daily_bars[date]
        times = [datetime.strptime(f"{date} {b['time']}", "%Y-%m-%d %H:%M:%S") for b in bars]
        prices = [b["close"] for b in bars]

        ax.plot(times, prices, color="#4A90D9", linewidth=1, label="5min收盘价")
        ax.fill_between(times, prices, min(prices) - 0.02, alpha=0.1, color="#4A90D9")

        # 当日交易
        day_trades = [t for t in trades_log if t["date"] == date]
        day_buy_t, day_buy_p = [], []
        day_sell_t, day_sell_p = [], []
        open_legs = []
        for t in day_trades:
            dt = datetime.strptime(f"{date} {t['time']}", "%Y-%m-%d %H:%M:%S")
            if t["direction"] == "buy":
                day_buy_t.append(dt)
                day_buy_p.append(t["fill_price"])
            else:
                day_sell_t.append(dt)
                day_sell_p.append(t["fill_price"])

            # 配对连线
            paired = False
            for leg in open_legs:
                if leg["direction"] != t["direction"]:
                    leg_dt = datetime.strptime(f"{date} {leg['time']}", "%Y-%m-%d %H:%M:%S")
                    color = "#E67E22" if t["pnl"] >= 0 else "#95A5A6"
                    ax.plot([leg_dt, dt], [leg["fill_price"], t["fill_price"]],
                            color=color, linestyle="--", linewidth=1.5, alpha=0.8)
                    mid_dt = leg_dt + (dt - leg_dt) / 2
                    mid_price = (leg["fill_price"] + t["fill_price"]) / 2
                    ax.annotate(f"{t['pnl']:+.0f}", xy=(mid_dt, mid_price),
                                fontsize=8, color=color, ha="center", fontweight="bold")
                    paired = True
                    open_legs.remove(leg)
                    break
            if not paired:
                open_legs.append(t)

        if day_sell_t:
            ax.scatter(day_sell_t, day_sell_p, marker="v", color="#E74C3C", s=150, zorder=5, label=f"卖出({len(day_sell_t)})")
        if day_buy_t:
            ax.scatter(day_buy_t, day_buy_p, marker="^", color="#27AE60", s=150, zorder=5, label=f"买入({len(day_buy_t)})")

        # 汇总信息
        day_result = next((r for r in trades_log if r["date"] == date), None)
        net_pnl = sum(t["pnl"] - t["cost"] for t in day_trades)
        n_trades = len(day_trades)
        title = f"{date}  |  {n_trades}笔  |  净{net_pnl:+.2f}元"
        ax.set_title(title, fontsize=10, loc="left")
        ax.set_ylabel("价格", fontsize=9)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    fig.suptitle("510300.SH 逐日交易明细", fontsize=14, y=1.01)
    fig.tight_layout()
    daily_path = OUTPUT_DIR / "trades_daily.png"
    fig.savefig(daily_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"[图表] 按日详细图: {daily_path}")


if __name__ == "__main__":
    main()
