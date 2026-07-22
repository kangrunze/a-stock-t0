#!/usr/bin/env python3
"""
L5 回测样例数据生成器
======================
生成多种日内形态的合成 1 分钟 K 线数据，用于回测调优。

形态类型:
  1. spike_pullback:  冲高回落（正T理想场景）
  2. dip_rally:       下探回升（反T理想场景）
  3. range_bound:     横盘震荡（T 难有作为）
  4. trend_up:        单边上涨（正T 卖出后难买回）
  5. trend_down:      单边下跌（反T 买入后难卖出）
  6. v_shape:         V型反转（先跌后涨）
  7. inverted_v:      倒V型（先涨后跌）

每种形态生成 240 根 1 分钟 K 线（A 股全天交易时间）。
"""

from __future__ import annotations

import math
import random
from typing import Optional


def _gen_time_str(i: int) -> str:
    """根据 K 线索引生成时间字符串（9:31 开始，午休跳过）。"""
    # 上午 9:31-11:30 = 120 根
    if i < 120:
        total_min = 31 + i
        hh = 9 + total_min // 60
        mm = total_min % 60
    else:
        # 下午 13:01-15:00 = 120 根
        total_min = (i - 120) + 1
        hh = 13 + total_min // 60
        mm = total_min % 60
    return f"{hh:02d}:{mm:02d}:00"


def _make_bar(time_str: str, price: float, volume: int, volatility: float = 0.003) -> dict:
    """生成单根 K 线（含微小波动模拟真实行情）。"""
    high = price * (1 + volatility + random.uniform(0, 0.002))
    low = price * (1 - volatility - random.uniform(0, 0.002))
    open_p = price * (1 + random.uniform(-0.001, 0.001))
    close = price * (1 + random.uniform(-0.001, 0.001))
    return {
        "time": time_str,
        "open": round(open_p, 3),
        "high": round(high, 3),
        "low": round(low, 3),
        "close": round(close, 3),
        "volume": max(int(volume), 100),
        "amount": round(close * max(int(volume), 100), 2),
    }


def gen_spike_pullback(
    base_price: float = 10.00,
    spike_pct: float = 0.03,      # 冲高 3%
    pullback_pct: float = 0.015,  # 回落 1.5%
    seed: Optional[int] = None,
) -> list[dict]:
    """冲高回落：上午冲高，中午前回落。正T 理想场景。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    peak = base_price * (1 + spike_pct)
    end_price = peak * (1 - pullback_pct)
    for i in range(240):
        if i < 90:  # 9:31-11:00 冲高
            t = i / 90
            p = base_price + (peak - base_price) * t
            vol = 15000 + i * 50
        elif i < 120:  # 11:01-11:30 高位震荡
            p = peak + random.uniform(-0.01, 0.01)
            vol = 8000
        elif i < 180:  # 13:01-14:00 回落
            t = (i - 120) / 60
            p = peak + (end_price - peak) * t
            vol = 6000
        else:  # 14:01-15:00 低位震荡
            p = end_price + random.uniform(-0.01, 0.01)
            vol = 4000
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_dip_rally(
    base_price: float = 10.00,
    dip_pct: float = 0.03,
    rally_pct: float = 0.015,
    seed: Optional[int] = None,
) -> list[dict]:
    """下探回升：上午下探，下午回升。反T 理想场景。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    trough = base_price * (1 - dip_pct)
    end_price = trough * (1 + rally_pct)
    for i in range(240):
        if i < 90:
            t = i / 90
            p = base_price + (trough - base_price) * t
            vol = 15000 + i * 50
        elif i < 120:
            p = trough + random.uniform(-0.01, 0.01)
            vol = 8000
        elif i < 180:
            t = (i - 120) / 60
            p = trough + (end_price - trough) * t
            vol = 6000
        else:
            p = end_price + random.uniform(-0.01, 0.01)
            vol = 4000
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_range_bound(
    base_price: float = 10.00,
    amplitude: float = 0.01,  # 1% 振幅
    seed: Optional[int] = None,
) -> list[dict]:
    """横盘震荡。T 难有作为。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    for i in range(240):
        # 正弦波 + 噪声
        p = base_price * (1 + amplitude * math.sin(i * 0.1) + random.uniform(-0.002, 0.002))
        vol = 8000 + random.randint(-2000, 2000)
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_trend_up(
    base_price: float = 10.00,
    trend_pct: float = 0.03,
    seed: Optional[int] = None,
) -> list[dict]:
    """单边上涨。正T 卖出后难买回。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    end_price = base_price * (1 + trend_pct)
    for i in range(240):
        t = i / 240
        p = base_price + (end_price - base_price) * t + random.uniform(-0.005, 0.005)
        vol = 10000 + random.randint(-2000, 2000)
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_trend_down(
    base_price: float = 10.00,
    trend_pct: float = 0.03,
    seed: Optional[int] = None,
) -> list[dict]:
    """单边下跌。反T 买入后难卖出。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    end_price = base_price * (1 - trend_pct)
    for i in range(240):
        t = i / 240
        p = base_price + (end_price - base_price) * t + random.uniform(-0.005, 0.005)
        vol = 10000 + random.randint(-2000, 2000)
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_v_shape(
    base_price: float = 10.00,
    dip_pct: float = 0.025,
    seed: Optional[int] = None,
) -> list[dict]:
    """V型反转：上午跌，下午涨回。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    trough = base_price * (1 - dip_pct)
    for i in range(240):
        if i < 120:
            t = i / 120
            p = base_price + (trough - base_price) * t
        else:
            t = (i - 120) / 120
            p = trough + (base_price - trough) * t
        vol = 10000 + random.randint(-2000, 2000)
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


def gen_inverted_v(
    base_price: float = 10.00,
    spike_pct: float = 0.025,
    seed: Optional[int] = None,
) -> list[dict]:
    """倒V型：上午涨，下午跌回。"""
    if seed is not None:
        random.seed(seed)
    bars = []
    peak = base_price * (1 + spike_pct)
    for i in range(240):
        if i < 120:
            t = i / 120
            p = base_price + (peak - base_price) * t
        else:
            t = (i - 120) / 120
            p = peak + (base_price - peak) * t
        vol = 10000 + random.randint(-2000, 2000)
        bars.append(_make_bar(_gen_time_str(i), p, vol))
    return bars


# ═══════════════════════════════════════════════════════════════
# 形态注册表
# ═══════════════════════════════════════════════════════════════
PATTERN_GENERATORS = {
    "spike_pullback": gen_spike_pullback,
    "dip_rally": gen_dip_rally,
    "range_bound": gen_range_bound,
    "trend_up": gen_trend_up,
    "trend_down": gen_trend_down,
    "v_shape": gen_v_shape,
    "inverted_v": gen_inverted_v,
}


def gen_pattern(pattern: str, base_price: float = 10.00, seed: Optional[int] = None) -> list[dict]:
    """按名称生成指定形态的分钟数据。"""
    gen = PATTERN_GENERATORS.get(pattern)
    if gen is None:
        raise ValueError(f"unknown pattern: {pattern}, available: {list(PATTERN_GENERATORS.keys())}")
    return gen(base_price=base_price, seed=seed)


if __name__ == "__main__":
    # 快速自检：生成所有形态并打印摘要
    for pattern in PATTERN_GENERATORS:
        bars = gen_pattern(pattern, seed=42)
        first = bars[0]
        last = bars[-1]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        print(f"{pattern:20s}: {len(bars)} bars, "
              f"open={first['close']:.3f}, close={last['close']:.3f}, "
              f"high={max(highs):.3f}, low={min(lows):.3f}, "
              f"amplitude={(max(highs)-min(lows))/first['close']*100:.2f}%")
