"""
strategy 层模块 — T+0 信号决策层。

包含 P0-5 三层信号引擎（极值层 / 确认层 / 环境层）：
  - 极值层（extreme）：VWAP 偏离 / BB / 开盘区间 + KDJ(主触发) + RSI(辅助) + MFI
  - 确认层（confirm）：缩量企稳 / 量能衰减 / 主动买卖压力
  - 环境层（filter）：涨跌停封板过滤 + 趋势过滤 + 市场层门控

regime（市场趋势状态：trend_up / trend_down / extreme / range）由 features 层
的 detect_market_regime 产出，本层仅消费，避免 risk 反向依赖 strategy。

L5 T+0 信号引擎（P0-5 三层决策结构）
======================================
特征计算 vs 信号决策两层分离：
  - 特征计算层（intraday_reference.py + stock_quote_features.py）：广算所有指标
  - 信号决策层（本模块）：按极值层/确认层/环境层三层组织

极值层（extreme，≥2项触发）: VWAP偏离 / BB / 开盘区间 + KDJ(主触发) + RSI(辅助) + MFI
确认层（confirm，≥1项触发）: 缩量企稳 / 量能衰减 / 主动买卖压力
环境层（filter，必须通过）: 涨跌停封板过滤 + 趋势过滤(P0-6 regime) + 市场层门控

设计原则（避免重蹈"规则堆了一堆但大部分没用"的覆祸）:
  1. 动量超买超卖 5 个指标(RSI/KDJ/CCI/BIAS/ROC)只挑 KDJ 作主触发 + RSI 辅助
  2. MACD/DMI 滞后性高，降级为趋势过滤（判断趋势盘/震荡盘），不作 1 分钟触发
  3. OBV 与 VOL Ratio 方向性重叠，OBV 不进决策层
  4. MFI 属资金超买超卖，归入极值层（P0-5 调整，原在量能层）
  5. 市场层(MarketSnapshot)作为门控：COLD 市场禁加仓
  6. 盘口特征(quote_feats)作为辅助：订单流代理进入确认层

触发规则（P0-5 三层结构，对齐方案 v1.1 Phase 3）:
  - 减仓信号: 极值≥2 + 确认≥1 + 环境通过
      极值层: 项1 VWAP偏离度≥+0.8×ATR / 项2 KDJ.K>80或RSI>70 / 项2b MFI>80
      确认层: 项3 5min缩量 或 主动卖占比>0.55
      环境层: 项4 未涨停封板 + 趋势过滤(extreme否决/trend_up加严)
  - 加仓信号: 极值≥2 + 确认≥1 + 环境通过
      极值层: 项1 VWAP偏离≤-0.8×ATR或跌破OR/BB / 项2 KDJ.K<20或RSI<30 / 项2b MFI<20
      确认层: 项3 连续缩量不创新低 或 主动买占比>0.55
      环境层: 项4 板块未退潮+未跌停 + 趋势过滤(extreme否决/trend_down加严)

独立性：只依赖 features 层的纯计算函数，不依赖 L1/L2/L3/L4。
L1/L2 熔断联动由调用方（t_risk_guard）负责，本引擎只产出原始信号。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .features import compute_reference_snapshot, detect_market_regime
from .features import merge_with_reference_snapshot
from .features import (
    market_gate_for_add,
    market_gate_for_reduce,
    adjust_signal_weight,
)


# ═══════════════════════════════════════════════════════════════
# 信号参数
# ═══════════════════════════════════════════════════════════════
@dataclass
class SignalParams:
    """L5 信号参数。当前值为合成数据调优结果，需用真实分钟数据复验。"""
    # Layer A — 位置层
    vwap_dev_atr_multiplier: float = 0.8    # VWAP 偏离度阈值 = ±0.8 × ATR_intraday（相对值）
    bb_period: int = 20                     # 布林带周期
    bb_std: float = 2.0                     # 布林带标准差倍数
    ema_period: int = 20                    # EMA 周期（位置基准补充）

    # Layer B — 动量层
    rsi_period: int = 14
    rsi_overbought: float = 70.0            # RSI 超买（辅助，方案 v0.2 起始值）
    rsi_oversold: float = 30.0              # RSI 超卖（辅助，方案 v0.2 起始值）
    kdj_n: int = 9
    kdj_m1: int = 3
    kdj_m2: int = 3
    kdj_overbought: float = 80.0            # KDJ K 超买（主触发）
    kdj_oversold: float = 20.0              # KDJ K 超卖（主触发）

    # Layer B — 趋势过滤（不作触发，仅调整动量层权重）
    trend_filter_enabled: bool = True       # 是否启用 MACD/DMI 趋势过滤
    adx_trend_threshold: float = 25.0       # ADX > 此值视为趋势盘
    # P0-6: 极端趋势过滤参数
    adx_extreme_threshold: float = 40.0     # ADX ≥ 此值且价格远离VWAP时为极端趋势
    extreme_vwap_dev_multiplier: float = 2.0  # |VWAP偏离| ≥ 此值 × ATR相对值 时为极端偏离

    # Layer C — 量能层
    vol_ratio_lookback: int = 5             # 量比当前窗口
    vol_ratio_baseline: int = 20            # 量比基准窗口
    shrink_threshold: float = 0.8           # 缩量阈值：当前5min量 < 过去20min均量 × 0.8
    mfi_overbought: float = 80.0            # MFI 超买（资金超买）
    mfi_oversold: float = 20.0              # MFI 超卖（资金超卖）

    # Layer C — 订单流代理（来自 quote_feats，需 _quote_available=True）
    active_sell_pressure: float = 0.55      # 主动卖占比 > 此值视为卖压（减仓辅助）
    active_buy_pressure: float = 0.55       # 主动买占比 > 此值视为买盘（加仓辅助）

    # 触发阈值（P0-5: 三层结构）
    min_rules_to_trigger: int = 3           # 总分阈值（向后兼容；实际触发还受 extreme_min/confirm_min 约束）
    extreme_min: int = 2                    # 极值层至少满足项数
    confirm_min: int = 1                    # 确认层至少满足项数

    # 方案A（开仓深度上限）：偏离过深时禁止开仓，从源头减少无法回归的超时腿
    # 诊断显示 86.7% 超时腿因 open_too_deep：开仓在深偏离区，1小时窗口内回归不到平仓阈值
    # 2026-07-24 诊断：2.0% 深度的开仓止损均亏-873（3.9%不利移动），深偏离往往是真趋势而非回归
    # 收紧到 1.2%：只开 0.8%~1.2% 甜区，避免在趋势行情中被当成逆势开仓
    open_max_vwap_dev: float = 0.012

    # ── 趋势跟随参数（2026-07-24 策略转向）──
    # 数据诊断：5min VWAP偏离后65%概率延续，29%概率回归
    # 均值回归策略在A股5min级别不可行，转为顺趋势方向开仓
    # 开仓：VWAP偏离方向 + ADX趋势确认 + KDJ同向 + 量能放大
    # 平仓：趋势反转（VWAP穿越/ADX回落/KDJ反向），止损纯风控兜底（分离止盈止损）
    tf_adx_threshold: float = 30.0        # ADX > 此值确认趋势存在（开仓条件，提高以降频）
    tf_vol_ratio_min: float = 1.5         # 量比 > 此值确认量能放大（开仓条件，提高以降频）
    tf_trend_reverse_adx: float = 28.0    # ADX<此值视为趋势减弱（平仓条件，分离止盈止损后放宽让平仓信号能触发）
    tf_vwap_cross_threshold: float = 0.002  # |VWAP偏离| < 此值视为VWAP穿越（平仓条件，0.2%）
    tf_kdj_reverse_bars: int = 2          # KDJ连续N根反向确认（平仓条件，防单根噪声）

    # Layer P — 平仓层（is_for_pairing=True 时使用）
    # 趋势跟随平仓：趋势反转信号（ADX回落/VWAP穿越/KDJ反向）
    pairing_vwap_dev_threshold: float = 0.008  # 平仓阈值下限（0.8%），open_vwap_dev 缺失时退化为此值
    pairing_no_new_extreme_bars: int = 2       # 连续 N 根 K 线不创新极值（轻量方向确认，防单根噪声误触发）
    min_capture_spread_for_pairing: float = 0.006   # 平仓动态阈值的成本锚点（0.6%，与 RiskParams.min_capture_spread 对齐）
    pairing_max_regression_ratio: float = 0.5       # 动态阈值上限比例（开仓深度的50%）


DEFAULT_PARAMS = SignalParams()


# ═══════════════════════════════════════════════════════════════
# 信号结果
# ═══════════════════════════════════════════════════════════════
@dataclass
class TSignal:
    """单个 T 信号。"""
    direction: str                          # "reduce" (减仓) 或 "add" (加仓/买回)
    rules_fired: list[str] = field(default_factory=list)  # 触发的规则描述
    rules_score: int = 0                    # 触发项数（总分 = extreme + confirm + filter）
    price: float = 0.0                      # 信号触发时的价格
    snapshot: dict = field(default_factory=dict)  # 完整指标快照（用于审计）
    layer_scores: dict = field(default_factory=dict)  # 三层各自触发项数 {extreme: n, confirm: n, filter: n}
    market_gate: Optional[dict] = None      # 市场层门控结果 {allowed, reason, weight}
    trend_context: Optional[str] = None     # 趋势过滤判定 "trend_up"/"trend_down"/"range"/"extreme"
    trigger_threshold: int = 3              # 触发阈值（从 params.min_rules_to_trigger 传入）
    # P0-5: 三层结构得分与门槛
    extreme_score: int = 0                  # 极值层触发项数
    confirm_score: int = 0                  # 确认层触发项数
    filter_passed: bool = True              # 环境层过滤是否通过
    extreme_min: int = 2                    # 极值层最少项数
    confirm_min: int = 1                    # 确认层最少项数
    # Layer P: 平仓层（is_for_pairing=True 时使用）
    is_pairing: bool = False                 # 是否为平仓评估（vs 新开仓评估）
    pairing_near_vwap: bool = False          # 平仓：价格已回归 VWAP 附近
    pairing_direction_confirmed: bool = False  # 平仓：轻量方向确认（不创新极值）

    @property
    def triggered(self) -> bool:
        """
        触发条件：
        - 平仓分支（is_pairing=True）：环境层通过 + 价格回归VWAP附近 + 轻量方向确认
          （不走三层 confluence，因为均值回归的平仓定义是"价格回归均值"而非"对向出现极端"）
        - 开仓分支（is_pairing=False）：环境层通过 + 极值≥extreme_min + 确认≥confirm_min + 总分≥阈值
        """
        if not self.filter_passed:
            return False
        if self.is_pairing:
            # 平仓分支：距离判断 + 方向确认（不走三层 confluence）
            return self.pairing_near_vwap and self.pairing_direction_confirmed
        # 开仓分支：三层 confluence
        if self.extreme_score < self.extreme_min:
            return False
        if self.confirm_score < self.confirm_min:
            return False
        return self.rules_score >= self.trigger_threshold

    @property
    def t_type(self) -> str:
        """对应的 T 操作类型说明。"""
        if self.direction == "reduce":
            return "正T-卖出 / 反T-买回"
        return "反T-买入 / 正T-买回"


# ═══════════════════════════════════════════════════════════════
# 平仓层辅助：轻量方向确认（连续 N 根 K 线不创新极值）
# ═══════════════════════════════════════════════════════════════
def _no_new_extreme_recently(bars: list[dict], direction: str, lookback: int) -> bool:
    """
    轻量方向确认：最近 lookback 根 K 线不再创新极值。

    - direction="reduce"（卖出平 buy 仓）：最近 lookback 根 K 线的 high 都 ≤ 之前的高点
      （价格不再创新高，上行已停滞，适合获利了结卖出）
    - direction="add"（买回平 sell 仓）：最近 lookback 根 K 线的 low 都 ≥ 之前的低点
      （价格不再创新低，下行已停滞，适合获利了结买回）

    防止单根 K 线的噪声误触发平仓信号。
    """
    if len(bars) < lookback + 1:
        return False
    prior = bars[:-lookback]
    recent = bars[-lookback:]
    if direction == "reduce":
        prior_high = max(b["high"] for b in prior) if prior else 0
        return all(b["high"] <= prior_high for b in recent)
    else:  # add
        prior_low = min(b["low"] for b in prior) if prior else float("inf")
        return all(b["low"] >= prior_low for b in recent)


# ═══════════════════════════════════════════════════════════════
# 价格趋势确认（趋势跟随平仓的方向确认）
# ═══════════════════════════════════════════════════════════════
def _price_trend_confirmed(bars: list[dict], direction: str, lookback: int) -> bool:
    """
    检查最近 lookback 根 K 线收盘价是否多数同方向（趋势跟随平仓的方向确认）。

    - direction="up":  买入腿平仓（卖出），需价格上行确认趋势反转向上
    - direction="down": 卖出腿平仓（买回），需价格下行确认趋势反转向下

    60% 以上同方向即确认（避免单根噪声，比"全部同方向"更实用）。
    """
    if len(bars) < lookback + 1:
        return False
    recent = bars[-(lookback + 1):]
    up_count = sum(1 for i in range(1, len(recent))
                   if recent[i]["close"] > recent[i - 1]["close"])
    if direction == "up":
        return up_count >= lookback * 0.6
    else:  # down
        return (lookback - up_count) >= lookback * 0.6


# ═══════════════════════════════════════════════════════════════
# 平仓动态阈值计算（方案C1修正版）
# ═══════════════════════════════════════════════════════════════
def _compute_pairing_threshold(
    open_vwap_dev: Optional[float],
    params: SignalParams,
    holding_ratio: float = 0.0,
) -> float:
    """
    计算平仓动态阈值（方案C1修正版 + 方案B时间衰减）。

    基础阈值 = max(floor, min(|open_dev| - cost, |open_dev| × max_regression_ratio))

    - floor (pairing_vwap_dev_threshold, 0.8%): 下限保护，避免阈值过小
    - |open_dev| - cost: 距离锚定成本，价格回归到"仍能覆盖成本"的位置即平仓
    - |open_dev| × max_regression_ratio (0.7): 上限保护，防止 open_dev 过大时
      阈值过松导致过早平仓（修正 C1 原始公式在 open_dev>4% 时的缺陷）

    方案B 时间衰减（holding_ratio > 0 时生效）：
      holding_ratio = holding_bars / max_holding_bars（0.0~1.0）
      - >0.8（接近超时）：阈值 ×0.5，让腿更容易平仓，避免被动 expired
      - >0.5（过半未平）：阈值 ×0.7，适度降低门槛促成平仓
      - ≤0.5：不衰减
    目的：把"被动 expired 大亏"转化为"主动平仓 小亏/小赚"。

    open_vwap_dev 为 None 时（未传入开仓信息，向后兼容），退化为固定 floor（仍受衰减影响）。

    回放验证依据: outputs/backtest/diagnose_formula_replay.json
      - C1 触发 34 条 / +10,838 元（vs C2 触发 10 条 / +11,011 元）
      - C1 触发数是 C2 的 3.4 倍，统计更稳；C2 盈亏集中度高不可信
      - C1 原始公式在 open_dev=5.17% 时阈值 4.57% 过松，单条亏损 -3818
        加 70% 上限后阈值降至 3.62%，可避免此类过早平仓
    """
    floor = params.pairing_vwap_dev_threshold
    if open_vwap_dev is None:
        base = floor
    else:
        open_dev_abs = abs(open_vwap_dev)
        cost_anchored = open_dev_abs - params.min_capture_spread_for_pairing
        max_allowed = open_dev_abs * params.pairing_max_regression_ratio
        base = max(floor, min(cost_anchored, max_allowed))

    # 方案B：时间衰减（接近超时时降低平仓门槛）
    if holding_ratio > 0.8:
        return base * 0.5
    elif holding_ratio > 0.5:
        return base * 0.7
    return base


# ═══════════════════════════════════════════════════════════════
# 趋势过滤判定（P0-6: 委托给 features 层的 detect_market_regime）
# ═══════════════════════════════════════════════════════════════
def _judge_trend_context(snap: dict, params: SignalParams, frequency: str = "1min") -> str:
    """
    判定当前趋势上下文（委托给 features 层的 detect_market_regime）。

    P0-6 整改：regime 检测逻辑归入 features 层（intraday_reference.py），
    strategy 和 risk 都可读，避免 risk 反向依赖 strategy。

    P0-6 修复（2026-07-24）：min_bars_for_trend 按频率自适应。
    旧实现硬编码 60 根，5min 数据一天 48 根永远 < 60，detect_market_regime
    直接返回 "range"，趋势过滤（trend_up/down/extreme）完全失效，逆势均值回归
    在强趋势中照常触发。改为：1min=60根（1小时），5min=12根（1小时，等价时长）。

    返回:
      "trend_up"   : 上升趋势盘
      "trend_down" : 下降趋势盘
      "extreme"    : 极端趋势（ADX极高 + 价格远离VWAP）
      "range"      : 震荡盘或数据不足

    用途（P0-6 趋势过滤）:
      - trend_up 时，减仓信号需更高确认（趋势可能继续上行，卖出逆势）
      - trend_down 时，加仓信号需更高确认（趋势可能继续下探，买入逆势）
      - extreme 时，暂停所有均值回归（硬否决）
      - range 时，不调整
    """
    if not params.trend_filter_enabled:
        return "range"

    # P0-6: 按频率自适应 min_bars_for_trend（等价 1 小时 K 线数）
    if frequency == "5min":
        effective_min_bars = 12   # 5min × 12 = 60 分钟
    else:
        effective_min_bars = 60   # 1min × 60 = 60 分钟

    return detect_market_regime(
        snap,
        adx_trend_threshold=params.adx_trend_threshold,
        adx_extreme_threshold=params.adx_extreme_threshold,
        extreme_vwap_dev_multiplier=params.extreme_vwap_dev_multiplier,
        min_bars_for_trend=effective_min_bars,
    )


# ═══════════════════════════════════════════════════════════════
# 减仓信号评估（正T-卖出 / 反T-买回）
# ═══════════════════════════════════════════════════════════════
def evaluate_reduce_signal(
    bars: list[dict],
    current_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    is_limit_up_locked: bool = False,
    params: Optional[SignalParams] = None,
    quote_feats: Optional[dict] = None,
    is_for_pairing: bool = False,
    open_vwap_dev: Optional[float] = None,
    frequency: str = "1min",
    holding_ratio: float = 0.0,
) -> TSignal:
    """
    减仓信号评估（趋势跟随：下跌趋势中卖出开仓 / 趋势反转时平买仓）。

    数据诊断：5min VWAP偏离后65%概率延续，29%概率回归。
    均值回归策略在A股5min级别不可行，转为顺趋势方向开仓。

    - is_for_pairing=False（默认，新开仓）：下跌趋势跟随卖出
      极值层（满分3）：VWAP偏离为负 + ADX确认趋势 + KDJ死叉
      确认层（满分1）：量能放大
      环境层：未涨停封板（可成交）
    - is_for_pairing=True（为配对已有 buy 仓位）：趋势反转平仓
      趋势反转（偏离转正 / 穿越VWAP / ADX回落）+ 价格上行确认 + 未涨停封板

    趋势过滤（_judge_trend_context）保留但仅作信息记录，不再用于逆势加严
    （趋势跟随本身即顺趋势，无需对逆势开仓加严）。

    open_vwap_dev: 开仓时刻的 vwap_dev（由调用方从 TradeLeg 传入）。
                  趋势跟随不再使用动态平仓阈值（_compute_pairing_threshold），
                  保留参数以兼容函数签名。
    """
    params = params or DEFAULT_PARAMS
    snap = compute_reference_snapshot(bars, current_price, prev_close)
    if not snap:
        return TSignal(direction="reduce")

    snap = merge_with_reference_snapshot(snap, quote_feats)

    fired: list[str] = []
    extreme_score = 0  # 极值层计分（0-3）
    confirm_score = 0  # 确认层计分（0-1）
    price = snap["current_price"]
    vwap_dev = snap.get("vwap_dev")
    adx = snap.get("adx")
    k_val = snap.get("kdj_k")
    d_val = snap.get("kdj_d")
    vol_ratio = snap.get("volume_ratio")

    # 趋势过滤：仅作信息记录，趋势跟随不据此加严或否决
    trend_ctx = _judge_trend_context(snap, params, frequency)

    # 格式化辅助（防 None）
    vwap_dev_str = f"{vwap_dev*100:+.2f}%" if vwap_dev is not None else "N/A"
    adx_str = f"{adx:.1f}" if adx is not None else "N/A"

    # ═══ 平仓分支（is_for_pairing=True）：趋势反转 + 价格上行确认 ═══
    if is_for_pairing:
        filter_passed = not is_limit_up_locked
        # 趋势反转判断：偏离转正 / 穿越VWAP / ADX回落（任一即视为下跌趋势结束）
        trend_reversed = False
        if vwap_dev is not None and vwap_dev > 0:
            trend_reversed = True
        elif vwap_dev is not None and abs(vwap_dev) < params.tf_vwap_cross_threshold:
            trend_reversed = True
        elif adx is not None and adx < params.tf_trend_reverse_adx:
            trend_reversed = True
        near_vwap = trend_reversed  # 复用字段语义：趋势反转 ≈ 价格回到反转点
        # 方向确认：价格上行确认趋势反转向上（买入腿平仓卖出，需价格上行获利了结）
        dir_confirmed = _price_trend_confirmed(bars, "up", params.tf_kdj_reverse_bars)
        fired_pairing = []
        if near_vwap:
            fired_pairing.append(f"[平仓-趋势反转] vwap_dev={vwap_dev_str} adx={adx_str} 下跌趋势结束（偏离转正/穿越VWAP/ADX回落）")
        else:
            fired_pairing.append(f"[平仓-趋势反转] vwap_dev={vwap_dev_str} adx={adx_str} 下跌趋势延续（未反转）")
        if dir_confirmed:
            fired_pairing.append(f"[平仓-方向确认] 最近{params.tf_kdj_reverse_bars}根K线价格多数上行（反转向上）")
        else:
            fired_pairing.append(f"[平仓-方向确认] 最近{params.tf_kdj_reverse_bars}根K线价格未上行（仍在下探）")
        if filter_passed:
            fired_pairing.append("[环境4] 未涨停封板（可成交）")
        else:
            fired_pairing.append("[环境4] 涨停封板（硬否决）")
        return TSignal(
            direction="reduce",
            rules_fired=fired_pairing,
            rules_score=2 if (near_vwap and dir_confirmed and filter_passed) else 0,
            price=price,
            snapshot=snap,
            layer_scores={"pairing_distance": 1 if near_vwap else 0,
                          "pairing_direction": 1 if dir_confirmed else 0,
                          "filter": 1 if filter_passed else 0},
            trend_context=trend_ctx,
            trigger_threshold=2,  # 平仓分支走自己的触发逻辑，不走 trigger_threshold
            extreme_score=0,
            confirm_score=0,
            filter_passed=filter_passed,
            extreme_min=params.extreme_min,
            confirm_min=params.confirm_min,
            is_pairing=True,
            pairing_near_vwap=near_vwap,
            pairing_direction_confirmed=dir_confirmed,
        )

    # ── 极值层 项1: VWAP偏离为负（价格在VWAP下方，空头）──
    if vwap_dev is not None and vwap_dev < 0:
        fired.append(f"[极值1] VWAP偏离 {vwap_dev_str} < 0（价格在VWAP下方，空头）")
        extreme_score += 1

    # ── 极值层 项2: ADX确认趋势存在 ──
    if adx is not None and adx > params.tf_adx_threshold:
        fired.append(f"[极值2] ADX={adx_str} > {params.tf_adx_threshold}（趋势确认）")
        extreme_score += 1

    # ── 极值层 项3: KDJ死叉（K < D，动量向下）──
    if k_val is not None and d_val is not None and k_val < d_val:
        fired.append(f"[极值3] KDJ.K={k_val:.1f} < D={d_val:.1f}（死叉，动量向下）")
        extreme_score += 1

    # ── 确认层 项4: 量能放大 ──
    if vol_ratio is not None and vol_ratio > params.tf_vol_ratio_min:
        fired.append(f"[确认4] 量比 {vol_ratio:.2f} > {params.tf_vol_ratio_min}（量能放大）")
        confirm_score += 1

    # ── 环境层 项5: 涨停封板过滤（卖出需可成交）──
    filter_passed = not is_limit_up_locked
    if filter_passed:
        fired.append("[环境5] 未涨停封板（可成交）")
    else:
        fired.append("[环境5] 涨停封板（硬否决）")

    # 趋势上下文（信息记录，不调整阈值）
    fired.append(f"[趋势] {trend_ctx}（信息记录，趋势跟随不调整阈值）")

    # 计分：总分 = 极值 + 确认 + 过滤
    total_score = extreme_score + confirm_score + (1 if filter_passed else 0)
    if not filter_passed:
        total_score = 0

    # 趋势跟随：不额外加严，trigger_threshold = min_rules_to_trigger
    trigger_threshold = params.min_rules_to_trigger

    return TSignal(
        direction="reduce",
        rules_fired=fired,
        rules_score=total_score,
        price=price,
        snapshot=snap,
        layer_scores={"extreme": extreme_score, "confirm": confirm_score, "filter": 1 if filter_passed else 0},
        trend_context=trend_ctx,
        trigger_threshold=trigger_threshold,
        extreme_score=extreme_score,
        confirm_score=confirm_score,
        filter_passed=filter_passed,
        extreme_min=params.extreme_min,
        confirm_min=params.confirm_min,
    )


# ═══════════════════════════════════════════════════════════════
# 加仓/买回信号评估（反T-买入 / 正T-买回）
# ═══════════════════════════════════════════════════════════════
def evaluate_add_signal(
    bars: list[dict],
    current_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    is_limit_down_locked: bool = False,
    theme_retreated: bool = False,
    params: Optional[SignalParams] = None,
    quote_feats: Optional[dict] = None,
    is_for_pairing: bool = False,
    open_vwap_dev: Optional[float] = None,
    frequency: str = "1min",
    holding_ratio: float = 0.0,
) -> TSignal:
    """
    加仓/买回信号评估（趋势跟随：上涨趋势中买入开仓 / 趋势反转时平卖仓）。

    数据诊断：5min VWAP偏离后65%概率延续，29%概率回归。
    均值回归策略在A股5min级别不可行，转为顺趋势方向开仓。

    - is_for_pairing=False（默认，新开仓）：上涨趋势跟随买入
      极值层（满分3）：VWAP偏离为正 + ADX确认趋势 + KDJ金叉
      确认层（满分1）：量能放大
      环境层：板块未退潮 + 未跌停封板
    - is_for_pairing=True（为配对已有 sell 仓位）：趋势反转平仓
      趋势反转（偏离转负 / 穿越VWAP / ADX回落）+ 价格下行确认 + 未跌停封板

    趋势过滤（_judge_trend_context）保留但仅作信息记录，不再用于逆势加严。

    open_vwap_dev: 开仓时刻的 vwap_dev（由调用方从 TradeLeg 传入）。
                  趋势跟随不再使用动态平仓阈值（_compute_pairing_threshold），
                  保留参数以兼容函数签名。
    """
    params = params or DEFAULT_PARAMS
    snap = compute_reference_snapshot(bars, current_price, prev_close)
    if not snap:
        return TSignal(direction="add")

    snap = merge_with_reference_snapshot(snap, quote_feats)

    fired: list[str] = []
    extreme_score = 0  # 极值层计分（0-3）
    confirm_score = 0  # 确认层计分（0-1）
    price = snap["current_price"]
    vwap_dev = snap.get("vwap_dev")
    adx = snap.get("adx")
    k_val = snap.get("kdj_k")
    d_val = snap.get("kdj_d")
    vol_ratio = snap.get("volume_ratio")

    # 趋势过滤：仅作信息记录，趋势跟随不据此加严或否决
    trend_ctx = _judge_trend_context(snap, params, frequency)

    # 格式化辅助（防 None）
    vwap_dev_str = f"{vwap_dev*100:+.2f}%" if vwap_dev is not None else "N/A"
    adx_str = f"{adx:.1f}" if adx is not None else "N/A"

    # ═══ 平仓分支（is_for_pairing=True）：趋势反转 + 价格下行确认 ═══
    if is_for_pairing:
        filter_passed = (not theme_retreated) and (not is_limit_down_locked)
        # 趋势反转判断：偏离转负 / 穿越VWAP / ADX回落（任一即视为上涨趋势结束）
        trend_reversed = False
        if vwap_dev is not None and vwap_dev < 0:
            trend_reversed = True
        elif vwap_dev is not None and abs(vwap_dev) < params.tf_vwap_cross_threshold:
            trend_reversed = True
        elif adx is not None and adx < params.tf_trend_reverse_adx:
            trend_reversed = True
        near_vwap = trend_reversed  # 复用字段语义：趋势反转 ≈ 价格回到反转点
        # 方向确认：价格下行确认趋势反转向下（卖出腿平仓买回，需价格下行获利了结）
        dir_confirmed = _price_trend_confirmed(bars, "down", params.tf_kdj_reverse_bars)
        fired_pairing = []
        if near_vwap:
            fired_pairing.append(f"[平仓-趋势反转] vwap_dev={vwap_dev_str} adx={adx_str} 上涨趋势结束（偏离转负/穿越VWAP/ADX回落）")
        else:
            fired_pairing.append(f"[平仓-趋势反转] vwap_dev={vwap_dev_str} adx={adx_str} 上涨趋势延续（未反转）")
        if dir_confirmed:
            fired_pairing.append(f"[平仓-方向确认] 最近{params.tf_kdj_reverse_bars}根K线价格多数下行（反转向下）")
        else:
            fired_pairing.append(f"[平仓-方向确认] 最近{params.tf_kdj_reverse_bars}根K线价格未下行（仍在冲高）")
        if filter_passed:
            fired_pairing.append("[环境4] 板块未退潮且未跌停封板（可成交）")
        else:
            reason = []
            if theme_retreated:
                reason.append("板块退潮")
            if is_limit_down_locked:
                reason.append("跌停封板")
            fired_pairing.append(f"[环境4] {'+'.join(reason)}（硬否决）")
        return TSignal(
            direction="add",
            rules_fired=fired_pairing,
            rules_score=2 if (near_vwap and dir_confirmed and filter_passed) else 0,
            price=price,
            snapshot=snap,
            layer_scores={"pairing_distance": 1 if near_vwap else 0,
                          "pairing_direction": 1 if dir_confirmed else 0,
                          "filter": 1 if filter_passed else 0},
            trend_context=trend_ctx,
            trigger_threshold=2,  # 平仓分支走自己的触发逻辑，不走 trigger_threshold
            extreme_score=0,
            confirm_score=0,
            filter_passed=filter_passed,
            extreme_min=params.extreme_min,
            confirm_min=params.confirm_min,
            is_pairing=True,
            pairing_near_vwap=near_vwap,
            pairing_direction_confirmed=dir_confirmed,
        )

    # ── 极值层 项1: VWAP偏离为正（价格在VWAP上方，多头）──
    if vwap_dev is not None and vwap_dev > 0:
        fired.append(f"[极值1] VWAP偏离 {vwap_dev_str} > 0（价格在VWAP上方，多头）")
        extreme_score += 1

    # ── 极值层 项2: ADX确认趋势存在 ──
    if adx is not None and adx > params.tf_adx_threshold:
        fired.append(f"[极值2] ADX={adx_str} > {params.tf_adx_threshold}（趋势确认）")
        extreme_score += 1

    # ── 极值层 项3: KDJ金叉（K > D，动量向上）──
    if k_val is not None and d_val is not None and k_val > d_val:
        fired.append(f"[极值3] KDJ.K={k_val:.1f} > D={d_val:.1f}（金叉，动量向上）")
        extreme_score += 1

    # ── 确认层 项4: 量能放大 ──
    if vol_ratio is not None and vol_ratio > params.tf_vol_ratio_min:
        fired.append(f"[确认4] 量比 {vol_ratio:.2f} > {params.tf_vol_ratio_min}（量能放大）")
        confirm_score += 1

    # ── 环境层 项5: 板块未退潮 + 未跌停封板 ──
    filter_passed = (not theme_retreated) and (not is_limit_down_locked)
    if filter_passed:
        fired.append("[环境5] 板块未退潮且未跌停封板（可成交）")
    else:
        reason = []
        if theme_retreated:
            reason.append("板块退潮")
        if is_limit_down_locked:
            reason.append("跌停封板")
        fired.append(f"[环境5] {'+'.join(reason)}（硬否决）")

    # 趋势上下文（信息记录，不调整阈值）
    fired.append(f"[趋势] {trend_ctx}（信息记录，趋势跟随不调整阈值）")

    # 计分：总分 = 极值 + 确认 + 过滤
    total_score = extreme_score + confirm_score + (1 if filter_passed else 0)
    if not filter_passed:
        total_score = 0

    # 趋势跟随：不额外加严，trigger_threshold = min_rules_to_trigger
    trigger_threshold = params.min_rules_to_trigger

    return TSignal(
        direction="add",
        rules_fired=fired,
        rules_score=total_score,
        price=price,
        snapshot=snap,
        layer_scores={"extreme": extreme_score, "confirm": confirm_score, "filter": 1 if filter_passed else 0},
        trend_context=trend_ctx,
        trigger_threshold=trigger_threshold,
        extreme_score=extreme_score,
        confirm_score=confirm_score,
        filter_passed=filter_passed,
        extreme_min=params.extreme_min,
        confirm_min=params.confirm_min,
    )


# ═══════════════════════════════════════════════════════════════
# 市场情绪权重 → 动态触发阈值（P1-1: 接入 adjust_signal_weight）
# ═══════════════════════════════════════════════════════════════
def _apply_weight_to_threshold(base_threshold: int, weight: float) -> int:
    """
    根据市场情绪权重动态调整触发阈值。

    - weight > 1.0 → 放宽触发（降低阈值，用 floor 取整）
    - weight < 1.0 → 收紧触发（提高阈值，用 ceil 取整）
    - weight = 1.0 → 不变

    结果钳制在 [2, 4] 范围内（最低2项即可，最高需全部4项）。
    """
    if weight > 1.0:
        adjusted = math.floor(base_threshold / weight)
    elif weight < 1.0:
        adjusted = math.ceil(base_threshold / weight)
    else:
        adjusted = base_threshold
    return max(2, min(4, adjusted))


# ═══════════════════════════════════════════════════════════════
# 综合评估
# ═══════════════════════════════════════════════════════════════
def evaluate_all_signals(
    bars: list[dict],
    current_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    is_limit_up_locked: bool = False,
    is_limit_down_locked: bool = False,
    theme_retreated: bool = False,
    params: Optional[SignalParams] = None,
    market=None,
    quote_feats: Optional[dict] = None,
) -> dict:
    """
    综合评估减仓/加仓信号，返回两者及推荐方向。

    参数:
      market: MarketSnapshot（可选），用于市场层门控。COLD 市场禁加仓。
      quote_feats: 盘口特征 dict（可选），来自 stock_quote_features.fetch_quote_features。
                   注入后 Layer C 可使用订单流代理指标。

    返回:
    {
        "reduce_signal": TSignal,
        "add_signal": TSignal,
        "recommendation": "reduce" | "add" | "none" | "conflict",
        "snapshot": dict,
        "market_gate_add": dict,   # 加仓门控结果（仅 market 非空时）
        "market_gate_reduce": dict,# 减仓门控结果
    }
    """
    params = params or DEFAULT_PARAMS
    reduce_sig = evaluate_reduce_signal(
        bars, current_price, prev_close, is_limit_up_locked, params, quote_feats
    )
    add_sig = evaluate_add_signal(
        bars, current_price, prev_close, is_limit_down_locked, theme_retreated, params, quote_feats
    )

    # 市场层门控
    market_gate_add = None
    market_gate_reduce = None
    if market is not None:
        market_gate_add = {
            "allowed": market_gate_for_add(market)[0],
            "reason": market_gate_for_add(market)[1],
        }
        market_gate_reduce = {
            "allowed": market_gate_for_reduce(market)[0],
            "reason": market_gate_for_reduce(market)[1],
        }
        # P1-1: 市场情绪加权 → 动态调整触发阈值
        # 权重 > 1.0 放宽触发（降低阈值），权重 < 1.0 收紧触发（提高阈值）
        reduce_weight = adjust_signal_weight(market, "reduce")
        add_weight = adjust_signal_weight(market, "add")
        # P0-7 整改（2026-07-24）：基于"已加严的 trigger_threshold"叠加市场权重，
        # 而非从 base（params.min_rules_to_trigger）重算。
        # 旧实现用 base 重算会覆盖 evaluate_reduce/add_signal 内部按 trend_ctx
        # 已经 +1 的趋势加严结果（见 strategy.py:418-419 / 604-606），
        # 导致"上升趋势 + COLD 市场"双重作用下趋势保护丢失。
        reduce_sig.trigger_threshold = _apply_weight_to_threshold(
            reduce_sig.trigger_threshold, reduce_weight
        )
        add_sig.trigger_threshold = _apply_weight_to_threshold(
            add_sig.trigger_threshold, add_weight
        )
        market_gate_add["weight"] = add_weight
        market_gate_add["adjusted_threshold"] = add_sig.trigger_threshold
        market_gate_reduce["weight"] = reduce_weight
        market_gate_reduce["adjusted_threshold"] = reduce_sig.trigger_threshold
        # 把门控结果写入信号
        reduce_sig.market_gate = market_gate_reduce
        add_sig.market_gate = market_gate_add

    recommendation = "none"
    reduce_triggered = reduce_sig.triggered
    add_triggered = add_sig.triggered

    # 市场层门控覆盖：COLD 市场强制加仓不触发
    if market_gate_add is not None and not market_gate_add["allowed"]:
        add_triggered = False

    if reduce_triggered and add_triggered:
        recommendation = "conflict"
    elif reduce_triggered:
        recommendation = "reduce"
    elif add_triggered:
        recommendation = "add"

    return {
        "reduce_signal": reduce_sig,
        "add_signal": add_sig,
        "recommendation": recommendation,
        "snapshot": reduce_sig.snapshot or add_sig.snapshot,
        "market_gate_add": market_gate_add,
        "market_gate_reduce": market_gate_reduce,
    }


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 测试 1：构造"冲高缩量"场景 → 应触发减仓信号
    print("=== Test 1: 冲高缩量（应触发 reduce）===")
    test_bars = []
    base_price = 10.00
    for i in range(40):
        if i < 20:
            p = base_price + i * 0.01
            vol = 10000 + i * 100
        else:
            p = base_price + 0.20 + (i - 20) * 0.015  # 冲高
            vol = 8000 - (i - 20) * 400  # 缩量
        test_bars.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": vol, "amount": (p + 0.005) * vol,
        })

    result = evaluate_all_signals(test_bars, current_price=10.35, prev_close=10.00)
    print(f"  recommendation: {result['recommendation']}")
    print(f"  reduce_score: {result['reduce_signal'].rules_score}/4 — {result['reduce_signal'].rules_fired}")
    print(f"  reduce_layers: {result['reduce_signal'].layer_scores}")
    print(f"  trend_context: {result['reduce_signal'].trend_context}")
    print(f"  add_score: {result['add_signal'].rules_score}/4 — {result['add_signal'].rules_fired}")

    # 测试 2：构造"下探地量企稳"场景 → 应触发加仓信号
    print("\n=== Test 2: 下探地量企稳（应触发 add）===")
    test_bars2 = []
    base_price = 10.00
    for i in range(40):
        if i < 20:
            p = base_price - i * 0.015  # 下探
            vol = 10000 + i * 200  # 放量下跌
        else:
            p = base_price - 0.30 + (i - 20) * 0.002  # 企稳微升
            vol = 4000 - (i - 20) * 200  # 持续缩量
        test_bars2.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.001, "volume": max(vol, 100), "amount": (p + 0.001) * max(vol, 100),
        })

    result2 = evaluate_all_signals(test_bars2, current_price=9.70, prev_close=10.00)
    print(f"  recommendation: {result2['recommendation']}")
    print(f"  add_score: {result2['add_signal'].rules_score}/4 — {result2['add_signal'].rules_fired}")
    print(f"  add_layers: {result2['add_signal'].layer_scores}")
    print(f"  trend_context: {result2['add_signal'].trend_context}")
    print(f"  reduce_score: {result2['reduce_signal'].rules_score}/4 — {result2['reduce_signal'].rules_fired}")

    # 测试 3：横盘无信号
    print("\n=== Test 3: 横盘（应为 none）===")
    test_bars3 = []
    for i in range(40):
        p = 10.00 + (i % 5 - 2) * 0.001
        test_bars3.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.01, "low": p - 0.01,
            "close": p, "volume": 10000, "amount": p * 10000,
        })
    result3 = evaluate_all_signals(test_bars3, current_price=10.00, prev_close=10.00)
    print(f"  recommendation: {result3['recommendation']}")
    print(f"  reduce_score: {result3['reduce_signal'].rules_score}, add_score: {result3['add_signal'].rules_score}")

    # 测试 4：市场层门控（COLD 禁加仓）
    print("\n=== Test 4: 市场层门控（COLD 禁加仓）===")
    try:
        from market_layer import MarketSnapshot
        cold_market = MarketSnapshot(
            up_limit_count=10, down_limit_count=80, up_ratio=20,
            timestamp="2026-07-22T10:00:00",
        )
        # 用下探企稳数据 + COLD 市场 → add 应被门控拦截
        result4 = evaluate_all_signals(test_bars2, current_price=9.70, prev_close=10.00, market=cold_market)
        print(f"  recommendation: {result4['recommendation']} (COLD 市场应为 none 或 reduce)")
        print(f"  market_gate_add: {result4['market_gate_add']}")
        print(f"  add_triggered(门控前): {result4['add_signal'].triggered}")
    except ImportError:
        print("  [SKIP] market_layer 不可用")
