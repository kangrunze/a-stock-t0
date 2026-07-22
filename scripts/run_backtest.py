#!/usr/bin/env python3
"""
T+0 策略回测运行器（多数据源 → 统一出口 → 回测 → 买卖触发记录）
============================================================
流程:
  1. data_provider.fetch_multi_day 拉取多日分钟数据（mootdx→westock→baostock 自动回退）
  2. 按数据频率自适应 BacktestParams（5min 线缩小 warmup/eod 索引）
  3. backtest_t_strategy.backtest_multi_day 执行回测
  4. 输出:
     - {code}_{start}_{end}_trades.csv   扁平买卖触发记录（每行一笔）
     - {code}_{start}_{end}_report.json  完整回测报告（含每日明细）

用法:
  python run_backtest.py --code 600000 --days 7
  python run_backtest.py --code sh.600000 --start 2026-07-15 --end 2026-07-22 --source baostock
  python run_backtest.py --code 600000 --base-shares 5000 --avg-cost 8.95
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_provider import fetch_multi_day, normalize_code
from backtest_t_strategy import BacktestParams, backtest_multi_day, print_backtest_summary
from t_signal_engine import SignalParams
from t_risk_guard import RiskParams


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "backtest"


# ═══════════════════════════════════════════════════════════════
# 按数据频率自适应回测参数
# ═══════════════════════════════════════════════════════════════
def adapt_params_by_frequency(
    params: BacktestParams,
    frequency: str,
    bars_per_day: int,
) -> BacktestParams:
    """
    5 分钟线一天 48 根，1 分钟线一天 240 根。
    warmup_bars / eod_check_bar_idx 原按 1 分钟(240根)设计，需按比例缩放。
    """
    if frequency == "5min":
        # 预热 30 分钟: 1分钟用30根 → 5分钟用6根
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        # 14:50 约对应 5分钟线第 33 根（9:35起，上午23根+下午10根）
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
    else:  # 1min
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
    return params


# ═══════════════════════════════════════════════════════════════
# 输出：买卖触发记录 JSON（扁平数组）
# ═══════════════════════════════════════════════════════════════
def save_trades_json(
    result: dict,
    daily_meta: dict,
    out_path: Path,
) -> int:
    """
    扁平化买卖触发记录到 JSON 数组，每笔一条。
    返回写入的笔数。

    注: 用 .json 而非 .csv/.txt —— Trae 沙箱会对 .csv/.txt 加密成 TSD，
    .json 保持明文可读。
    """
    import json as _json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    for day_result in result.get("daily_results", []):
        d = day_result["date"]
        meta = daily_meta.get(d, {})
        src = meta.get("source", "")
        freq = meta.get("frequency", "")
        for t in day_result.get("trades", []):
            records.append({
                "date": d,
                "time": t.get("time", ""),
                "direction": t.get("direction", ""),
                "shares": t.get("shares", 0),
                "signal_price": t.get("signal_price", 0),
                "fill_price": t.get("fill_price", 0),
                "cost": t.get("cost", 0),
                "pnl": t.get("pnl", 0),
                "paired": t.get("paired", False),
                "rules_score": t.get("rules_score", 0),
                "rules_fired": t.get("rules_fired", []),
                "vwap": t.get("vwap", 0),
                "expected_spread": t.get("expected_spread", 0),
                "source": src,
                "frequency": freq,
            })
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(records, f, ensure_ascii=False, indent=2, default=str)
    return len(records)


def save_report_json(result: dict, daily_meta: dict, out_path: Path) -> None:
    """完整回测报告 JSON（含数据源元信息）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 在报告里附加数据源汇总
    sources_used = sorted({
        daily_meta.get(dr.get("date"), {}).get("source")
        for dr in result.get("daily_results", [])
        if daily_meta.get(dr.get("date"), {}).get("source")
    })
    report = {
        **result,
        "data_sources_used": sources_used,
        "daily_meta": daily_meta,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def run(
    code: str,
    start_date: str,
    end_date: str,
    source: str = "auto",
    base_shares: int = 3000,
    avg_cost: float | None = None,
    l1_risk_dates: set[str] | None = None,
    retreated_dates: set[str] | None = None,
) -> dict:
    """
    执行完整回测流程，返回报告 dict（同时落盘 CSV/JSON）。
    """
    # 1. 拉数据
    print(f"[run_backtest] 拉取 {code} {start_date}~{end_date} (source={source}) ...")
    daily_bars, daily_prev_closes, daily_meta = fetch_multi_day(
        code, start_date, end_date, source
    )
    if not daily_bars:
        print("[run_backtest] 未拉到任何交易日数据，退出")
        return {}

    # 2. 推断频率（取首个交易日 meta）
    first_meta = next(iter(daily_meta.values()))
    frequency = first_meta.get("frequency", "1min")
    bars_per_day = first_meta.get("bars_count", 240)
    print(f"[run_backtest] 数据频率={frequency}, 约 {bars_per_day} 根/天")

    # 3. 底仓成本：未指定则用首日 prev_close
    if avg_cost is None:
        first_date = min(daily_prev_closes.keys())
        avg_cost = daily_prev_closes[first_date]
        print(f"[run_backtest] avg_cost 未指定，取首日 prev_close={avg_cost:.4f}")

    # 4. 构造回测参数 + 频率自适应
    params = BacktestParams(
        base_shares=base_shares,
        avg_cost=avg_cost,
        signal_params=SignalParams(),
        risk_params=RiskParams(),
    )
    params = adapt_params_by_frequency(params, frequency, bars_per_day)
    print(f"[run_backtest] warmup_bars={params.warmup_bars}, "
          f"eod_check_bar_idx={params.eod_check_bar_idx}")

    # 5. 回测
    result = backtest_multi_day(
        code=normalize_code(code)["pure"],
        daily_bars=daily_bars,
        daily_prev_closes=daily_prev_closes,
        params=params,
        l1_risk_dates=l1_risk_dates,
        retreated_dates=retreated_dates,
    )

    # 6. 输出
    code_tag = normalize_code(code)["pure"]
    # 注: .csv/.txt 会被 Trae 沙箱加密成 TSD，统一用 .json 保持明文
    trades_json = BACKTEST_OUTPUT_DIR / f"{code_tag}_{start_date}_{end_date}_trades.json"
    report_json = BACKTEST_OUTPUT_DIR / f"{code_tag}_{start_date}_{end_date}_report.json"
    n = save_trades_json(result, daily_meta, trades_json)
    save_report_json(result, daily_meta, report_json)

    print(f"\n[run_backtest] 买卖触发记录 -> {trades_json}  ({n} 笔)")
    print(f"[run_backtest] 完整报告     -> {report_json}")
    print_backtest_summary(result)
    return result


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="T+0 回测运行器（多数据源）")
    parser.add_argument("--code", default="600000", help="股票代码")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7, help="回溯天数（无 --start 时用）")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "mootdx", "westock", "baostock"])
    parser.add_argument("--base-shares", type=int, default=3000, help="底仓股数")
    parser.add_argument("--avg-cost", type=float, default=None, help="底仓成本")
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.now()
    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else end - timedelta(days=args.days)

    run(
        code=args.code,
        start_date=start.strftime("%Y-%m-%d"),
        end_date=end.strftime("%Y-%m-%d"),
        source=args.source,
        base_shares=args.base_shares,
        avg_cost=args.avg_cost,
    )
