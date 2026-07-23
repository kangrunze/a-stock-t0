#!/usr/bin/env python3
"""
生成 T-eligible 候选池（沪深300 baostock 筛选），保存到 JSON。
用于第三步批量回测的候选股票池。
"""
import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from at0.screener import screen_hs300_baostock, DEFAULT_SCREENER_PARAMS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "outputs" / "backtest" / "candidate_pool.json"

if __name__ == "__main__":
    end_date = "2026-07-22"  # 与 600000 回测对齐
    print(f"=== 生成候选池 end_date={end_date} max_count=20 ===")
    print(f"    筛选阈值: 振幅≥{DEFAULT_SCREENER_PARAMS.min_20d_amplitude*100}% "
          f"额≥{DEFAULT_SCREENER_PARAMS.min_20d_amount/1e8}亿")

    results = screen_hs300_baostock(end_date=end_date, max_count=20)

    pool = []
    for r in results:
        pool.append({
            "code": r.code,
            "avg_amplitude_20d": round(r.avg_amplitude_20d or 0, 4),
            "avg_amount_20d": round(r.avg_amount_20d or 0, 0),
            "reasons": r.reasons,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "end_date": end_date,
            "screener_params": {
                "min_20d_amplitude": DEFAULT_SCREENER_PARAMS.min_20d_amplitude,
                "min_20d_amount": DEFAULT_SCREENER_PARAMS.min_20d_amount,
            },
            "count": len(pool),
            "candidates": pool,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n=== 候选池生成完毕 ===")
    print(f"入选 {len(pool)} 只，保存到 {OUT}")
    for p in pool:
        print(f"  {p['code']:10s} 振幅={p['avg_amplitude_20d']*100:5.2f}%  额={p['avg_amount_20d']/1e8:6.2f}亿")
