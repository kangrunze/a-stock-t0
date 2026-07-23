#!/usr/bin/env python3
"""
cli 入口层模块
================
本模块是 at0 包的 cli 入口层，将原 ``scripts/`` 下三个独立入口脚本合并为一：

- ``backtest`` 入口（原 ``scripts/run_backtest.py``）：单股多日回测，
  含 ``run`` / ``adapt_params_by_frequency`` / ``save_trades_json`` /
  ``save_report_json``。
- ``optimize`` 入口（原 ``scripts/batch_backtest.py``）：批量多股票回测
  与汇总统计，含 ``batch_main``（原 ``main``）。
- ``paper_monitor`` 入口（原 ``scripts/l5_monitor.py``）：L5 T+0 日内做T
  实盘监控（research_only，不执行交易），含 ``monitor_single_stock`` /
  ``monitor_main``（原 ``main``）/ ``is_trading_time`` / ``is_eod_check_time``。
- ``validate_data`` 入口：预留，暂未实现。

合并说明：
  - 三个原入口的函数/类/常量实现完整保留，未做业务逻辑修改。
  - 因合并后存在同名 ``main``，将批量回测入口重命名为 ``batch_main``、
    实盘监控入口重命名为 ``monitor_main``（仅改名，函数体不变）。
  - 原 ``__main__`` argparse 块分别封装为 ``run_backtest_cli`` /
    ``l5_monitor_cli``（batch 入口的 argparse 即在 ``batch_main`` 内），
    并由文件末尾的统一分发器调度。
  - 路径常量（``PROJECT_ROOT`` / ``MARKET_GATE_FILE``）因文件位置从
    ``scripts/`` 迁至 ``src/at0/``，由 ``parent.parent`` 调整为
    ``parent.parent.parent`` 以指向同一绝对路径。

import 策略：
  - 本地模块走包内相对导入（``from .data import ...`` 等）。

用法::

  python -m at0.cli backtest --code 600000 --days 7
  python -m at0.cli optimize --start 2026-07-01 --end 2026-07-22
  python -m at0.cli paper_monitor --source auto
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── 包内相对导入（合并自三个原入口的本地 import）──
from .data import (
    fetch_multi_day,
    normalize_code,
    fetch_minute_bars,
    fetch_realtime_quote,
    check_bar_freshness,
    is_one_word_board,
    is_limit_up_locked,
    is_limit_down_locked,
)
from .backtest import (
    BacktestParams,
    backtest_multi_day,
    print_backtest_summary,
    compute_data_fingerprint,
    generate_run_id,
    save_run_artifacts,
    extract_trades,
    summarize_one_stock,
    aggregate_batch,
)
from .reports import save_html_report, save_batch_html_report
from .strategy import SignalParams, evaluate_all_signals
from .risk import (
    RiskParams,
    check_risk,
    is_l1_systemic_risk,
    is_theme_retreated,
    eod_balance_check_all,
)
from .execution import load_positions, get_position, get_sellable_shares
from .features import compute_market_snapshot, MarketSnapshot, fetch_quote_features
from .logging_utils import log_signal, log_trade, log_monitor
from .config import load_signal_params, load_risk_params


# ═══ cli: run_backtest（单股回测入口） ═══

# 注：原 scripts/run_backtest.py 中 PROJECT_ROOT = parent.parent；
# 本模块位于 src/at0/，需 parent.parent.parent 才能指向项目根。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
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
    report_html = BACKTEST_OUTPUT_DIR / f"{code_tag}_{start_date}_{end_date}_report.html"
    n = save_trades_json(result, daily_meta, trades_json)
    save_report_json(result, daily_meta, report_json)
    save_html_report(result, daily_bars, report_html,
                     code=code_tag, start_date=start_date, end_date=end_date)

    print(f"\n[run_backtest] 买卖触发记录 -> {trades_json}  ({n} 笔)")
    print(f"[run_backtest] 完整报告     -> {report_json}")
    print(f"[run_backtest] HTML 可视化  -> {report_html}")
    print_backtest_summary(result)

    # P0-8: 运行版本化与 artifacts 落盘
    from dataclasses import asdict
    data_fp = compute_data_fingerprint(
        code=normalize_code(code)["pure"],
        start_date=start_date,
        end_date=end_date,
        daily_meta=daily_meta,
        frequency=frequency,
    )
    params_dict = asdict(params)
    run_id = generate_run_id(params_dict, data_fp)
    run_dir = save_run_artifacts(
        run_id=run_id,
        params_dict=params_dict,
        data_fingerprint=data_fp,
        output_files={
            "trades": trades_json,
            "report": report_json,
            "html": report_html,
        },
        run_type="single",
    )
    print(f"[run_backtest] run_id       -> {run_id}")
    print(f"[run_backtest] artifacts    -> {run_dir}")

    return result


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def run_backtest_cli():
    """单股回测 CLI 入口（原 run_backtest.py 的 __main__ 块）。"""
    import argparse
    parser = argparse.ArgumentParser(description="T+0 回测运行器（多数据源）")
    parser.add_argument("--code", default="600000", help="股票代码")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=7, help="回溯天数（无 --start 时用）")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"])
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


# ═══ cli: batch_backtest（批量回测入口） ═══

# 注：PROJECT_ROOT 同 run_backtest 段（parent.parent.parent 指向项目根）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
SUMMARY_PATH = PROJECT_ROOT / "outputs" / "backtest" / "batch_summary.json"
HTML_PATH = PROJECT_ROOT / "outputs" / "backtest" / "batch_summary.html"


def batch_main():
    """批量多股票回测入口（原 batch_backtest.py 的 main，仅改名）。"""
    import argparse
    parser = argparse.ArgumentParser(description="批量多股票回测")
    parser.add_argument("--start", default="2026-06-22", help="起始日期")
    parser.add_argument("--end", default="2026-07-22", help="结束日期")
    parser.add_argument("--source", default="baostock",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"])
    parser.add_argument("--codes", default=None, help="逗号分隔代码（覆盖候选池）")
    parser.add_argument("--base-shares", type=int, default=3000)
    args = parser.parse_args()

    # 候选池
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        if not POOL_PATH.exists():
            print(f"[batch] 候选池不存在: {POOL_PATH}，先跑 gen_candidate_pool.py")
            return
        with open(POOL_PATH, "r", encoding="utf-8") as f:
            pool = json.load(f)
        codes = [c["code"] for c in pool["candidates"]]
        print(f"[batch] 从候选池加载 {len(codes)} 只股票")

    print(f"[batch] 回测窗口 {args.start} ~ {args.end}, source={args.source}")
    print(f"[batch] base_shares={args.base_shares}")
    print("=" * 70)

    per_stock: list[dict] = []
    all_trades_count = 0

    for i, code in enumerate(codes):
        prefix = f"[{i+1}/{len(codes)}] {code}"
        # 捕获 run() 的详细输出（失败时回放）
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = run(
                    code=code,
                    start_date=args.start,
                    end_date=args.end,
                    source=args.source,
                    base_shares=args.base_shares,
                    avg_cost=None,  # 用首日 prev_close
                )
        except Exception as e:
            print(f"{prefix} 异常: {e}")
            per_stock.append({"code": code, "error": str(e),
                              "total_trades": 0, "paired_trades": 0,
                              "win_trades": 0, "win_rate": 0.0, "net_pnl": 0.0})
            continue

        if not result or not result.get("daily_results"):
            print(f"{prefix} 无数据")
            per_stock.append({"code": code, "error": "no_data",
                              "total_trades": 0, "paired_trades": 0,
                              "win_trades": 0, "win_rate": 0.0, "net_pnl": 0.0})
            continue

        trades = extract_trades(result)
        summary = summarize_one_stock(code, result)
        per_stock.append(summary)
        all_trades_count += len(trades)

        wr = f"{summary['win_rate']*100:.1f}%" if summary['paired_trades'] else "N/A"
        print(f"{prefix}  交易={summary['total_trades']:2d} "
              f"配对={summary['paired_trades']:2d} "
              f"胜率={wr:>5s} "
              f"净盈亏={summary['net_pnl']:+8.2f} "
              f"浮盈={summary['unrealized_pnl']:+8.2f} "
              f"未配对腿={summary['final_open_legs_count']}")

    # ── 整体汇总（P0-4: 委托给 backtest_metrics.aggregate_batch）──
    overall = aggregate_batch(per_stock, all_trades_count)

    print("\n" + "=" * 70)
    print("整体汇总")
    print("=" * 70)
    print(f"  股票数:          {overall['stocks']}")
    print(f"  总交易笔数:      {overall['total_trades']}")
    paired_pct = f" ({overall['paired_trades']/overall['total_trades']*100:.1f}%)" if overall['total_trades'] else ""
    print(f"  配对笔数:        {overall['paired_trades']}{paired_pct}" if overall['total_trades'] else "  配对笔数: 0")
    print(f"  盈利笔数:        {overall['win_trades']}")
    print(f"  整体胜率:        {overall['win_rate']*100:.1f}%")
    print(f"  毛利润:          {overall['gross_pnl']:+.2f}")
    print(f"  总成本:          {overall['total_cost']:.2f}")
    print(f"  净盈亏(已实现):  {overall['net_pnl']:+.2f}")
    print(f"  未配对浮盈浮亏:  {overall['unrealized_pnl']:+.2f}")
    print(f"  净盈亏(含浮盈):  {overall['net_pnl_with_unrealized']:+.2f}")
    print(f"  回测结束未配对腿: {overall['final_open_legs_count']}")

    # 按股票拆分
    print("\n" + "-" * 70)
    print("按股票拆分（按含浮盈净盈亏降序）")
    print("-" * 70)
    sorted_by_pnl = sorted(per_stock,
                           key=lambda x: x.get("net_pnl_with_unrealized", x.get("net_pnl", 0)), reverse=True)
    for s in sorted_by_pnl:
        if "error" in s:
            print(f"  {s['code']:12s}  ERROR: {s['error']}")
            continue
        wr = f"{s['win_rate']*100:.1f}%" if s['paired_trades'] else "  N/A"
        print(f"  {s['code']:12s}  交易={s['total_trades']:2d}  "
              f"配对={s['paired_trades']:2d}  胜率={wr:>5s}  "
              f"已实现={s['net_pnl']:+8.2f}  浮盈={s['unrealized_pnl']:+8.2f}  "
              f"含浮盈={s['net_pnl_with_unrealized']:+8.2f}  未配对={s['final_open_legs_count']}")

    # 盈利集中度（按含浮盈口径）
    profitable = [s for s in per_stock
                  if "error" not in s and s.get("net_pnl_with_unrealized", s.get("net_pnl", 0)) > 0]
    losing = [s for s in per_stock
              if "error" not in s and s.get("net_pnl_with_unrealized", s.get("net_pnl", 0)) < 0]
    print(f"\n  盈利股票: {overall['profitable_stocks']} 只, 合计 {sum(s['net_pnl_with_unrealized'] for s in profitable):+.2f}")
    print(f"  亏损股票: {overall['losing_stocks']} 只, 合计 {sum(s['net_pnl_with_unrealized'] for s in losing):+.2f}")

    # 落盘（P0-4: overall 由 aggregate_batch 统一产出）
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "start": args.start,
            "end": args.end,
            "source": args.source,
            "base_shares": args.base_shares,
            "overall": overall,
            "per_stock": per_stock,
        }, f, ensure_ascii=False, indent=2)

    # 生成 HTML 可视化报告
    summary_data = {
        "start": args.start,
        "end": args.end,
        "source": args.source,
        "base_shares": args.base_shares,
        "overall": overall,
        "per_stock": per_stock,
    }
    save_batch_html_report(summary_data, HTML_PATH)

    print(f"\n[batch] 汇总报告 -> {SUMMARY_PATH}")
    print(f"[batch] HTML报告 -> {HTML_PATH}")

    # P0-8: 批量回测运行版本化
    batch_params = {
        "start": args.start,
        "end": args.end,
        "source": args.source,
        "base_shares": args.base_shares,
        "codes_count": len(codes),
    }
    batch_data_fp = {
        "start": args.start,
        "end": args.end,
        "source": args.source,
        "stocks": len(per_stock),
        "codes": [s.get("code", "") for s in per_stock],
    }
    run_id = generate_run_id(batch_params, batch_data_fp)
    run_dir = save_run_artifacts(
        run_id=run_id,
        params_dict=batch_params,
        data_fingerprint=batch_data_fp,
        output_files={"summary": SUMMARY_PATH, "html": HTML_PATH},
        run_type="batch",
    )
    print(f"[batch] run_id    -> {run_id}")
    print(f"[batch] artifacts -> {run_dir}")


# ═══ cli: l5_monitor（实盘监控入口） ═══

# 注：原 scripts/l5_monitor.py 中 MARKET_GATE_FILE = parent.parent / "data" / ...；
# 本模块位于 src/at0/，需 parent.parent.parent 才能指向项目根。
MARKET_GATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "market_gate.json"


# ═══════════════════════════════════════════════════════════════
# 时间窗口判断
# ═══════════════════════════════════════════════════════════════
def is_trading_time() -> bool:
    """A 股交易时段 (9:25-11:35 或 12:55-15:05, 周一至五)。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = (9 * 60 + 25) <= t <= (11 * 60 + 35)
    afternoon = (12 * 60 + 55) <= t <= (15 * 60 + 5)
    return morning or afternoon


def is_eod_check_time() -> bool:
    """是否在尾盘平衡检查时段（14:50-14:55）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (14 * 60 + 50) <= t <= (14 * 60 + 55)


# ═══════════════════════════════════════════════════════════════
# 市场层快照落盘（方案 v0.2 第三节：market_gate.json 供个股层读取）
# ═══════════════════════════════════════════════════════════════
def _save_market_gate_json(market: MarketSnapshot) -> None:
    """将市场层快照写入 data/market_gate.json，供个股层/外部读取。"""
    try:
        gate_data = {
            "market_risk_state": market.market_sentiment,
            "is_tradable": market.is_tradable_market,
            "up_limit_count": market.up_limit_count,
            "down_limit_count": market.down_limit_count,
            "up_ratio": market.up_ratio,
            "total_amount": market.total_amount,
            "top_industries": market.top_industries[:5] if market.top_industries else [],
            "top_concepts": market.top_concepts[:5] if market.top_concepts else [],
            "futures_basis": market.futures_basis,
            "timestamp": market.timestamp,
        }
        MARKET_GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MARKET_GATE_FILE, "w", encoding="utf-8") as f:
            json.dump(gate_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_monitor(f"market_gate.json 落盘失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 合成报价（非 westock 数据源兜底）
# ═══════════════════════════════════════════════════════════════
def _build_synthetic_quote(bars: list[dict], prev_close: float) -> dict:
    """
    用 bars + prev_close 构造合成报价 dict（baostock/mootdx 无实时 quote 时兜底）。

    与 westock_client.fetch_realtime_quote 的字段口径一致：
      涨停 = prev_close × 1.1，跌停 = prev_close × 0.9（四舍五入到分）。
    """
    last = bars[-1] if bars else {}
    return {
        "code": "",
        "price": float(last.get("close", 0)),
        "prev_close": float(prev_close),
        "open": float(bars[0]["open"]) if bars else 0,
        "high": max(float(b["high"]) for b in bars) if bars else 0,
        "low": min(float(b["low"]) for b in bars) if bars else 0,
        "volume": sum(int(b["volume"]) for b in bars) if bars else 0,
        "amount": sum(float(b["amount"]) for b in bars) if bars else 0,
        "limit_up": round(float(prev_close) * 1.1, 2) if prev_close else 0,
        "limit_down": round(float(prev_close) * 0.9, 2) if prev_close else 0,
    }


# ═══════════════════════════════════════════════════════════════
# 单只股票监控
# ═══════════════════════════════════════════════════════════════
def monitor_single_stock(
    code: str,
    pos: dict,
    market: MarketSnapshot = None,
    signal_params: SignalParams = None,
    risk_params: RiskParams = None,
    source: str = "auto",
    trading_date: str = None,
) -> dict:
    """
    对单只持仓股票进行 T 信号监控。

    参数:
      market: 市场层快照（可选），COLD 市场禁加仓。由 main() 每轮统一计算后传入。
      signal_params: 信号参数（来自 config_loader.load_signal_params()）
      risk_params: 风控参数（来自 config_loader.load_risk_params()）
      source: 数据源 'auto'|'mootdx'|'westock'|'baostock'（统一走 data_provider）
      trading_date: 'YYYY-MM-DD'，None=实时（今日），指定日期=历史回放

    返回监控结果 dict。
    """
    signal_params = signal_params or SignalParams()
    risk_params = risk_params or RiskParams()
    today = datetime.now().strftime("%Y-%m-%d")
    trading_date = trading_date or today
    is_realtime = trading_date == today
    result = {
        "code": code,
        "theme": pos.get("sector_tag", ""),  # 输出键名保留 theme 便于展示
        "t_eligible": pos.get("t_eligible", True),
        "base_shares": pos.get("base_shares", 0),
        "action": "none",
        "signal": None,
        "risk_check": None,
        "reason": "",
    }

    if not result["t_eligible"]:
        result["reason"] = "t_eligible=false"
        return result

    # 获取数据（统一走 data_provider，支持 mootdx/westock/baostock）
    bars, prev_close, meta = fetch_minute_bars(code, trading_date, source)
    if not bars or len(bars) < 30:
        result["reason"] = f"数据不足 ({len(bars)} bars, source={meta.get('source')})"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 数据时效性检查（仅实时模式；历史回放不检查）
    if is_realtime and not check_bar_freshness(bars):
        result["reason"] = "数据陈旧（>2分钟无更新）"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 报价：实时模式优先 westock 实时报价，失败/历史模式用 prev_close 构造合成报价
    quote = None
    if is_realtime and source in ("auto", "westock"):
        try:
            quote = fetch_realtime_quote(code)
        except Exception:
            quote = None
    if not quote:
        quote = _build_synthetic_quote(bars, prev_close)

    # 一字板过滤
    if is_one_word_board(quote):
        result["reason"] = "一字板，无法做T"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 涨跌停封板检测
    l1_risk = is_l1_systemic_risk()
    theme_name = pos.get("sector_tag")
    retreated = is_theme_retreated(theme_name)

    is_lu_locked = is_limit_up_locked(quote, bars)
    is_ld_locked = is_limit_down_locked(quote, bars)

    # 盘口特征（westock quote 现成字段，用于订单流代理指标）
    quote_feats = fetch_quote_features(code)

    # 评估信号（接入市场层门控 + 盘口特征 + 配置参数）
    eval_result = evaluate_all_signals(
        bars=bars,
        current_price=quote.get("price"),
        prev_close=quote.get("prev_close"),
        is_limit_up_locked=is_lu_locked,
        is_limit_down_locked=is_ld_locked,
        theme_retreated=retreated,
        params=signal_params,
        market=market,
        quote_feats=quote_feats,
    )

    reduce_sig = eval_result["reduce_signal"]
    add_sig = eval_result["add_signal"]
    recommendation = eval_result["recommendation"]

    # 记录信号评估（无论是否触发）
    log_signal(
        code=code,
        direction=recommendation,
        recommendation=recommendation,
        reduce_score=reduce_sig.rules_score,
        add_score=add_sig.rules_score,
        price=quote.get("price", 0),
        snapshot=eval_result["snapshot"],
        reduce_rules=reduce_sig.rules_fired,
        add_rules=add_sig.rules_fired,
    )

    result["signal"] = {
        "recommendation": recommendation,
        "reduce_score": reduce_sig.rules_score,
        "add_score": add_sig.rules_score,
        "price": quote.get("price", 0),
        "rules": reduce_sig.rules_fired if recommendation == "reduce" else add_sig.rules_fired,
    }

    # 无信号
    if recommendation == "none" or recommendation == "conflict":
        result["reason"] = f"recommendation={recommendation}"
        return result

    # 风控校验
    direction = "sell" if recommendation == "reduce" else "buy"
    ref_price = eval_result["snapshot"].get("vwap") or quote.get("prev_close", 0)
    requested_shares = int(pos.get("base_shares", 0) * risk_params.max_t_size_ratio)
    requested_shares = (requested_shares // 100) * 100

    risk_result = check_risk(
        code=code,
        direction=direction,
        requested_shares=requested_shares,
        signal_price=quote.get("price", 0),
        reference_price=ref_price,
        params=risk_params,
    )
    result["risk_check"] = {
        "approved": risk_result.approved,
        "reason": risk_result.reason,
        "adjusted_shares": risk_result.adjusted_shares,
        "checks": risk_result.checks,
    }

    if not risk_result.approved:
        result["reason"] = f"风控拒绝: {risk_result.reason}"
        return result

    # 信号通过风控 → 输出提醒（research_only，不执行交易）
    t_type = ""
    if recommendation == "reduce":
        t_type = "正T-卖出" if not l1_risk else "正T-卖出（L1风险日仅减仓）"
    else:
        t_type = "反T-买入" if not retreated else "反T-买入（题材退潮仅卖允许）"

    result["action"] = "signal"
    result["reason"] = (
        f"{t_type} {risk_result.adjusted_shares} 股 @ {quote.get('price', 0):.2f} — "
        f"参考价 {ref_price:.2f}"
    )

    # 记录到交易日志（标记为 research_only）
    log_trade(
        code=code,
        t_type=t_type,
        direction=direction,
        shares=risk_result.adjusted_shares,
        price=quote.get("price", 0),
        reference_price=ref_price,
        rules_fired=reduce_sig.rules_fired if recommendation == "reduce" else add_sig.rules_fired,
        rules_score=reduce_sig.rules_score if recommendation == "reduce" else add_sig.rules_score,
        risk_approved=True,
        risk_checks=risk_result.checks,
        bar_time=bars[-1].get("time", ""),
        notes="research_only — 信号提醒，未实际执行",
    )

    return result


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def monitor_main(source: str = "auto", trading_date: str = None) -> int:
    """
    主入口。返回 0 (无信号) 或 1 (有信号输出)。

    参数:
      source: 数据源 'auto'|'mootdx'|'westock'|'baostock'
      trading_date: None=实时（今日），'YYYY-MM-DD'=历史回放
    """
    today = datetime.now().strftime("%Y-%m-%d")
    trading_date = trading_date or today
    is_realtime = trading_date == today

    # 时间窗口（仅实时模式检查；历史回放不受交易时段限制）
    if is_realtime and not is_trading_time():
        log_monitor("skip non_trading_time")
        return 0

    # 尾盘平衡检查（仅实时模式）
    if is_realtime and is_eod_check_time():
        eod_results = eod_balance_check_all()
        for eod in eod_results:
            if eod.get("status") in {"net_reduce", "net_add"}:
                log_monitor(
                    f"EOD {eod['code']}: {eod['status']} delta={eod['net_position_delta']} — {eod['action']}"
                )

    # 加载持仓
    positions = load_positions()
    if not positions:
        log_monitor("no positions")
        return 0

    # 加载配置参数（P0-2：实盘入口接入 config_loader，不再依赖 dataclass 默认值）
    # thresholds.yaml 缺失时 config_loader 会回退到 DEFAULT_PARAMS/DEFAULT_RISK_PARAMS
    signal_params = load_signal_params()
    risk_params = load_risk_params()

    # 市场层快照（跨股票共享，每轮计算一次，落盘 market_gate.json）
    # westock 为可选外部数据源：未配置 WESTOCK_DIR 时降级为独立模式
    # （市场情绪 NEUTRAL，无涨跌停/板块热度数据），与 L1/L2 软依赖处理一致
    use_westock = bool(os.environ.get("WESTOCK_DIR"))
    if not use_westock:
        print("[WARN] WESTOCK_DIR 未设置，市场层降级为独立模式"
              "（NEUTRAL 情绪，无涨跌停/板块数据）", file=sys.stderr)
    market = compute_market_snapshot(use_westock=use_westock)
    _save_market_gate_json(market)

    mode_label = f"历史回放 {trading_date}" if not is_realtime else "实时"
    log_monitor(f"run source={source} mode={mode_label} positions={len(positions)}")

    # 逐只监控
    results = []
    signal_count = 0
    for code, pos in positions.items():
        r = monitor_single_stock(
            code, pos, market=market,
            signal_params=signal_params, risk_params=risk_params,
            source=source, trading_date=trading_date,
        )
        results.append(r)
        if r["action"] == "signal":
            signal_count += 1

    # 输出
    if signal_count == 0:
        log_monitor(f"no_signal positions={len(positions)}")
        return 0

    # 格式化输出
    ts = datetime.now().strftime("%m-%d %H:%M")
    lines = [f"L5 日内做T信号提醒｜{ts} — research_only"]
    lines.append("说明：这是研究/监控信号，不是自动交易或立即执行指令。")
    lines.append("")

    for r in results:
        if r["action"] != "signal":
            continue
        sig = r["signal"]
        lines.append(
            f"- {r['code']} ({r['theme']})\n"
            f"  动作: {r['reason']}\n"
            f"  信号: {sig['recommendation']} (reduce={sig['reduce_score']}/4, add={sig['add_score']}/4)\n"
            f"  规则: {' | '.join(sig['rules'])}"
        )

    output = "\n".join(lines)
    print(output)
    log_monitor(f"signal positions={len(positions)} signals={signal_count}")
    return 1


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
def l5_monitor_cli():
    """L5 实盘监控 CLI 入口（原 l5_monitor.py 的 __main__ 块）。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="L5 T+0 日内做T监控（多数据源：eastmoney/mootdx/westock/baostock）"
    )
    parser.add_argument("--source", default="auto",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"],
                        help="数据源：auto=自动回退(默认) | eastmoney(免依赖,实时) | mootdx | westock | baostock(历史)")
    parser.add_argument("--date", default=None,
                        help="交易日 YYYY-MM-DD（不传=实时今日，传=历史回放）")
    parser.add_argument("--demo", action="store_true",
                        help="测试模式：忽略交易时段限制（实时模式用）")
    parser.add_argument("--eod-check", action="store_true",
                        help="仅执行尾盘平衡检查")
    args = parser.parse_args()

    if args.demo:
        # 测试模式：忽略时间窗口
        globals()["is_trading_time"] = lambda: True
        print("[DEMO] 测试模式 — 忽略时间窗口限制", file=sys.stderr)

    if args.eod_check:
        # 仅执行尾盘平衡检查
        results = eod_balance_check_all()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        sys.exit(0)

    sys.exit(monitor_main(source=args.source, trading_date=args.date))


# ═══════════════════════════════════════════════════════════════
# 统一入口分发
# 用法: python -m at0.cli <backtest|optimize|paper_monitor|validate_data> [args...]
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    _SUBCOMMANDS = ("backtest", "optimize", "paper_monitor", "validate_data")
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("用法: python -m at0.cli <backtest|optimize|paper_monitor|validate_data> [args...]")
        print("  backtest       单股多日回测（原 run_backtest.py）")
        print("  optimize       批量多股票回测与汇总（原 batch_backtest.py）")
        print("  paper_monitor  L5 T+0 日内做T实盘监控（原 l5_monitor.py）")
        print("  validate_data  数据校验入口（预留，暂未实现）")
        sys.exit(0)
    _sub = sys.argv[1]
    # 剥离子命令，让对应入口的 argparse 正常解析剩余参数
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if _sub == "backtest":
        run_backtest_cli()
    elif _sub == "optimize":
        batch_main()
    elif _sub == "paper_monitor":
        l5_monitor_cli()
    elif _sub == "validate_data":
        print("validate_data 入口暂未实现")
        sys.exit(0)
    else:
        print(f"未知子命令: {_sub}（可用: {', '.join(_SUBCOMMANDS)}）", file=sys.stderr)
        sys.exit(2)
