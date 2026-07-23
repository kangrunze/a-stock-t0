"""
A-T0 交易日志记录器
=====================
记录每笔 T 交易的完整明细，供复盘统计胜率、盈亏比、成本覆盖率。

输出文件:
  - state/signals.csv   信号记录（每次评估都记）
  - state/trades.csv    成交记录（风控通过后记）
  - state/monitor.log   运行日志

独立性：纯日志模块，不依赖其他业务模块。
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 项目根目录（src/at0/logging_utils.py → src/at0/ → src/ → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = PROJECT_ROOT / "state"
SIGNALS_CSV = STATE_DIR / "signals.csv"
TRADES_CSV = STATE_DIR / "trades.csv"
MONITOR_LOG = STATE_DIR / "monitor.log"


# ═══════════════════════════════════════════════════════════════
# 运行日志
# ═══════════════════════════════════════════════════════════════
def log_monitor(message: str) -> None:
    """写入监控运行日志。stdout 保持纯净，仅日志落盘。"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with MONITOR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 信号日志
# ═══════════════════════════════════════════════════════════════
SIGNALS_HEADER = [
    "timestamp", "code", "direction", "recommendation",
    "reduce_score", "add_score",
    "price", "vwap", "vwap_dev", "atr", "rsi", "kdj_k",
    "rules_fired", "snapshot_json",
]


def log_signal(
    code: str,
    direction: str,
    recommendation: str,
    reduce_score: int,
    add_score: int,
    price: float,
    snapshot: dict,
    reduce_rules: list[str],
    add_rules: list[str],
) -> None:
    """记录一次信号评估结果（无论是否触发）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = SIGNALS_CSV.exists()
    with open(SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(SIGNALS_HEADER)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            code,
            direction,
            recommendation,
            reduce_score,
            add_score,
            price,
            snapshot.get("vwap", ""),
            snapshot.get("vwap_dev", ""),
            snapshot.get("atr", ""),
            snapshot.get("rsi", ""),
            snapshot.get("kdj_k", ""),
            "REDUCE: " + " | ".join(reduce_rules) + " || ADD: " + " | ".join(add_rules),
            "",  # snapshot_json 留空，避免 CSV 膨胀
        ])


# ═══════════════════════════════════════════════════════════════
# 成交日志
# ═══════════════════════════════════════════════════════════════
TRADES_HEADER = [
    "timestamp", "code", "t_type", "direction",
    "shares", "price", "amount",
    "reference_price", "expected_spread",
    "rules_fired", "rules_score",
    "risk_approved", "risk_checks",
    "cost_estimate", "net_pnl_estimate",
    "bar_time", "notes",
]


def log_trade(
    code: str,
    t_type: str,              # "正T-卖出" / "正T-买回" / "反T-买入" / "反T-卖出"
    direction: str,           # "buy" / "sell"
    shares: int,
    price: float,
    reference_price: float,
    rules_fired: list[str],
    rules_score: int,
    risk_approved: bool,
    risk_checks: list[str],
    cost_estimate: float = 0.0,
    net_pnl_estimate: float = 0.0,
    bar_time: str = "",
    notes: str = "",
) -> None:
    """
    记录一笔 T 交易（风控通过后的成交）。

    cost_estimate: 估算的来回成本（元）
    net_pnl_estimate: 估算的净盈亏（元，扣成本前）
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    amount = shares * price
    expected_spread = (
        abs(price - reference_price) / reference_price
        if reference_price > 0
        else 0.0
    )
    file_exists = TRADES_CSV.exists()
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(TRADES_HEADER)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            code,
            t_type,
            direction,
            shares,
            price,
            amount,
            reference_price,
            f"{expected_spread*100:.2f}%",
            " | ".join(rules_fired),
            rules_score,
            risk_approved,
            " | ".join(risk_checks),
            cost_estimate,
            net_pnl_estimate,
            bar_time,
            notes,
        ])


# ═══════════════════════════════════════════════════════════════
# 复盘统计
# ═══════════════════════════════════════════════════════════════
def compute_trade_stats(trades_csv: Path = TRADES_CSV) -> dict:
    """
    从 trades.csv 统计胜率、盈亏比、成本覆盖率等。

    返回:
    {
        "total_trades": int,
        "buy_count": int, "sell_count": int,
        "total_amount": float,
        "total_cost": float,
        "total_pnl": float,
        "win_rate": float,  # 净盈亏 > 0 的比例
        "avg_spread": float,
    }
    """
    if not trades_csv.exists():
        return {"total_trades": 0}

    buy_count = 0
    sell_count = 0
    total_amount = 0.0
    total_cost = 0.0
    total_pnl = 0.0
    spreads = []
    win_count = 0

    with open(trades_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            direction = row.get("direction", "")
            shares = int(row.get("shares", 0))
            price = float(row.get("price", 0))
            cost = float(row.get("cost_estimate", 0))
            pnl = float(row.get("net_pnl_estimate", 0))
            spread_str = row.get("expected_spread", "0%").replace("%", "")
            try:
                spread = float(spread_str) / 100
            except ValueError:
                spread = 0.0

            if direction == "buy":
                buy_count += 1
            else:
                sell_count += 1
            total_amount += shares * price
            total_cost += cost
            total_pnl += pnl
            spreads.append(spread)
            if pnl > 0:
                win_count += 1

    total = buy_count + sell_count
    return {
        "total_trades": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_amount": total_amount,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "win_rate": win_count / total if total > 0 else 0.0,
        "avg_spread": sum(spreads) / len(spreads) if spreads else 0.0,
    }


if __name__ == "__main__":
    # 清理旧日志
    for f in [SIGNALS_CSV, TRADES_CSV, MONITOR_LOG]:
        if f.exists():
            f.unlink()

    log_monitor("L5 trade logger self-test start")

    # 记录一个信号
    log_signal(
        code="600xxx.SH",
        direction="reduce",
        recommendation="reduce",
        reduce_score=4,
        add_score=1,
        price=12.50,
        snapshot={"vwap": 12.35, "vwap_dev": 0.012, "atr": 0.05, "rsi": 75.0, "kdj_k": 85.0},
        reduce_rules=["VWAP偏离度≥0.8×ATR", "RSI>70"],
        add_rules=[],
    )

    # 记录一笔正T卖出成交
    log_trade(
        code="600xxx.SH",
        t_type="正T-卖出",
        direction="sell",
        shares=1000,
        price=12.50,
        reference_price=12.35,
        rules_fired=["VWAP偏离度≥0.8×ATR", "RSI>70", "缩量", "未涨停"],
        rules_score=4,
        risk_approved=True,
        risk_checks=["仓位比例✓", "T次数✓", "价差✓", "可用底仓✓", "L1✓", "L2✓"],
        cost_estimate=12.5,
        net_pnl_estimate=137.5,
        bar_time="10:15:00",
        notes="正T卖出 1000 股底仓",
    )

    # 记录一笔正T买回成交
    log_trade(
        code="600xxx.SH",
        t_type="正T-买回",
        direction="buy",
        shares=1000,
        price=12.20,
        reference_price=12.35,
        rules_fired=["VWAP偏离度≤-0.8×ATR", "RSI<30", "地量企稳", "题材未退潮"],
        rules_score=4,
        risk_approved=True,
        risk_checks=["仓位比例✓", "T次数✓", "价差✓", "L1✓", "L2✓"],
        cost_estimate=12.2,
        net_pnl_estimate=137.8,
        bar_time="13:45:00",
        notes="正T买回 1000 股",
    )

    # 统计
    stats = compute_trade_stats()
    print("=== Trade Stats ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print(f"\n[OK] signals.csv: {SIGNALS_CSV}")
    print(f"[OK] trades.csv:  {TRADES_CSV}")
    print(f"[OK] monitor.log: {MONITOR_LOG}")
