#!/usr/bin/env python3
"""
生成 T-eligible 候选池（baostock 筛选），保存到 JSON。
用于第三步批量回测的候选股票池。

支持两种模式：
  --universe hs300  : 沪深300成分股（默认，最多300只）
  --universe all_a   : 全 A 股（沪深，baostock query_all_stock）
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from at0.screener import (screen_hs300_baostock, screen_all_a_baostock,
                          DEFAULT_SCREENER_PARAMS)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 T-eligible 候选池")
    parser.add_argument("--end-date", default="2026-07-22", help="截止日期 YYYY-MM-DD")
    parser.add_argument("--max-count", type=int, default=20, help="最大入选数量")
    parser.add_argument("--universe", default="hs300",
                        choices=["hs300", "all_a"], help="股票池范围")
    parser.add_argument("--out", default=str(OUT), help="输出路径")
    args = parser.parse_args()

    end_date = args.end_date
    print(f"=== 生成候选池 universe={args.universe} end_date={end_date} max_count={args.max_count} ===")
    print(f"    筛选阈值: 振幅≥{DEFAULT_SCREENER_PARAMS.min_20d_amplitude*100}% "
          f"额≥{DEFAULT_SCREENER_PARAMS.min_20d_amount/1e8}亿")

    if args.universe == "hs300":
        results = screen_hs300_baostock(end_date=end_date, max_count=args.max_count)
    else:
        results = screen_all_a_baostock(end_date=end_date, max_count=args.max_count)

    pool = []
    for r in results:
        pool.append({
            "code": r.code,
            "avg_amplitude_20d": round(r.avg_amplitude_20d or 0, 4),
            "avg_amount_20d": round(r.avg_amount_20d or 0, 0),
            "reasons": r.reasons,
        })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "end_date": end_date,
            "universe": args.universe,
            "screener_params": {
                "min_20d_amplitude": DEFAULT_SCREENER_PARAMS.min_20d_amplitude,
                "min_20d_amount": DEFAULT_SCREENER_PARAMS.min_20d_amount,
            },
            "count": len(pool),
            "candidates": pool,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n=== 候选池生成完毕 ===")
    print(f"入选 {len(pool)} 只，保存到 {out_path}")
    for p in pool[:20]:
        print(f"  {p['code']:12s} 振幅={p['avg_amplitude_20d']*100:5.2f}%  额={p['avg_amount_20d']/1e8:6.2f}亿")
    if len(pool) > 20:
        print(f"  ... 共 {len(pool)} 只（仅显示前20）")

