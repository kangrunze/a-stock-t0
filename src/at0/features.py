"""
features 层合并模块
==================

本模块合并了原 scripts/ 下三个特征计算文件，统一作为 a-t0 项目 features 层的入口。
所有特征计算（reference / quote / market）都在此模块中，供 strategy 与 risk 层调用。

包含四类指标：
  - reference 层：VWAP、ATR、RSI、KDJ、MFI、布林带(BB)、MACD、DMI/ADX、EMA/MA、
                  CCI、BIAS、ROC、OBV + 综合快照 compute_reference_snapshot
                  + 市场状态识别 detect_market_regime
  - quote 盘口特征层：从 westock quote 拉取现成盘口/资金字段，派生订单流代理指标
  - market 市场层：跨股票共享的日内市场状态（情绪/题材/门控），作为个股层门控
                  与权重调整依据

合并来源：
  - scripts/intraday_reference.py  → reference 层（含 detect_market_regime）
  - scripts/stock_quote_features.py → quote 盘口特征层
  - scripts/market_layer.py         → market 市场层

注意：
  - 原始函数、类、常量的实现未做任何逻辑修改，仅做了 import 路径调整。
  - westock_client 的调用已改为 `from .data import run_westock, to_westock_symbol`
    （data.py 会合并 westock_client）。
  - l2_theme_reader 作为独立模块保留（scripts/l2_theme_reader.py 或随迁移保留）。
  - 不创建 __init__.py（由调用方单独创建）。
"""

from __future__ import annotations

# ── 顶层 import（合并三个文件的公共依赖）──
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .data import run_westock, to_westock_symbol, get_themes_snapshot


# ═══ features: reference 层（VWAP/ATR/RSI/KDJ/MFI/BB/MACD/DMI/ADX） ═══
# 原文件: scripts/intraday_reference.py
#
# L5 日内参考指标计算器
# ======================
# 基于 1 分钟 K 线滚动计算所有 L5 信号所需的参考指标。
# 严格因果：任何时刻 t 的指标只用 [0, t] 区间的数据，绝不用未来数据。
#
# 提供的指标:
#   - cumulative_vwap(bars_up_to_t):  截至当前时刻的累计 VWAP
#   - vwap_deviation(price, vwap):    VWAP 偏离度
#   - intraday_bollinger(bars, t):    日内动态布林带 MA(20)±2σ
#   - opening_range(bars):            开盘区间（9:30-10:00 高低点）
#   - intraday_atr(bars, period=14):  日内 ATR（用 1 分钟 K 线的 TR）
#   - rsi(bars, period=14):           分钟级 RSI
#   - kdj(bars, n=9, m1=3, m2=3):     分钟级 KDJ
#   - volume_ratio(bars, lookback=5, baseline=20): 量比
#
# 独立性：纯计算模块，不依赖 L1/L2/L3/L4，也不依赖外部数据源。


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
# 市场状态识别（P0-6: regime 归入 features 层）
# ═══════════════════════════════════════════════════════════════
def detect_market_regime(
    snap: dict,
    adx_trend_threshold: float = 25.0,
    adx_extreme_threshold: float = 40.0,
    extreme_vwap_dev_multiplier: float = 2.0,
    min_bars_for_trend: int = 60,
) -> str:
    """
    基于 MACD + DMI/ADX + VWAP偏离度 判定市场状态。

    P0-6 核心函数：regime 作为 features 层输出，strategy 和 risk 都可读，
    避免 risk 反向依赖 strategy。

    返回:
      "trend_up"   : MACD 金叉 + +DI > -DI + ADX ≥ 趋势阈值 → 上升趋势
      "trend_down" : MACD 死叉 + -DI > +DI + ADX ≥ 趋势阈值 → 下降趋势
      "extreme"    : ADX ≥ 极端阈值 且 |VWAP偏离| ≥ extreme_mult × ATR相对值 → 极端趋势
      "range"      : 其余（震荡或数据不足）

    设计要点:
      - extreme 优先判定：即使 MACD/DI 方向不明确，ADX 极高 + 价格远离 VWAP 即为极端
      - trend_up/down 需要三重确认（MACD + DI + ADX），减少假信号
      - 数据不足时返回 "range"（保守不拦截）
      - min_bars_for_trend: ADX/DMI 需 28 根预热 + 至少 32 根数据才可靠，
        不足 60 根时不做趋势判定（避免短样本 ADX 极端值误触发）
    """
    # ADX/DMI 需要足够数据才可靠（28根预热 + 至少32根数据 = 60根）
    bars_count = snap.get("bars_count", 0)
    if bars_count < min_bars_for_trend:
        return "range"

    macd_dif = snap.get("macd_dif")
    macd_dea = snap.get("macd_dea")
    pdi = snap.get("pdi")
    mdi = snap.get("mdi")
    adx = snap.get("adx")
    vwap = snap.get("vwap")
    vwap_dev = snap.get("vwap_dev")
    atr = snap.get("atr")

    if None in (macd_dif, macd_dea, pdi, mdi, adx):
        return "range"

    # 极端趋势：ADX 极高 + 价格远离 VWAP
    if adx >= adx_extreme_threshold:
        if vwap and vwap > 0 and atr and atr > 0 and vwap_dev is not None:
            atr_relative = atr / vwap
            extreme_dev = extreme_vwap_dev_multiplier * atr_relative
            if abs(vwap_dev) >= extreme_dev:
                return "extreme"

    if adx < adx_trend_threshold:
        return "range"

    if macd_dif > macd_dea and pdi > mdi:
        return "trend_up"
    if macd_dif < macd_dea and mdi > pdi:
        return "trend_down"
    return "range"


# ═══════════════════════════════════════════════════════════════
# 自检（reference 层）
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


# ═══ features: quote 盘口特征层 ═══
# 原文件: scripts/stock_quote_features.py
#
# 个股盘口特征层（Stock Quote Features）
# ======================================
# 从 westock quote 拉取现成的盘口/资金字段（无需自己算），与 intraday_reference
# 的特征计算层快照合并，供决策层（t_signal_engine）使用。
#
# westock quote 已返回的字段（实测 sh600000）:
#   - avg_price         = VWAP（现成，可交叉校验自算累计 VWAP）
#   - volume_ratio      = 量比（现成）
#   - turnover_rate     = 换手率（现成）
#   - range_pct         = 振幅（现成）
#   - wb_ratio          = 委比（订单失衡代理）
#   - inner_volume      = 内盘（主动卖，Lee-Ready 近似）
#   - outer_volume      = 外盘（主动买，Lee-Ready 近似）
#   - price_ceiling     = 涨停价（现成，覆盖 prev_close×1.1 估算）
#   - price_floor       = 跌停价（现成）
#   - price / prev_close / open / high / low / volume / amount
#
# 本模块不重复计算 intraday_reference 已有的指标，只补充 westock 现成字段 +
# 派生的订单流代理指标。
#
# 独立性：仅依赖 westock-data CLI（实盘）或调用方注入的 quote dict（回测）。


# ═══════════════════════════════════════════════════════════════
# 盘口特征提取
# ═══════════════════════════════════════════════════════════════
def fetch_quote_features(code: str) -> dict:
    """
    从 westock quote 拉取盘口/资金字段，返回标准化 dict。

    所有数值字段失败时为 None，不抛异常。
    """
    symbol = to_westock_symbol(code)
    raw = run_westock(f"quote {symbol}")
    if isinstance(raw, list) and raw:
        item = raw[0]
    elif isinstance(raw, dict):
        item = raw
    else:
        item = None
    if not item:
        return {}

    def _f(key: str) -> Optional[float]:
        v = item.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key: str) -> Optional[int]:
        v = item.get(key)
        try:
            return int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "code": code,
        "name": item.get("name"),
        # ── 价格（westock 现成，比 prev_close×1.1 估算更准）──
        "current_price": _f("price"),
        "prev_close": _f("prev_close"),
        "open": _f("open"),
        "high": _f("high"),
        "low": _f("low"),
        "limit_up": _f("price_ceiling"),    # 现成涨停价
        "limit_down": _f("price_floor"),    # 现成跌停价
        # ── 量能/资金（westock 现成）──
        "quote_vwap": _f("avg_price"),       # westock 算好的 VWAP
        "quote_volume_ratio": _f("volume_ratio"),
        "turnover_rate": _f("turnover_rate"),
        "amplitude_pct": _f("range_pct"),
        "volume": _i("volume"),
        "amount": _f("amount"),
        # ── 订单流代理（westock 现成）──
        "wb_ratio": _f("wb_ratio"),          # 委比 = 订单失衡代理
        "inner_volume": _i("inner_volume"),  # 内盘 = 主动卖
        "outer_volume": _i("outer_volume"),  # 外盘 = 主动买
    }


def compute_order_flow_proxy(quote_feats: dict) -> dict:
    """
    基于盘口字段派生订单流代理指标。

    返回:
      - active_buy_ratio: 主动买占比 = outer / (inner + outer)，0-1
      - active_sell_ratio: 主动卖占比 = inner / (inner + outer)，0-1
      - order_imbalance_pct: 委比（直接用 wb_ratio，正数偏买、负数偏卖）
      - vwap_dev_from_quote: (price - quote_vwap) / quote_vwap，与自算 vwap_dev 交叉校验
    """
    result = {
        "active_buy_ratio": None,
        "active_sell_ratio": None,
        "order_imbalance_pct": None,
        "vwap_dev_from_quote": None,
    }
    inner = quote_feats.get("inner_volume")
    outer = quote_feats.get("outer_volume")
    if inner is not None and outer is not None and (inner + outer) > 0:
        total = inner + outer
        result["active_buy_ratio"] = outer / total
        result["active_sell_ratio"] = inner / total
    result["order_imbalance_pct"] = quote_feats.get("wb_ratio")
    price = quote_feats.get("current_price")
    qvwap = quote_feats.get("quote_vwap")
    if price is not None and qvwap is not None and qvwap > 0:
        result["vwap_dev_from_quote"] = (price - qvwap) / qvwap
    return result


# ═══════════════════════════════════════════════════════════════
# 与特征计算层快照合并
# ═══════════════════════════════════════════════════════════════
def merge_with_reference_snapshot(
    ref_snap: dict,
    quote_feats: Optional[dict] = None,
) -> dict:
    """
    把盘口特征合并进 intraday_reference 的快照。

    参数:
      ref_snap: compute_reference_snapshot() 返回的 dict
      quote_feats: fetch_quote_features() 返回的 dict。None 时跳过（回测无 quote）

    返回: 合并后的 dict（原 ref_snap 的拷贝 + quote 字段 + 派生指标）。
    保留原 ref_snap 的所有键，quote 字段以 quote_ 前缀或新键名加入，不覆盖。
    """
    merged = dict(ref_snap)
    if not quote_feats:
        merged["_quote_available"] = False
        return merged

    merged["_quote_available"] = True

    # 合并 westock 现成字段（用独立键名，不覆盖 ref_snap 的自算值）
    for k in [
        "quote_vwap", "quote_volume_ratio", "turnover_rate",
        "amplitude_pct", "wb_ratio", "inner_volume", "outer_volume",
    ]:
        if k in quote_feats:
            merged[k] = quote_feats[k]

    # 涨跌停价：优先用 westock 的 price_ceiling/floor（比 prev_close×1.1 准）
    if quote_feats.get("limit_up") is not None:
        merged["limit_up"] = quote_feats["limit_up"]
    if quote_feats.get("limit_down") is not None:
        merged["limit_down"] = quote_feats["limit_down"]

    # 如果 ref_snap 没有 current_price（理论上不会），用 quote 的
    if merged.get("current_price") is None and quote_feats.get("current_price") is not None:
        merged["current_price"] = quote_feats["current_price"]

    # 派生订单流代理
    merged.update(compute_order_flow_proxy(quote_feats))

    return merged


# ═══════════════════════════════════════════════════════════════
# 自检（quote 盘口特征层）
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== 盘口特征层自检（实盘 sh600000）===")
    feats = fetch_quote_features("sh600000")
    if not feats:
        print("  [WARN] westock quote 无返回（非交易时段或数据源不可用）")
    else:
        for k, v in feats.items():
            print(f"  {k}: {v}")

        print("\n=== 订单流代理派生 ===")
        proxy = compute_order_flow_proxy(feats)
        for k, v in proxy.items():
            print(f"  {k}: {v}")

    print("\n=== 合并快照测试（注入模拟 ref_snap）===")
    fake_ref = {
        "current_price": 9.01,
        "vwap": 8.89,
        "vwap_dev": 0.0135,
        "rsi": 55.0,
        "kdj_k": 60.0,
    }
    merged = merge_with_reference_snapshot(fake_ref, feats if feats else None)
    print(f"  _quote_available: {merged.get('_quote_available')}")
    print(f"  ref_snap 键保留: vwap={merged.get('vwap')}, rsi={merged.get('rsi')}")
    if feats:
        print(f"  quote 字段合并: quote_vwap={merged.get('quote_vwap')}, wb_ratio={merged.get('wb_ratio')}")
        print(f"  派生: active_buy_ratio={merged.get('active_buy_ratio')}, "
              f"order_imbalance_pct={merged.get('order_imbalance_pct')}")
        print(f"  涨跌停价: limit_up={merged.get('limit_up')}, limit_down={merged.get('limit_down')}")

    # 回测模式（无 quote）
    print("\n=== 回测模式（无 quote）===")
    merged_no_q = merge_with_reference_snapshot(fake_ref, None)
    print(f"  _quote_available: {merged_no_q.get('_quote_available')}")
    print(f"  ref_snap 键保留: vwap={merged_no_q.get('vwap')}")


# ═══ features: market 市场层（情绪/题材/门控） ═══
# 原文件: scripts/market_layer.py
#
# 市场层（Market Layer）
# ======================
# 跨股票共享的日内市场状态，刷新频率低于个股层（建议 1-3 分钟一次）。
# 作为个股层（Layer A/B/C）的门控与权重调整依据。
#
# 数据源：
#   - 涨跌停数量 / 上涨占比：westock changedist（实时）
#   - 板块热度 / 行业排名：westock sector ranking（实时）
#   - 期指升贴水：westock 暂不支持国内 IF/IH/IC/IM，接口保留返回 None（二期接券商源）
#   - 题材状态（可选 fallback）：软依赖 ashare-sop-engine 的 themes_v17.json
#
# 独立性：不依赖 L1/L2/L3/L4。themes_v17.json 不存在时按默认值处理。
# 仅依赖 westock-data CLI（实盘）或调用方传入的缓存数据（回测）。


# ═══════════════════════════════════════════════════════════════
# 市场快照数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass
class MarketSnapshot:
    """市场层快照。所有字段在数据不可用时为 None / 空列表，不阻塞个股层。"""
    # ── 涨跌停 / 情绪（来自 changedist）──
    up_limit_count: Optional[int] = None       # 涨停家数
    down_limit_count: Optional[int] = None     # 跌停家数
    up_count: Optional[int] = None             # 上涨家数
    down_count: Optional[int] = None           # 下跌家数
    up_ratio: Optional[float] = None           # 上涨占比（0-100）
    up_ratio_comment: Optional[str] = None     # 情绪文案
    total_amount: Optional[float] = None       # 两市成交额（元）

    # ── 板块热度（来自 sector ranking）──
    top_industries: list[dict] = field(default_factory=list)   # 行业涨幅榜
    top_concepts: list[dict] = field(default_factory=list)     # 概念涨幅榜
    top_inflow_sectors: list[dict] = field(default_factory=list)  # 主力资金流入榜

    # ── 期指升贴水（二期，暂未接入）──
    futures_basis: Optional[dict] = None
    # 结构: {"if": {"spread": -0.5, "spread_pct": -0.08}, "ih": ..., "ic": ..., "im": ...}

    # ── 题材状态（软依赖 themes_v17.json，可选）──
    themes_snapshot: Optional[dict] = None

    # ── 元信息 ──
    timestamp: Optional[str] = None            # 快照时间 ISO
    source: str = "westock"                    # 数据源标记

    @property
    def market_sentiment(self) -> str:
        """
        市场情绪分级（供个股层门控）。

        判定逻辑（优先级从高到低）:
          1. 涨停 ≥ 80 且 跌停 ≤ 10 → HOT（赚钱效应强，可正常做T）
          2. 涨停 ≤ 20 或 跌停 ≥ 50 → COLD（赚钱效应弱，减仓信号宽松/加仓信号严格）
          3. 上涨占比 ≤ 30% → COOL（偏冷，谨慎）
          4. 其余 → NEUTRAL
        数据缺失时返回 NEUTRAL（不阻塞）。
        """
        if self.up_limit_count is not None and self.down_limit_count is not None:
            if self.up_limit_count >= 80 and self.down_limit_count <= 10:
                return "HOT"
            if self.up_limit_count <= 20 or self.down_limit_count >= 50:
                return "COLD"
        if self.up_ratio is not None and self.up_ratio <= 30:
            return "COOL"
        return "NEUTRAL"

    @property
    def is_tradable_market(self) -> bool:
        """市场是否适合做T（非 COLD 即可）。COLD 时建议只减不加。"""
        return self.market_sentiment != "COLD"


# ═══════════════════════════════════════════════════════════════
# 数据采集函数
# ═══════════════════════════════════════════════════════════════
def fetch_limit_board_snapshot() -> dict:
    """
    从 westock changedist 拉取涨跌停家数 + 上涨占比。

    返回 dict（字段与 MarketSnapshot 涨跌停部分对应）。失败返回空 dict。
    """
    data = run_westock("changedist")
    if not isinstance(data, dict):
        return {}
    return {
        "up_limit_count": data.get("upLimitCount"),
        "down_limit_count": data.get("downLimitCount"),
        "up_count": data.get("upCount"),
        "down_count": data.get("downCount"),
        "up_ratio": data.get("upRatio"),
        "up_ratio_comment": data.get("upRatioComment"),
        "total_amount": data.get("totalAmount"),
    }


def fetch_sector_ranking_snapshot() -> dict:
    """
    从 westock sector ranking 拉取板块热度。

    返回 dict，包含:
      - top_industries: 行业涨幅榜（list[dict]）
      - top_concepts: 概念涨幅榜（list[dict]）
      - top_inflow_sectors: 主力资金流入榜（list[dict]）
    失败返回空 dict。
    """
    data = run_westock("sector ranking")
    if not isinstance(data, dict):
        return {}
    sections = data.get("sections", [])
    result = {
        "top_industries": [],
        "top_concepts": [],
        "top_inflow_sectors": [],
    }
    # sections 结构: [行业榜, 概念榜, 资金流入榜]（按 westock 当前返回顺序）
    if len(sections) >= 1 and isinstance(sections[0], list):
        result["top_industries"] = sections[0]
    if len(sections) >= 2 and isinstance(sections[1], list):
        result["top_concepts"] = sections[1]
    if len(sections) >= 3 and isinstance(sections[2], list):
        result["top_inflow_sectors"] = sections[2]
    return result


def fetch_futures_basis() -> Optional[dict]:
    """
    期指升贴水（IF/IH/IC/IM 基差）。

    ⚠️ 一期未接入：westock futures 仅支持外盘商品/金融期货 + 港股股指，
    不支持国内 IF/IH/IC/IM。需二期接券商期货行情源（CTP/Choice/Wind）。

    返回 None 表示数据源未接入，不阻塞个股层。
    """
    return None


def read_themes_v17() -> Optional[dict]:
    """
    软依赖读取 ashare-sop-engine 的 L2 题材数据。

    P1-2: 改为委托 l2_theme_reader.get_themes_snapshot()，
    统一文件发现逻辑（不再各自猜测文件名）。

    文件不存在时返回 None（a-t0 独立运行，不报错）。
    """
    return get_themes_snapshot()


# ═══════════════════════════════════════════════════════════════
# 市场层主入口
# ═══════════════════════════════════════════════════════════════
def compute_market_snapshot(
    use_westock: bool = True,
    cached_limit_board: Optional[dict] = None,
    cached_sector_ranking: Optional[dict] = None,
) -> MarketSnapshot:
    """
    汇总市场层快照。

    参数:
      use_westock: 是否实时调用 westock（实盘 True / 回测 False）
      cached_limit_board: 预先拉取的 changedist 数据（回测注入，避免重复调用）
      cached_sector_ranking: 预先拉取的 sector ranking 数据

    返回: MarketSnapshot。任何数据源失败时对应字段为 None/空，不抛异常。
    """
    snap = MarketSnapshot(timestamp=datetime.now().isoformat(timespec="seconds"))

    # 涨跌停 / 情绪
    limit_data = cached_limit_board if cached_limit_board is not None else (
        fetch_limit_board_snapshot() if use_westock else {}
    )
    for k, v in limit_data.items():
        if hasattr(snap, k):
            setattr(snap, k, v)

    # 板块热度
    sector_data = cached_sector_ranking if cached_sector_ranking is not None else (
        fetch_sector_ranking_snapshot() if use_westock else {}
    )
    for k, v in sector_data.items():
        if hasattr(snap, k):
            setattr(snap, k, v)

    # 期指升贴水（二期）
    snap.futures_basis = fetch_futures_basis() if use_westock else None

    # 题材状态（软依赖）
    snap.themes_snapshot = read_themes_v17()

    return snap


# ═══════════════════════════════════════════════════════════════
# 个股层门控接口
# ═══════════════════════════════════════════════════════════════
def market_gate_for_add(market: MarketSnapshot) -> tuple[bool, str]:
    """
    加仓门控：市场层是否允许加仓信号触发。

    返回 (allowed, reason)。
    COLD 市场禁止加仓（只减不加）；其余允许。
    """
    sentiment = market.market_sentiment
    if sentiment == "COLD":
        return False, f"市场情绪 COLD（涨停{market.up_limit_count}/跌停{market.down_limit_count}），禁止加仓"
    return True, f"市场情绪 {sentiment}，加仓放行"


def market_gate_for_reduce(market: MarketSnapshot) -> tuple[bool, str]:
    """
    减仓门控：市场层是否允许减仓信号触发。

    减仓在任何市场情绪下都允许（COLD 时反而更应减仓）。
    """
    return True, f"市场情绪 {market.market_sentiment}，减仓放行"


def adjust_signal_weight(market: MarketSnapshot, direction: str) -> float:
    """
    根据市场情绪调整信号权重（供决策层加权使用）。

    参数:
      direction: "reduce" 或 "add"

    返回: 权重乘数（0.5 ~ 1.2）
      - HOT 市场：加仓权重 ↑（回调买入更安全），减仓权重 ↓（趋势可能延续）
      - COLD 市场：加仓权重 ↓（避免接飞刀），减仓权重 ↑（及时止盈更重要）
      - NEUTRAL/COOL：1.0
    """
    sentiment = market.market_sentiment
    if direction == "add":
        return {"HOT": 1.2, "NEUTRAL": 1.0, "COOL": 0.8, "COLD": 0.5}.get(sentiment, 1.0)
    if direction == "reduce":
        return {"HOT": 0.8, "NEUTRAL": 1.0, "COOL": 1.1, "COLD": 1.2}.get(sentiment, 1.0)
    return 1.0


# ═══════════════════════════════════════════════════════════════
# 自检（market 市场层）
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== 市场层自检（实盘拉取）===")
    snap = compute_market_snapshot(use_westock=True)
    print(f"  时间: {snap.timestamp}")
    print(f"  情绪: {snap.market_sentiment} (tradable={snap.is_tradable_market})")
    print(f"  涨停/跌停: {snap.up_limit_count}/{snap.down_limit_count}")
    print(f"  上涨/下跌: {snap.up_count}/{snap.down_count} ({snap.up_ratio}%)")
    print(f"  情绪文案: {snap.up_ratio_comment}")
    print(f"  两市成交额: {snap.total_amount}")
    print(f"  行业榜前3: {[s.get('name') for s in snap.top_industries[:3]]}")
    print(f"  概念榜前3: {[s.get('name') for s in snap.top_concepts[:3]]}")
    print(f"  资金流入榜前3: {[s.get('name') for s in snap.top_inflow_sectors[:3]]}")
    print(f"  期指升贴水: {snap.futures_basis}")
    print(f"  themes_v17: {'已加载' if snap.themes_snapshot else '未加载（独立模式）'}")

    print("\n=== 门控测试 ===")
    for d in ["add", "reduce"]:
        allowed, reason = (market_gate_for_add if d == "add" else market_gate_for_reduce)(snap)
        weight = adjust_signal_weight(snap, d)
        print(f"  {d}: allowed={allowed}, weight={weight}, reason={reason}")

    print("\n=== 回测模式（注入缓存数据，不调 westock）===")
    cached_snap = compute_market_snapshot(
        use_westock=False,
        cached_limit_board={
            "up_limit_count": 120, "down_limit_count": 5,
            "up_count": 3000, "down_count": 2000, "up_ratio": 60,
            "up_ratio_comment": "市场情绪高涨", "total_amount": 1.5e12,
        },
        cached_sector_ranking={
            "top_industries": [{"name": "半导体", "changePct": "3.5"}],
            "top_concepts": [{"name": "AI芯片", "changePct": "5.2"}],
            "top_inflow_sectors": [{"name": "半导体", "mainNetInflow": "100000"}],
        },
    )
    print(f"  情绪: {cached_snap.market_sentiment} (应为 HOT)")
    print(f"  涨停: {cached_snap.up_limit_count} (应为 120)")
    print(f"  加仓门控: {market_gate_for_add(cached_snap)}")
    print(f"  加仓权重: {adjust_signal_weight(cached_snap, 'add')} (应为 1.2)")

    # COLD 场景
    cold_snap = compute_market_snapshot(
        use_westock=False,
        cached_limit_board={"up_limit_count": 10, "down_limit_count": 80, "up_ratio": 20},
    )
    print(f"\n  COLD 情绪: {cold_snap.market_sentiment} (应为 COLD)")
    print(f"  COLD 加仓门控: {market_gate_for_add(cold_snap)}")
    print(f"  COLD 加仓权重: {adjust_signal_weight(cold_snap, 'add')} (应为 0.5)")
