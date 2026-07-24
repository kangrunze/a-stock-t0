#!/usr/bin/env python3
"""
批量回测脚本
============
读取候选池 JSON，逐股跑 backtest_multi_day，生成 batch_summary.json。

用法:
  python scripts/batch_backtest.py --pool outputs/backtest/candidate_pool_500.json \
      --start 2025-07-24 --end 2026-07-22 --source baostock \
      --out outputs/backtest/batch_summary_500.json

支持 --limit 限制股票数量（小范围验证用）。
数据缓存自动启用（fetch_multi_day use_cache=True），二次运行直接读缓存。
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import (BacktestParams, backtest_multi_day,
                          summarize_one_stock, aggregate_batch, extract_trades)
from at0.strategy import SignalParams
from at0.risk import RiskParams


def adapt_params(params: BacktestParams, frequency: str, bars_per_day: int) -> BacktestParams:
    """按频率自适应 warmup/eod_check 参数（内联自 cli.adapt_params_by_frequency）。"""
    if frequency == "5min":
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
    else:
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
    return params


def main():
    parser = argparse.ArgumentParser(description="批量回测")
    parser.add_argument("--pool", default="outputs/backtest/candidate_pool_500.json",
                        help="候选池 JSON 路径")
    parser.add_argument("--start", default="2025-07-24", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-07-22", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--source", default="baostock", help="数据源")
    parser.add_argument("--out", default="outputs/backtest/batch_summary_500.json",
                        help="输出 JSON 路径")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制股票数量（0=全部）")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    pool_path = Path(args.pool) if Path(args.pool).is_absolute() else project_root / args.pool
    out_path = Path(args.out) if Path(args.out).is_absolute() else project_root / args.out

    with open(pool_path, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    if args.limit > 0:
        codes = codes[:args.limit]
    print(f"[batch] 候选池 {len(codes)} 只股票，{args.start}~{args.end}")

    per_stock = []
    all_trades_count = 0
    ok_count = 0
    err_count = 0

    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code} ...", end=" ", flush=True)
        try:
            daily_bars, daily_prev, daily_meta = fetch_multi_day(
                code, args.start, args.end, args.source, use_cache=True
            )
            if not daily_bars:
                print("无数据，跳过")
                err_count += 1
                continue

            pure_code = normalize_code(code)["pure"]
            first_meta = next(iter(daily_meta.values()))
            freq = first_meta.get("frequency", "5min")
            bpd = first_meta.get("bars_count", 48)
            first_date = min(daily_prev.keys())
            avg_cost = daily_prev[first_date]

            params = BacktestParams(
                base_shares=3000, avg_cost=avg_cost,
                signal_params=SignalParams(), risk_params=RiskParams(),
            )
            adapt_params(params, freq, bpd)

            result = backtest_multi_day(pure_code, daily_bars, daily_prev, params)
            summary = summarize_one_stock(pure_code, result)
            all_trades_count += summary["total_trades"]
            per_stock.append(summary)
            ok_count += 1
            print(f"net={summary['net_pnl']:+.0f} "
                  f"expired={summary['expired_legs_count']}/{summary['expired_legs_real_pnl']:+.0f} "
                  f"net_w_u={summary['net_pnl_with_unrealized']:+.0f} "
                  f"paired={summary['paired_trades']} win={summary['win_rate']*100:.0f}%")
        except Exception as e:
            print(f"ERROR: {e}")
            err_count += 1
            per_stock.append({"code": code, "error": str(e)})

    print(f"\n[batch] 完成: 成功 {ok_count} 失败 {err_count}")

    overall = aggregate_batch(per_stock, all_trades_count)
    print("\n=== 批量回测汇总 ===")
    print(f"股票数: {overall['stocks']}")
    print(f"总交易数: {overall['total_trades']}")
    print(f"配对交易数: {overall['paired_trades']}")
    print(f"胜率: {overall['win_rate']*100:.2f}%")
    print(f"净盈亏(配对): {overall['net_pnl']:+.0f}")
    print(f"未配对浮盈: {overall['unrealized_pnl']:+.0f}")
    print(f"超时腿真实盈亏: {overall['expired_legs_real_pnl']:+.0f} ({overall['expired_legs_count']}条)")
    print(f"真正净盈亏(含浮盈+超时): {overall['net_pnl_with_unrealized']:+.0f}")
    print(f"盈利股票: {overall['profitable_stocks']}  亏损股票: {overall['losing_stocks']}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "start_date": args.start,
            "end_date": args.end,
            "source": args.source,
            "pool_path": str(pool_path),
            "overall": overall,
            "per_stock": per_stock,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n已保存: {out_path}")


if __name__ == "__main__":
    main()
