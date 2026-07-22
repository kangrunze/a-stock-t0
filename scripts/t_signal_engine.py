#!/usr/bin/env python3
"""
L5 T+0 信号引擎（三层决策层）
================================
特征计算 vs 信号决策两层分离：
  - 特征计算层（intraday_reference.py + stock_quote_features.py）：广算所有指标
  - 信号决策层（本模块）：只用回测/IC 验证过的子集，按 Layer A/B/C 三层组织

Layer A 位置层: VWAP 偏离度 / 日内布林带 / 开盘区间 / EMA
Layer B 动量层: KDJ(主触发) + RSI(辅助) + MACD/DMI(趋势过滤，不作触发)
Layer C 量能层: VOL Ratio / 缩量企稳 / MFI + 订单流代理(wb_ratio/内外盘)

设计原则（避免重蹈"规则堆了一堆但大部分没用"的覆辙）:
  1. 动量超买超卖 5 个指标(RSI/KDJ/CCI/BIAS/ROC)只挑 KDJ 作主触发 + RSI 辅助
  2. MACD/DMI 滞后性高，降级为趋势过滤（判断趋势盘/震荡盘），不作 1 分钟触发
  3. OBV 与 VOL Ratio 方向性重叠，OBV 不进决策层
  4. 市场层(MarketSnapshot)作为门控：COLD 市场禁加仓
  5. 盘口特征(quote_feats)作为辅助：订单流代理进入 Layer C

触发规则（对齐方案 v0.2 第四节 4.2）:
  - 减仓信号: 4 项中 ≥3 项满足（3 内容项 + 1 过滤项"未涨停"）
      项1 VWAP偏离度 ≥ +0.8×ATR_intraday
      项2 KDJ.K > 80 或 RSI(14) > 70（OR 合并为一项）
      项3 当前5分钟量能 < 过去20分钟均量×0.8
      项4 未处于涨停封板状态（过滤项，不满足时硬否决）
  - 加仓信号: 4 项中 ≥3 项满足（3 内容项 + 1 过滤项"未跌停且题材未退潮"）
      项1 VWAP偏离度 ≤ -0.8×ATR 或 跌破开盘区间下轨 或 跌破布林带下轨（OR）
      项2 KDJ.K < 20 或 RSI(14) < 30（OR）
      项3 连续缩量且不再创新低
      项4 所属板块未退潮 且 未跌停封板（过滤项，不满足时硬否决）

独立性：只依赖 intraday_reference.py 的纯计算函数，不依赖 L1/L2/L3/L4。
L1/L2 熔断联动由调用方（t_risk_guard）负责，本引擎只产出原始信号。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from intraday_reference import compute_reference_snapshot
from stock_quote_features import merge_with_reference_snapshot


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

    # Layer C — 量能层
    vol_ratio_lookback: int = 5             # 量比当前窗口
    vol_ratio_baseline: int = 20            # 量比基准窗口
    shrink_threshold: float = 0.8           # 缩量阈值：当前5min量 < 过去20min均量 × 0.8
    mfi_overbought: float = 80.0            # MFI 超买（资金超买）
    mfi_oversold: float = 20.0              # MFI 超卖（资金超卖）

    # Layer C — 订单流代理（来自 quote_feats，需 _quote_available=True）
    active_sell_pressure: float = 0.55      # 主动卖占比 > 此值视为卖压（减仓辅助）
    active_buy_pressure: float = 0.55       # 主动买占比 > 此值视为买盘（加仓辅助）

    # 触发阈值
    min_rules_to_trigger: int = 3           # 4 项中至少 3 项满足（3 内容项 + 1 过滤项）


DEFAULT_PARAMS = SignalParams()


# ═══════════════════════════════════════════════════════════════
# 信号结果
# ═══════════════════════════════════════════════════════════════
@dataclass
class TSignal:
    """单个 T 信号。"""
    direction: str                          # "reduce" (减仓) 或 "add" (加仓/买回)
    rules_fired: list[str] = field(default_factory=list)  # 触发的规则描述
    rules_score: int = 0                    # 触发项数
    price: float = 0.0                      # 信号触发时的价格
    snapshot: dict = field(default_factory=dict)  # 完整指标快照（用于审计）
    layer_scores: dict = field(default_factory=dict)  # 三层各自触发项数 {content: n, filter: n}
    market_gate: Optional[dict] = None      # 市场层门控结果 {allowed, reason, weight}
    trend_context: Optional[str] = None     # 趋势过滤判定 "trend_up"/"trend_down"/"range"
    trigger_threshold: int = 3              # 触发阈值（从 params.min_rules_to_trigger 传入）

    @property
    def triggered(self) -> bool:
        return self.rules_score >= self.trigger_threshold

    @property
    def t_type(self) -> str:
        """对应的 T 操作类型说明。"""
        if self.direction == "reduce":
            return "正T-卖出 / 反T-买回"
        return "反T-买入 / 正T-买回"


# ═══════════════════════════════════════════════════════════════
# 趋势过滤判定（MACD/DMI，不作触发，仅判断趋势盘/震荡盘）
# ═══════════════════════════════════════════════════════════════
def _judge_trend_context(snap: dict, params: SignalParams) -> str:
    """
    基于 MACD + DMI 判定当前趋势上下文。

    返回:
      "trend_up"   : MACD 金叉(dif>dea) 且 +DI > -DI 且 ADX > 阈值 → 上升趋势盘
      "trend_down" : MACD 死叉(dif<dea) 且 -DI > +DI 且 ADX > 阈值 → 下降趋势盘
      "range"      : 其余（震荡盘或数据不足）

    用途:
      - trend_up 时，减仓信号更坚决（趋势可能见顶回落），加仓信号更谨慎（不接飞刀）
      - trend_down 时，加仓信号更谨慎（趋势可能继续下探），减仓信号更坚决
      - range 时，两层都不加权
    """
    if not params.trend_filter_enabled:
        return "range"

    macd_dif = snap.get("macd_dif")
    macd_dea = snap.get("macd_dea")
    pdi = snap.get("pdi")
    mdi = snap.get("mdi")
    adx = snap.get("adx")

    if None in (macd_dif, macd_dea, pdi, mdi, adx):
        return "range"

    if adx < params.adx_trend_threshold:
        return "range"

    if macd_dif > macd_dea and pdi > mdi:
        return "trend_up"
    if macd_dif < macd_dea and mdi > pdi:
        return "trend_down"
    return "range"


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
) -> TSignal:
    """
    减仓信号评估。4 项中 ≥3 项触发（对齐方案 v0.2 4.2）:

    内容项（3项）:
      项1 VWAP偏离度 ≥ +0.8×ATR_intraday（相对值）
      项2 KDJ.K > 80 或 RSI(14) > 70（OR 合并为一项）
      项3 当前5分钟量能 < 过去20分钟均量×0.8（缩量冲高）
    过滤项（1项，不满足时硬否决）:
      项4 未处于涨停封板状态

    盘口特征辅助：MFI>80 或 主动卖占比>0.55 可作为项3的替代量能信号。
    """
    params = params or DEFAULT_PARAMS
    snap = compute_reference_snapshot(bars, current_price, prev_close)
    if not snap:
        return TSignal(direction="reduce")

    snap = merge_with_reference_snapshot(snap, quote_feats)

    fired: list[str] = []
    content_score = 0  # 内容项计分（0-3）
    price = snap["current_price"]
    vwap = snap.get("vwap")
    vwap_dev = snap.get("vwap_dev")
    atr = snap.get("atr")
    rsi_val = snap.get("rsi")
    k_val = snap.get("kdj_k")
    mfi_val = snap.get("mfi")
    recent_5_vol = snap.get("recent_5_vol")
    prior_20_vol_avg = snap.get("prior_20_vol_avg")

    trend_ctx = _judge_trend_context(snap, params)

    # ── 项1: VWAP 偏离度 ≥ +0.8 × ATR_intraday（相对值）──
    if vwap and vwap > 0 and atr and atr > 0:
        atr_relative = atr / vwap
        threshold = params.vwap_dev_atr_multiplier * atr_relative
        if vwap_dev is not None and vwap_dev >= threshold:
            fired.append(f"[项1] VWAP偏离 {vwap_dev*100:+.2f}% ≥ +{params.vwap_dev_atr_multiplier}×ATR({threshold*100:.2f}%)")
            content_score += 1

    # ── 项2: KDJ.K > 80 或 RSI(14) > 70（OR 合并）──
    momentum_fired = False
    if k_val is not None and k_val > params.kdj_overbought:
        fired.append(f"[项2] KDJ.K={k_val:.1f} > {params.kdj_overbought}")
        momentum_fired = True
    elif rsi_val is not None and rsi_val > params.rsi_overbought:
        fired.append(f"[项2] RSI={rsi_val:.1f} > {params.rsi_overbought}")
        momentum_fired = True
    if momentum_fired:
        content_score += 1

    # ── 项3: 当前5分钟量能 < 过去20分钟均量×0.8（缩量冲高）──
    # 盘口辅助：MFI>80（资金超买）或 主动卖占比>0.55（卖压）可替代
    vol_fired = False
    if recent_5_vol is not None and prior_20_vol_avg is not None and prior_20_vol_avg > 0:
        recent_avg = recent_5_vol / params.vol_ratio_lookback
        shrink_line = prior_20_vol_avg * params.shrink_threshold
        if recent_avg < shrink_line:
            fired.append(f"[项3] 5min均量 {recent_avg:.0f} < 20min×{params.shrink_threshold}({shrink_line:.0f})")
            vol_fired = True
    if not vol_fired:
        # 盘口辅助：MFI 超买 或 主动卖压
        if mfi_val is not None and mfi_val > params.mfi_overbought:
            fired.append(f"[项3] MFI={mfi_val:.1f} > {params.mfi_overbought}（资金超买）")
            vol_fired = True
        else:
            active_sell = snap.get("active_sell_ratio")
            if active_sell is not None and active_sell > params.active_sell_pressure:
                fired.append(f"[项3] 主动卖占比 {active_sell:.2%} > {params.active_sell_pressure:.0%}（卖压）")
                vol_fired = True
    if vol_fired:
        content_score += 1

    # ── 项4: 未涨停封板（过滤项，硬否决）──
    filter_passed = not is_limit_up_locked
    if filter_passed:
        fired.append("[项4] 未涨停封板（可成交）")
    else:
        fired.append("[项4] 涨停封板（硬否决）")

    # 计分：4项中≥3项 = 内容分 + 过滤分(1) ≥ 3
    # 涨停封板时硬否决（content_score 归零）
    total_score = content_score + (1 if filter_passed else 0)
    if not filter_passed:
        total_score = 0

    return TSignal(
        direction="reduce",
        rules_fired=fired,
        rules_score=total_score,
        price=price,
        snapshot=snap,
        layer_scores={"content": content_score, "filter": 1 if filter_passed else 0},
        trend_context=trend_ctx,
        trigger_threshold=params.min_rules_to_trigger,
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
) -> TSignal:
    """
    加仓/买回信号评估。4 项中 ≥3 项触发（对齐方案 v0.2 4.2）:

    内容项（3项）:
      项1 VWAP偏离度 ≤ -0.8×ATR 或 跌破开盘区间下轨 或 跌破布林带下轨（OR）
      项2 KDJ.K < 20 或 RSI(14) < 30（OR 合并为一项）
      项3 连续缩量且不再创新低（地量企稳）
    过滤项（1项，不满足时硬否决）:
      项4 所属板块未退潮 且 未跌停封板

    盘口特征辅助：MFI<20 或 主动买占比>0.55 可作为项3的替代量能信号。
    """
    params = params or DEFAULT_PARAMS
    snap = compute_reference_snapshot(bars, current_price, prev_close)
    if not snap:
        return TSignal(direction="add")

    snap = merge_with_reference_snapshot(snap, quote_feats)

    fired: list[str] = []
    content_score = 0
    price = snap["current_price"]
    vwap = snap.get("vwap")
    vwap_dev = snap.get("vwap_dev")
    atr = snap.get("atr")
    rsi_val = snap.get("rsi")
    k_val = snap.get("kdj_k")
    mfi_val = snap.get("mfi")
    or_low = snap.get("or_low")
    bb_lower = snap.get("bb_lower")
    consecutive_shrink = snap.get("consecutive_shrink_no_new_low")

    trend_ctx = _judge_trend_context(snap, params)

    # ── 项1: VWAP偏离度 ≤ -0.8×ATR 或 跌破开盘区间下轨 或 跌破布林带下轨（OR）──
    pos_fired = False
    if vwap and vwap > 0 and atr and atr > 0 and vwap_dev is not None:
        atr_relative = atr / vwap
        threshold = -params.vwap_dev_atr_multiplier * atr_relative
        if vwap_dev <= threshold:
            fired.append(f"[项1] VWAP偏离 {vwap_dev*100:+.2f}% ≤ {threshold*100:.2f}%")
            pos_fired = True
    if not pos_fired and or_low is not None and price < or_low:
        fired.append(f"[项1] 价格 {price} < 开盘区间下轨 {or_low}")
        pos_fired = True
    if not pos_fired and bb_lower is not None and price < bb_lower:
        fired.append(f"[项1] 价格 {price} < 布林带下轨 {bb_lower:.2f}")
        pos_fired = True
    if pos_fired:
        content_score += 1

    # ── 项2: KDJ.K < 20 或 RSI(14) < 30（OR 合并）──
    momentum_fired = False
    if k_val is not None and k_val < params.kdj_oversold:
        fired.append(f"[项2] KDJ.K={k_val:.1f} < {params.kdj_oversold}")
        momentum_fired = True
    elif rsi_val is not None and rsi_val < params.rsi_oversold:
        fired.append(f"[项2] RSI={rsi_val:.1f} < {params.rsi_oversold}")
        momentum_fired = True
    if momentum_fired:
        content_score += 1

    # ── 项3: 连续缩量且不再创新低（地量企稳）──
    # 盘口辅助：MFI<20（资金超卖）或 主动买占比>0.55（买盘）可替代
    vol_fired = False
    if consecutive_shrink:
        fired.append("[项3] 连续缩量且不再创新低（地量企稳）")
        vol_fired = True
    if not vol_fired:
        if mfi_val is not None and mfi_val < params.mfi_oversold:
            fired.append(f"[项3] MFI={mfi_val:.1f} < {params.mfi_oversold}（资金超卖）")
            vol_fired = True
        else:
            active_buy = snap.get("active_buy_ratio")
            if active_buy is not None and active_buy > params.active_buy_pressure:
                fired.append(f"[项3] 主动买占比 {active_buy:.2%} > {params.active_buy_pressure:.0%}（买盘）")
                vol_fired = True
    if vol_fired:
        content_score += 1

    # ── 项4: 所属板块未退潮 且 未跌停封板（过滤项，硬否决）──
    filter_passed = (not theme_retreated) and (not is_limit_down_locked)
    if filter_passed:
        fired.append("[项4] 板块未退潮且未跌停封板（可成交）")
    else:
        reason = []
        if theme_retreated:
            reason.append("板块退潮")
        if is_limit_down_locked:
            reason.append("跌停封板")
        fired.append(f"[项4] {'+'.join(reason)}（硬否决）")

    total_score = content_score + (1 if filter_passed else 0)
    if not filter_passed:
        total_score = 0

    return TSignal(
        direction="add",
        rules_fired=fired,
        rules_score=total_score,
        price=price,
        snapshot=snap,
        layer_scores={"content": content_score, "filter": 1 if filter_passed else 0},
        trend_context=trend_ctx,
        trigger_threshold=params.min_rules_to_trigger,
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
        try:
            from market_layer import market_gate_for_add, market_gate_for_reduce, adjust_signal_weight
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
            reduce_sig.trigger_threshold = _apply_weight_to_threshold(
                params.min_rules_to_trigger, reduce_weight
            )
            add_sig.trigger_threshold = _apply_weight_to_threshold(
                params.min_rules_to_trigger, add_weight
            )
            market_gate_add["weight"] = add_weight
            market_gate_add["adjusted_threshold"] = add_sig.trigger_threshold
            market_gate_reduce["weight"] = reduce_weight
            market_gate_reduce["adjusted_threshold"] = reduce_sig.trigger_threshold
            # 把门控结果写入信号
            reduce_sig.market_gate = market_gate_reduce
            add_sig.market_gate = market_gate_add
        except ImportError:
            pass  # market_layer 不可用时跳过门控

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
