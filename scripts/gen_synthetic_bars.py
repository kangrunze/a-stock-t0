#!/usr/bin/env python3
"""
合成分钟数据生成器（离线验证用）
==============================
在沙箱无实时行情的环境下，生成确定性的合成分钟 K 线，写入
data/multi_day_cache/{code}_{start}_{end}.json，使 `python -m at0.cli backtest`
可在无网络时跑通引擎、验证 FIFO/T+1/regime/频率自适应等逻辑。

数据特征：
  - 5 分钟线，每天 48 根（09:30~11:30 + 13:00~15:00）
  - 围绕缓慢漂移的日内 VWAP 做均值回归（让 T 策略能触发配对）
  - 固定随机种子 → 结果可复现（Phase C 回归测试依赖确定性）
  - prev_close 跨日链式传递，符合真实连续交易

用法:
  python scripts/gen_synthetic_bars.py --code 600000 --start 2026-07-17 --end 2026-07-24
  python scripts/gen_synthetic_bars.py --code 600000 --start 2026-07-17 --end 2026-07-24 --seed 42 --base 11.50
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _trading_days(start: str, end: str) -> list[str]:
    d = datetime.strptime(start, "%Y-%m-%d")
    last = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= last:
        if d.weekday() < 5:  # 周一到周五
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _bar_times_5min() -> list[str]:
    """生成 5 分钟线时间点（含午休跳过）。"""
    times = []
    # 上午 09:30 ~ 11:30
    h, m = 9, 30
    while (h, m) <= (11, 30):
        times.append(f"{h:02d}:{m:02d}:00")
        m += 5
        if m >= 60:
            m = 0
            h += 1
    # 下午 13:00 ~ 15:00
    h, m = 13, 0
    while (h, m) <= (15, 0):
        times.append(f"{h:02d}:{m:02d}:00")
        m += 5
        if m >= 60:
            m = 0
            h += 1
    return times


def _gen_day(prev_close: float, rng: random.Random, vol_base: float,
             wave_amp: float, waves: float, drift: float) -> list[dict]:
    """生成单日 5min 棒，沿日内波浪路径 + 噪声，确保双边 T 信号触发并可 FIFO 配对。

    wave_amp: 日内波浪振幅（如 0.02 = ±2%）
    waves:    一天内完整波浪数（1~2，制造先跌后涨/先涨后跌）
    drift:    全天净漂移（如 +0.01 = 收涨 1%）
    """
    times = _bar_times_5min()
    n = len(times)
    bars = []
    price = prev_close
    for i, t in enumerate(times):
        frac = i / (n - 1)
        # 波浪目标价：正弦波 + 线性漂移
        wave = wave_amp * math.sin(2 * math.pi * waves * frac - math.pi / 2)
        target = prev_close * (1 + wave + drift * frac)
        # 向 target 靠拢 + 噪声
        pull = (target - price) * 0.5
        noise = rng.gauss(0, prev_close * 0.0009)
        step = pull + noise
        close = max(0.01, price + step)
        open_p = price
        intra = abs(rng.gauss(0, prev_close * 0.0010))
        high = max(open_p, close) + intra
        low = max(0.01, min(open_p, close) - intra)
        # 成交量：U 型（开盘/收盘大）+ 波动大时放量
        u = 1.0 + 1.2 * (abs(i - (n - 1) / 2) / ((n - 1) / 2)) ** 1.5
        vol_mult = 1.0 + abs(step) / (prev_close * 0.003)
        volume = int(vol_base * u * vol_mult * rng.uniform(0.7, 1.3))
        amount = volume * (open_p + close) / 2.0
        bars.append({
            "time": t,
            "open": round(open_p, 3),
            "high": round(high, 3),
            "low": round(low, 3),
            "close": round(close, 3),
            "volume": volume,
            "amount": round(amount, 2),
        })
        price = close
    return bars


def generate(code: str, start: str, end: str, seed: int, base: float) -> dict:
    rng = random.Random(seed)
    days = _trading_days(start, end)
    daily_bars: dict[str, list[dict]] = {}
    daily_prev_closes: dict[str, float] = {}
    daily_meta: dict[str, dict] = {}
    prev_close = base
    vol_base = 200_000  # 每 5min 基础成交量（股）
    for idx, d in enumerate(days):
        # 交替波浪方向：偶数日先跌后涨，奇数日先涨后跌；振幅/漂移微扰
        wave_amp = 0.022 + rng.uniform(-0.004, 0.006)
        waves = 1.0 + rng.uniform(0, 0.5)
        drift = rng.uniform(-0.012, 0.014)
        if idx % 2 == 1:
            drift = -drift  # 反向漂移制造多空交替
        bars = _gen_day(prev_close, rng, vol_base, wave_amp, waves, drift)
        daily_bars[d] = bars
        daily_prev_closes[d] = round(prev_close, 3)
        daily_meta[d] = {
            "source": "synthetic",
            "frequency": "5min",
            "bars_count": len(bars),
        }
        prev_close = bars[-1]["close"]
    return {
        "code": code,
        "start_date": start,
        "end_date": end,
        "daily_bars": daily_bars,
        "daily_prev_closes": daily_prev_closes,
        "daily_meta": daily_meta,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None, help="单只代码（与 --codes 二选一）")
    ap.add_argument("--codes", default=None, help="逗号分隔多只代码")
    ap.add_argument("--start", default="2026-07-17")
    ap.add_argument("--end", default="2026-07-24")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--base", type=float, default=11.50)
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        codes = [args.code or "600000"]

    cache_dir = PROJECT_ROOT / "data" / "multi_day_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for code in codes:
        # 按代码派生确定性 seed（用稳定哈希，不受 PYTHONHASHSEED 影响）
        code_seed = args.seed + (int.from_bytes(code.encode(), "big") % 997)
        payload = generate(code, args.start, args.end, code_seed, args.base)
        path = cache_dir / f"{code}_{args.start}_{args.end}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        n_days = len(payload["daily_bars"])
        n_bars = sum(len(v) for v in payload["daily_bars"].values())
        print(f"[gen] {code} {args.start}~{args.end} -> {path.name} "
              f"({n_days}日/{n_bars}棒, seed={code_seed})")


if __name__ == "__main__":
    main()
