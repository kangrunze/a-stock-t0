#!/usr/bin/env python3
"""
L5 日内参考指标计算器
======================
基于 1 分钟 K 线滚动计算所有 L5 信号所需的参考指标。
严格因果：任何时刻 t 的指标只用 [0, t] 区间的数据，绝不用未来数据。

提供的指标:
  - cumulative_vwap(bars_up_to_t):  截至当前时刻的累计 VWAP
  - vwap_deviation(price, vwap):    VWAP 偏离度
  - intraday_bollinger(bars, t):    日内动态布林带 MA(20)±2σ
  - opening_range(bars):            开盘区间（9:30-10:00 高低点）
  - intraday_atr(bars, period=14):  日内 ATR（用 1 分钟 K 线的 TR）
  - rsi(bars, period=14):           分钟级 RSI
  - kdj(bars, n=9, m1=3, m2=3):     分钟级 KDJ
  - volume_ratio(bars, lookback=5, baseline=20): 量比

独立性：纯计算模块，不依赖 L1/L2/L3/L4，也不依赖外部数据源。
"""

from __future__ import annotations

from typing import Optional


# ═══════════════════════════════════════════════════════════════
# VWAP — 截至当前时刻的累计成交量加权均价
# ═══════════════════════════════════════════════════════════════
def cumulative_vwap(bars: list[dict]) -> Optional[float]:
    """
    截至最后一根 K 线的累计 VWAP。

    VWAP = Σ(close_i × volume_i) / Σ(volume_i)

    ⚠️ 严格因果：只用传入的 bars 列表，调用方负责只传"截至当前时刻"的数据。
    """
    if not bars:
        return None
    total_amount = sum(b.get("amount", 0) for b in bars)
    total_vol = sum(b.get("volume", 0) for b in bars)
    if total_vol <= 0:
        # 退化为简单均价
        closes = [b.get("close", 0) for b in bars if b.get("close", 0) > 0]
        return sum(closes) / len(closes) if closes else None
    return total_amount / total_vol


def vwap_deviation(price: float, vwap: Optional[float]) -> Optional[float]:
    """
    VWAP 偏离度 = (price - VWAP) / VWAP

    返回小数（0.01 = 1%）。VWAP 为 None 时返回 None。
    """
    if vwap is None or vwap <= 0:
        return None
    return (price - vwap) / vwap


# ═══════════════════════════════════════════════════════════════
# 日内布林带 — MA(20分钟) ± 2×STD(20分钟)
# ═══════════════════════════════════════════════════════════════
def intraday_bollinger(
    bars: list[dict],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    用最后 period 根 1 分钟 K 线的收盘价计算布林带。

    返回 (mid, upper, lower)。数据不足返回 (None, None, None)。

    ⚠️ 严格因果：只用 bars[-period:]，即"截至当前"的最近 period 根。
    """
    if len(bars) < period:
        return None, None, None
    closes = [b["close"] for b in bars[-period:]]
    mid = sum(closes) / period
    variance = sum((c - mid) ** 2 for c in closes) / period
    std = variance ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


# ═══════════════════════════════════════════════════════════════
# 开盘区间 — 9:30-10:00 高低点
# ═══════════════════════════════════════════════════════════════
def opening_range(
    bars: list[dict],
    or_start: str = "09:31",
    or_end: str = "10:00",
) -> tuple[Optional[float], Optional[float]]:
    """
    计算开盘区间（Opening Range）的高低点。

    约定：9:31（第一根分钟K线收盘）至 10:00 之间的最高/最低价。
    （9:30 集合竞价不产生分钟K线，第一根是 9:31）

    返回 (or_high, or_low)。无数据返回 (None, None)。
    """
    if not bars:
        return None, None

    or_high = None
    or_low = None
    for b in bars:
        t = b.get("time", "")
        # 提取 HH:MM 部分
        if " " in t:
            t = t.split(" ")[1]
        if len(t) >= 5:
            t = t[:5]
        if or_start <= t <= or_end:
            h = b.get("high", 0)
            l = b.get("low", 0)
            if h > 0:
                or_high = h if or_high is None else max(or_high, h)
            if l > 0:
                or_low = l if or_low is None else min(or_low, l)
    return or_high, or_low


# ═══════════════════════════════════════════════════════════════
# 日内 ATR — 1 分钟 K 线的真实波幅均值
# ═══════════════════════════════════════════════════════════════
def intraday_atr(bars: list[dict], period: int = 14) -> Optional[float]:
    """
    用 1 分钟 K 线计算 ATR。

    TR_i = max(
        high_i - low_i,
        |high_i - close_{i-1}|,
        |low_i - close_{i-1}|
    )
    ATR = SMA(TR, period)

    ⚠️ 严格因果：用最后 period 根 K 线的 TR，需前一根 close 作为参照。
    """
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        cur = bars[i]
        prev = bars[i - 1]
        prev_close = prev.get("close", 0)
        h = cur.get("high", 0)
        l = cur.get("low", 0)
        if prev_close <= 0:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


# ═══════════════════════════════════════════════════════════════
# RSI — 分钟级相对强弱指标
# ═══════════════════════════════════════════════════════════════
def rsi(bars: list[dict], period: int = 14) -> Optional[float]:
    """
    分钟级 RSI(period)。

    RSI = 100 - 100 / (1 + RS)
    RS = 平均涨幅 / 平均跌幅（SMA）

    ⚠️ 严格因果：用最后 period+1 根 K 线计算 period 个涨跌幅。
    """
    if len(bars) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        cur_close = bars[i]["close"]
        prev_close = bars[i - 1]["close"]
        change = cur_close - prev_close
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


# ═══════════════════════════════════════════════════════════════
# KDJ — 分钟级随机指标
# ═══════════════════════════════════════════════════════════════
def kdj(
    bars: list[dict],
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    分钟级 KDJ(n, m1, m2)。

    RSV = (close - lowest_low(n)) / (highest_high(n) - lowest_low(n)) × 100
    K = SMA(RSV, m1)  （K_new = (m1-1)/m1 × K_old + 1/m1 × RSV）
    D = SMA(K, m2)
    J = 3K - 2D

    返回 (K, D, J)。数据不足返回 (None, None, None)。

    P0-4 修复：对传入的完整 bars 从头递推 RSV 序列，K/D 只在数据开头
    （第一次满足 n 根时）用 50 做种子，之后连续递推，不再每次从 50 重新起跳。
    这样和 ema()/macd() 的因果计算方式一致：调用方传全天累计数据时，
    结果稳定且正确。
    """
    if len(bars) < n:
        return None, None, None

    # P0-4: 不再截断 window，对完整 bars 从第 n-1 根开始逐根计算 RSV
    rsvs = []
    for i in range(n - 1, len(bars)):
        chunk = bars[i - n + 1 : i + 1]
        highs = [b["high"] for b in chunk]
        lows = [b["low"] for b in chunk]
        close = chunk[-1]["close"]
        hh = max(highs)
        ll = min(lows)
        if hh == ll:
            rsv = 50.0
        else:
            rsv = (close - ll) / (hh - ll) * 100
        rsvs.append(rsv)

    if len(rsvs) < m1:
        return None, None, None

    # 递推 K, D（初始 K=D=50，只在序列开头做种子，之后连续递推）
    k = 50.0
    d = 50.0
    alpha_k = 1 / m1
    alpha_d = 1 / m2
    for rsv in rsvs:
        k = (1 - alpha_k) * k + alpha_k * rsv
        d = (1 - alpha_d) * d + alpha_d * k
    j = 3 * k - 2 * d
    return k, d, j


# ═══════════════════════════════════════════════════════════════
# 量比 — 当前 5 分钟成交量 vs 过去 20 分钟均量
# ═══════════════════════════════════════════════════════════════
def volume_ratio(
    bars: list[dict],
    lookback: int = 5,
    baseline: int = 20,
) -> Optional[float]:
    """
    量比 = 最近 lookback 分钟平均成交量 / 之前 baseline 分钟平均成交量

    ⚠️ 严格因果：lookback 用最后 5 根，baseline 用再往前 20 根，两者不重叠。
    """
    if len(bars) < lookback + baseline:
        return None
    recent = bars[-lookback:]
    prior = bars[-(lookback + baseline) : -lookback]
    recent_avg = sum(b["volume"] for b in recent) / lookback
    prior_avg = sum(b["volume"] for b in prior) / baseline
    if prior_avg <= 0:
        return None
    return recent_avg / prior_avg


# ═══════════════════════════════════════════════════════════════
# EMA — 指数移动平均
# ═══════════════════════════════════════════════════════════════
def ema(bars: list[dict], period: int = 20) -> Optional[float]:
    """
    分钟级 EMA(period)，基于收盘价。

    EMA_t = α × close_t + (1-α) × EMA_{t-1}，α = 2/(period+1)
    初始 EMA 用前 period 根的简单均价。

    ⚠️ 严格因果：用截至最后一根的所有 K 线递推。
    """
    if len(bars) < period:
        return None
    closes = [b["close"] for b in bars]
    # 初始 EMA = 前 period 根均价
    ema_val = sum(closes[:period]) / period
    alpha = 2 / (period + 1)
    for c in closes[period:]:
        ema_val = alpha * c + (1 - alpha) * ema_val
    return ema_val


# ═══════════════════════════════════════════════════════════════
# MA — 简单移动平均
# ═══════════════════════════════════════════════════════════════
def ma(bars: list[dict], period: int = 20) -> Optional[float]:
    """
    分钟级 MA(period)，基于收盘价。用最后 period 根。

    ⚠️ 严格因果：只用 bars[-period:]。
    """
    if len(bars) < period:
        return None
    closes = [b["close"] for b in bars[-period:]]
    return sum(closes) / period


# ═══════════════════════════════════════════════════════════════
# MACD — 异同移动平均线
# ═══════════════════════════════════════════════════════════════
def macd(
    bars: list[dict],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    分钟级 MACD。

    返回 (dif, dea, hist)：
      dif = EMA(fast) - EMA(slow)
      dea = EMA(dif, signal)
      hist = 2 × (dif - dea)  （柱状图，A 股惯例 2×）

    数据不足返回 (None, None, None)。
    """
    if len(bars) < slow + signal:
        return None, None, None
    closes = [b["close"] for b in bars]

    def _ema_series(vals: list[float], period: int) -> list[float]:
        if len(vals) < period:
            return []
        out = []
        e = sum(vals[:period]) / period
        out.append(e)
        alpha = 2 / (period + 1)
        for v in vals[period:]:
            e = alpha * v + (1 - alpha) * e
            out.append(e)
        return out

    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    if len(ema_fast) < len(ema_slow):
        return None, None, None
    # 对齐：ema_fast 从 index fast-1 开始，ema_slow 从 slow-1 开始
    offset = slow - fast
    dif_list = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(dif_list) < signal:
        return None, None, None
    dea_list = _ema_series(dif_list, signal)
    if not dea_list:
        return None, None, None
    dif = dif_list[-1]
    dea = dea_list[-1]
    hist = 2 * (dif - dea)
    return dif, dea, hist


# ═══════════════════════════════════════════════════════════════
# DMI — 趋向指标
# ═══════════════════════════════════════════════════════════════
def dmi(
    bars: list[dict],
    period: int = 14,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    分钟级 DMI。

    返回 (pdi, mdi, adx)：
      +DM = max(high_t - high_{t-1}, 0) if > |low_t - low_{t-1}| else 0
      -DM = max(low_{t-1} - low_t, 0) if > (high_t - high_{t-1}) else 0
      TR = max(high-low, |high-prev_close|, |low-prev_close|)
      +DI = EMA(+DM) / EMA(TR) × 100
      -DI = EMA(-DM) / EMA(TR) × 100
      ADX = EMA(|+DI - -DI| / (+DI + -DI) × 100)

    数据不足返回 (None, None, None)。
    """
    if len(bars) < period + 1:
        return None, None, None

    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]
        pdm = up if (up > down and up > 0) else 0
        mdm = down if (down > up and down > 0) else 0
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        plus_dms.append(pdm)
        minus_dms.append(mdm)
        trs.append(tr)

    def _ema(vals: list[float], p: int) -> list[float]:
        if len(vals) < p:
            return []
        out = []
        e = sum(vals[:p]) / p
        out.append(e)
        alpha = 2 / (p + 1)
        for v in vals[p:]:
            e = alpha * v + (1 - alpha) * e
            out.append(e)
        return out

    atr_list = _ema(trs, period)
    pdm_list = _ema(plus_dms, period)
    mdm_list = _ema(minus_dms, period)
    if not atr_list or not pdm_list or not mdm_list:
        return None, None, None

    n = min(len(atr_list), len(pdm_list), len(mdm_list))
    pdi_list, mdi_list, dx_list = [], [], []
    for i in range(-n, 0):
        pdi = (pdm_list[i] / atr_list[i] * 100) if atr_list[i] > 0 else 0
        mdi = (mdm_list[i] / atr_list[i] * 100) if atr_list[i] > 0 else 0
        pdi_list.append(pdi)
        mdi_list.append(mdi)
        denom = pdi + mdi
        dx_list.append(abs(pdi - mdi) / denom * 100 if denom > 0 else 0)

    if len(dx_list) < period:
        return None, None, None
    adx_list = _ema(dx_list, period)
    if not adx_list:
        return None, None, None
    return pdi_list[-1], mdi_list[-1], adx_list[-1]


# ═══════════════════════════════════════════════════════════════
# CCI — 顺势指标
# ═══════════════════════════════════════════════════════════════
def cci(bars: list[dict], period: int = 14) -> Optional[float]:
    """
    分钟级 CCI(period)。

    TP = (high + low + close) / 3
    CCI = (TP - MA(TP, period)) / (0.015 × MeanDev(TP, period))

    超买 > +100，超卖 < -100。

    ⚠️ 严格因果：用最后 period 根。
    """
    if len(bars) < period:
        return None
    tps = [(b["high"] + b["low"] + b["close"]) / 3 for b in bars[-period:]]
    ma_tp = sum(tps) / period
    mean_dev = sum(abs(tp - ma_tp) for tp in tps) / period
    if mean_dev == 0:
        return 0.0
    return (tps[-1] - ma_tp) / (0.015 * mean_dev)


# ═══════════════════════════════════════════════════════════════
# BIAS — 乖离率
# ═══════════════════════════════════════════════════════════════
def bias(bars: list[dict], period: int = 6) -> Optional[float]:
    """
    分钟级 BIAS(period) = (close - MA(period)) / MA(period) × 100。

    返回百分比（如 2.5 = 偏离均线 2.5%）。
    """
    ma_val = ma(bars, period)
    if ma_val is None or ma_val == 0:
        return None
    return (bars[-1]["close"] - ma_val) / ma_val * 100


# ═══════════════════════════════════════════════════════════════
# ROC — 变动率
# ═══════════════════════════════════════════════════════════════
def roc(bars: list[dict], period: int = 12) -> Optional[float]:
    """
    分钟级 ROC(period) = (close_t - close_{t-period}) / close_{t-period} × 100。

    返回百分比。
    """
    if len(bars) < period + 1:
        return None
    cur = bars[-1]["close"]
    prev = bars[-(period + 1)]["close"]
    if prev == 0:
        return None
    return (cur - prev) / prev * 100


# ═══════════════════════════════════════════════════════════════
# OBV — 能量潮
# ═══════════════════════════════════════════════════════════════
def obv(bars: list[dict]) -> Optional[float]:
    """
    分钟级 OBV（累计）。

    规则：
      close_t > close_{t-1} → OBV += volume_t
      close_t < close_{t-1} → OBV -= volume_t
      close_t == close_{t-1} → OBV 不变

    返回截至最后一根的累计 OBV。至少需要 2 根。
    """
    if len(bars) < 2:
        return None
    val = 0.0
    for i in range(1, len(bars)):
        if bars[i]["close"] > bars[i - 1]["close"]:
            val += bars[i]["volume"]
        elif bars[i]["close"] < bars[i - 1]["close"]:
            val -= bars[i]["volume"]
    return val


# ═══════════════════════════════════════════════════════════════
# MFI — 资金流量指标（成交量加权的 RSI）
# ═══════════════════════════════════════════════════════════════
def mfi(bars: list[dict], period: int = 14) -> Optional[float]:
    """
    分钟级 MFI(period)。

    TP = (high + low + close) / 3
    MF = TP × volume
    若 TP 升 → 正 MF，若 TP 降 → 负 MF
    MR = Σ正MF / Σ负MF
    MFI = 100 - 100 / (1 + MR)

    返回 0-100。超买 > 80，超卖 < 20。
    """
    if len(bars) < period + 1:
        return None
    tps = [(b["high"] + b["low"] + b["close"]) / 3 for b in bars]
    pos_mf, neg_mf = 0.0, 0.0
    for i in range(-period, 0):
        mf = tps[i] * bars[i]["volume"]
        if tps[i] > tps[i - 1]:
            pos_mf += mf
        elif tps[i] < tps[i - 1]:
            neg_mf += mf
    if neg_mf == 0:
        return 100.0
    mr = pos_mf / neg_mf
    return 100 - 100 / (1 + mr)


# ═══════════════════════════════════════════════════════════════
# 综合参考指标快照
# ═══════════════════════════════════════════════════════════════
def compute_reference_snapshot(
    bars: list[dict],
    current_price: Optional[float] = None,
    prev_close: Optional[float] = None,
) -> dict:
    """
    一次性计算所有 L5 信号所需的参考指标。

    参数:
      bars: 1 分钟 K 线列表（按时间升序，最后一根是最新的）
      current_price: 当前价（实盘从 quote 取；回测用最后一根 close）
      prev_close: 昨收（用于开盘区间突破判断）

    返回 dict，包含所有指标。任何指标数据不足时值为 None。
    """
    if not bars:
        return {}

    if current_price is None:
        current_price = bars[-1]["close"]

    vwap = cumulative_vwap(bars)
    vwap_dev = vwap_deviation(current_price, vwap)
    bb_mid, bb_upper, bb_lower = intraday_bollinger(bars, period=20, num_std=2.0)
    or_high, or_low = opening_range(bars)
    atr = intraday_atr(bars, period=14)
    rsi_val = rsi(bars, period=14)
    k_val, d_val, j_val = kdj(bars, n=9, m1=3, m2=3)
    vol_ratio = volume_ratio(bars, lookback=5, baseline=20)

    # 最近 5 分钟成交量（用于"缩量冲高"判断）
    recent_5_vol = sum(b["volume"] for b in bars[-5:]) if len(bars) >= 5 else None
    prior_20_vol_avg = (
        sum(b["volume"] for b in bars[-25:-5]) / 20 if len(bars) >= 25 else None
    )

    # 连续缩量且不再创新低（地量企稳信号）
    consecutive_shrink_no_new_low = _consecutive_shrink_no_new_low(bars, lookback=3)

    # ── 广算扩展指标（决策层按需取用，计算成本低）──
    # 均线/位置基准
    ema_val = ema(bars, period=20)
    ma5_val = ma(bars, period=5)
    ma20_val = ma(bars, period=20)
    # 动量超买超卖（与 RSI/KDJ 高度相关，决策层择一即可）
    cci_val = cci(bars, period=14)
    bias_val = bias(bars, period=6)
    roc_val = roc(bars, period=12)
    # 趋势强度（滞后，建议作 5-15 分钟级辅助过滤，不作 1 分钟触发）
    macd_dif, macd_dea, macd_hist = macd(bars, fast=12, slow=26, signal=9)
    pdi_val, mdi_val, adx_val = dmi(bars, period=14)
    # 量能/资金
    obv_val = obv(bars)
    mfi_val = mfi(bars, period=14)

    return {
        "current_price": current_price,
        "prev_close": prev_close,
        "vwap": vwap,
        "vwap_dev": vwap_dev,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "or_high": or_high,
        "or_low": or_low,
        "atr": atr,
        "rsi": rsi_val,
        "kdj_k": k_val,
        "kdj_d": d_val,
        "kdj_j": j_val,
        "volume_ratio": vol_ratio,
        "recent_5_vol": recent_5_vol,
        "prior_20_vol_avg": prior_20_vol_avg,
        "consecutive_shrink_no_new_low": consecutive_shrink_no_new_low,
        # ── 扩展指标 ──
        "ema": ema_val,
        "ma5": ma5_val,
        "ma20": ma20_val,
        "cci": cci_val,
        "bias": bias_val,
        "roc": roc_val,
        "macd_dif": macd_dif,
        "macd_dea": macd_dea,
        "macd_hist": macd_hist,
        "pdi": pdi_val,
        "mdi": mdi_val,
        "adx": adx_val,
        "obv": obv_val,
        "mfi": mfi_val,
        "bars_count": len(bars),
    }


def _consecutive_shrink_no_new_low(bars: list[dict], lookback: int = 3) -> bool:
    """
    检测"地量企稳"：最近 lookback 根 K 线成交量连续递减，且不再创新低。

    用于反T 买入信号的第 3 项规则。
    """
    if len(bars) < lookback + 1:
        return False
    recent = bars[-lookback:]
    # 成交量连续递减
    vols = [b["volume"] for b in recent]
    if not all(vols[i] <= vols[i - 1] for i in range(1, len(vols))):
        return False
    # 不再创新低：最近 lookback 根的最低价 >= 之前 lookback 根的最低价
    prior = bars[-(2 * lookback) : -lookback]
    if not prior:
        return False
    prior_low = min(b["low"] for b in prior)
    recent_low = min(b["low"] for b in recent)
    return recent_low >= prior_low


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 构造 60 根 1 分钟 K 线：前 40 根缓慢上行，后 20 根冲高回落
    # 60 根足以触发所有指标（MACD 需 35 根，DMI ADX 需 ~28 根）
    test_bars = []
    base_price = 10.00
    for i in range(60):
        if i < 40:
            p = base_price + i * 0.01
            vol = 10000 + i * 50
        else:
            p = base_price + 0.40 - (i - 40) * 0.015
            vol = 8000 - (i - 40) * 100
        test_bars.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": max(vol, 100),
            "amount": (p + 0.005) * max(vol, 100),
        })

    snap = compute_reference_snapshot(test_bars, current_price=10.45, prev_close=9.98)
    print("=== 特征计算层快照（60 根 K 线）===")
    for k, v in snap.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")

    # 因果性验证：前 30 根的快照不应等于前 60 根的快照
    snap_30 = compute_reference_snapshot(test_bars[:30])
    assert snap_30["vwap"] != snap["vwap"], "VWAP 因果性破坏"
    assert snap_30["ema"] is not None, "EMA 在 30 根时应有值"
    assert snap_30["macd_dif"] is None, "MACD 在 30 根时应为 None（需 35 根）"
    assert snap["macd_dif"] is not None, "MACD 在 60 根时应有值"
    assert snap["adx"] is not None, "ADX 在 60 根时应有值"
    print("\n[PASS] 因果性与数据充足性验证通过")

    # P0-4: KDJ 连续递推验证 — 打印连续 10 次调用的 K 值，确认平滑递推
    print("\n=== P0-4: KDJ 连续递推验证 ===")
    print("连续 10 次调用 kdj(bars[:k]) 的 K 值（k 从 30 到 39）：")
    k_values = []
    for k_idx in range(30, 40):
        k_val, d_val, j_val = kdj(test_bars[:k_idx])
        k_values.append(k_val)
        print(f"  bars[:{k_idx}] → K={k_val:.4f}  D={d_val:.4f}  J={j_val:.4f}")
    # 验证曲线平滑：相邻 K 值差值应 < 5（如果每次从 50 起跳会有 >10 的跳变）
    max_jump = max(abs(k_values[i+1] - k_values[i]) for i in range(len(k_values)-1))
    print(f"  相邻 K 值最大跳变: {max_jump:.4f}")
    assert max_jump < 5.0, f"KDJ K 值跳变过大 ({max_jump:.2f})，可能仍从 50 起跳"
    print("[PASS] KDJ 连续递推验证通过（无锯齿状跳变）")
