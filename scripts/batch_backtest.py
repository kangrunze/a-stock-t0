#!/usr/bin/env python3
"""
批量多股票回测 → 汇总统计
=========================
读取 candidate_pool.json 候选池，对每只股票调用 run_backtest.run()，
产出整体胜率/净盈亏 + 按股票拆分的胜率分布。

用法:
  python batch_backtest.py                         # 默认近1个月 baostock
  python batch_backtest.py --start 2026-07-01 --end 2026-07-22
  python batch_backtest.py --codes sh.600176,sh.600183  # 指定代码
"""
from __future__ import annotations

import io
import json
import sys
import contextlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_backtest import run

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"
SUMMARY_PATH = PROJECT_ROOT / "outputs" / "backtest" / "batch_summary.json"


def extract_trades(result: dict) -> list[dict]:
    """从 backtest_multi_day 的 result 提取扁平 trades 列表。"""
    trades = []
    for dr in result.get("daily_results", []):
        for t in dr.get("trades", []):
            trades.append(t)
    return trades


def summarize_one(code: str, trades: list[dict]) -> dict:
    """单只股票汇总。"""
    paired = [t for t in trades if t.get("paired")]
    wins = [t for t in paired if t.get("pnl", 0) > 0]
    losses = [t for t in paired if t.get("pnl", 0) < 0]
    # pnl=毛利（配对价差），cost=每笔手续费；净盈亏 = sum(pnl) - sum(cost)
    gross_pnl = sum(t.get("pnl", 0) for t in trades)
    total_cost = sum(t.get("cost", 0) for t in trades)
    return {
        "code": code,
        "total_trades": len(trades),
        "paired_trades": len(paired),
        "unpaired_trades": len(trades) - len(paired),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": round(len(wins) / len(paired), 4) if paired else 0.0,
        "gross_pnl": round(gross_pnl, 2),
        "total_cost": round(total_cost, 2),
        "net_pnl": round(gross_pnl - total_cost, 2),
    }


def main():
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
        summary = summarize_one(code, trades)
        per_stock.append(summary)
        all_trades_count += len(trades)

        wr = f"{summary['win_rate']*100:.1f}%" if summary['paired_trades'] else "N/A"
        print(f"{prefix}  交易={summary['total_trades']:2d} "
              f"配对={summary['paired_trades']:2d} "
              f"胜率={wr:>5s} "
              f"净盈亏={summary['net_pnl']:+8.2f}")

    # ── 整体汇总 ──
    total_paired = sum(s.get("paired_trades", 0) for s in per_stock)
    total_wins = sum(s.get("win_trades", 0) for s in per_stock)
    total_net = sum(s.get("net_pnl", 0) for s in per_stock)
    total_gross = sum(s.get("gross_pnl", 0) for s in per_stock)
    total_cost = sum(s.get("total_cost", 0) for s in per_stock)
    overall_wr = (total_wins / total_paired) if total_paired else 0.0

    print("\n" + "=" * 70)
    print("整体汇总")
    print("=" * 70)
    print(f"  股票数:     {len(per_stock)}")
    print(f"  总交易笔数: {all_trades_count}")
    print(f"  配对笔数:   {total_paired}")
    print(f"  盈利笔数:   {total_wins}")
    print(f"  整体胜率:   {overall_wr*100:.1f}%")
    print(f"  毛利润:     {total_gross:+.2f}")
    print(f"  总成本:     {total_cost:.2f}")
    print(f"  净盈亏:     {total_net:+.2f}")

    # 按股票拆分
    print("\n" + "-" * 70)
    print("按股票拆分（按净盈亏降序）")
    print("-" * 70)
    sorted_by_pnl = sorted(per_stock,
                           key=lambda x: x.get("net_pnl", 0), reverse=True)
    for s in sorted_by_pnl:
        if "error" in s:
            print(f"  {s['code']:12s}  ERROR: {s['error']}")
            continue
        wr = f"{s['win_rate']*100:.1f}%" if s['paired_trades'] else "  N/A"
        print(f"  {s['code']:12s}  交易={s['total_trades']:2d}  "
              f"配对={s['paired_trades']:2d}  胜率={wr:>5s}  "
              f"净盈亏={s['net_pnl']:+8.2f}")

    # 盈利集中度
    profitable = [s for s in per_stock
                  if "error" not in s and s.get("net_pnl", 0) > 0]
    losing = [s for s in per_stock
              if "error" not in s and s.get("net_pnl", 0) < 0]
    print(f"\n  盈利股票: {len(profitable)} 只, 合计 {sum(s['net_pnl'] for s in profitable):+.2f}")
    print(f"  亏损股票: {len(losing)} 只, 合计 {sum(s['net_pnl'] for s in losing):+.2f}")
    if profitable:
        top3 = sorted(profitable, key=lambda x: x['net_pnl'], reverse=True)[:3]
        top3_sum = sum(s['net_pnl'] for s in top3)
        print(f"  盈利前3名合计: {top3_sum:+.2f} "
              f"(占整体净盈亏的 {top3_sum/total_net*100:.0f}%)" if total_net else "")

    # 落盘
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "start": args.start,
            "end": args.end,
            "source": args.source,
            "base_shares": args.base_shares,
            "overall": {
                "stocks": len(per_stock),
                "total_trades": all_trades_count,
                "paired_trades": total_paired,
                "win_trades": total_wins,
                "win_rate": round(overall_wr, 4),
                "gross_pnl": round(total_gross, 2),
                "total_cost": round(total_cost, 2),
                "net_pnl": round(total_net, 2),
                "profitable_stocks": len(profitable),
                "losing_stocks": len(losing),
            },
            "per_stock": per_stock,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[batch] 汇总报告 -> {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
