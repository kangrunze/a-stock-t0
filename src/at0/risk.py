"""
risk 层合并模块
================

本模块为 risk 层的合并实现，整合了下列子领域能力：
  - cost_model:   佣金/印花税/滑点/冲击成本的统一建模（CostModel / calc_trade_cost / apply_slippage）
  - pre_trade:    下单前风控检查（RiskParams / check_risk / L1-L2 软联动熔断）
  - exposure:     敞口与尾盘时段策略（ExposurePolicy / approve_signal / RiskDecision）
  - exit_policy:  尾盘强制了结与风险事件（eod_risk_disposal / eod_balance_check）

合并自以下源文件（原 scripts/ 下文件保持不变）：
  - scripts/cost_model.py
  - scripts/exposure_policy.py
  - scripts/t_risk_guard.py

其中 t_risk_guard.py 原先依赖的 position_tracker 已合并入 .execution，
故本地依赖改为相对导入；l2_theme_reader 维持原导入方式。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# 同目录导入（execution.py 已合并 position_tracker）
from .execution import (
    POSITIONS_FILE,
    load_positions,
    save_positions,
    get_position,
    get_sellable_shares,
    get_t_trades_today,
    get_net_position_delta,
    apply_t_trade,
    reset_today_state,
    set_t_eligible,
    init_sample_positions,
)

from .data import get_theme_state


# ═══ risk: cost_model（佣金/印花税/滑点/冲击） ═══
"""
统一成本模型（P0-1 整改）
========================
将散落在 backtest_t_strategy.py 中的佣金、印花税、滑点、冲击成本
统一收敛到此处，确保回测、纸面监控和参数搜索使用同一个成本实现。

核心规则：
  - 买入：佣金 + 滑点（买入价上浮）
  - 卖出：佣金 + 印花税 + 滑点（卖出价下浮）
  - 来回总成本率 = 佣金×2 + 印花税 + 滑点×2 + 冲击成本×2

三种场景：
  - optimistic: 滑点减半，无冲击成本
  - base:       标准费率
  - pessimistic: 滑点加倍，含冲击成本
"""


@dataclass(frozen=True)
class CostModel:
    """统一成本模型。所有费率均为小数（0.00025 = 万2.5）。"""

    commission_rate: float = 0.00025      # 佣金（单边，万2.5）
    stamp_tax_rate: float = 0.0005        # 印花税（卖出单边，0.05%）
    slippage_rate: float = 0.001          # 滑点（单边，0.1%）
    impact_rate: float = 0.0              # 冲击成本（单边，默认0，小单忽略）
    scenario: str = "base"                # optimistic / base / pessimistic

    # ── 成交价（含滑点+冲击）──
    def fill_price(self, direction: str, raw_price: float) -> float:
        """
        计算实际成交价（含滑点和冲击成本）。
        买入上浮，卖出下浮（模拟对手价成交）。
        """
        spread = self.slippage_rate + self.impact_rate
        if direction == "buy":
            return round(raw_price * (1 + spread), 4)
        else:
            return round(raw_price * (1 - spread), 4)

    # ── 单笔成本 ──
    def calc_cost(self, direction: str, shares: int, fill_price: float) -> float:
        """
        计算单笔交易成本（佣金 + 印花税 + 冲击成本）。
        注意：fill_price 应已是含滑点的成交价。
        """
        amount = shares * fill_price
        commission = amount * self.commission_rate
        tax = amount * self.stamp_tax_rate if direction == "sell" else 0.0
        impact = amount * self.impact_rate
        return round(commission + tax + impact, 4)

    # ── 来回总成本率 ──
    def round_trip_cost_rate(self) -> float:
        """
        来回总成本率（买+卖），用于最小价差门槛。
        = 佣金×2 + 印花税 + 滑点×2 + 冲击×2
        """
        buy_side = self.commission_rate + self.slippage_rate + self.impact_rate
        sell_side = (
            self.commission_rate
            + self.stamp_tax_rate
            + self.slippage_rate
            + self.impact_rate
        )
        return round(buy_side + sell_side, 6)

    # ── 预期净收益 ──
    def expected_net_return(
        self,
        direction: str,
        signal_price: float,
        reference_price: float,
    ) -> float:
        """
        计算预期净收益率（扣除来回成本）。
        用于风控层判断是否值得交易。

        :return: 净收益率（小数），正值表示预期盈利
        """
        if reference_price <= 0:
            return 0.0
        gross_spread = abs(signal_price - reference_price) / reference_price
        return round(gross_spread - self.round_trip_cost_rate(), 6)

    # ── 场景工厂 ──
    @classmethod
    def optimistic(cls) -> "CostModel":
        """乐观场景：滑点减半，无冲击成本。"""
        return cls(
            slippage_rate=0.0005,
            impact_rate=0.0,
            scenario="optimistic",
        )

    @classmethod
    def base(cls) -> "CostModel":
        """基准场景：标准费率。"""
        return cls(scenario="base")

    @classmethod
    def pessimistic(cls) -> "CostModel":
        """悲观场景：滑点加倍，含冲击成本。"""
        return cls(
            slippage_rate=0.002,
            impact_rate=0.0005,
            scenario="pessimistic",
        )

    @classmethod
    def from_scenario(cls, scenario: str) -> "CostModel":
        """按场景名创建成本模型。"""
        if scenario == "optimistic":
            return cls.optimistic()
        elif scenario == "pessimistic":
            return cls.pessimistic()
        return cls.base()


# ═══════════════════════════════════════════════════════════════
# 兼容函数：供 backtest_t_strategy.py 旧接口调用
# ═══════════════════════════════════════════════════════════════
_DEFAULT_MODEL = CostModel.base()


def calc_trade_cost(
    direction: str,
    shares: int,
    price: float,
    model: Optional[CostModel] = None,
) -> float:
    """兼容旧接口：计算单笔交易成本。"""
    m = model or _DEFAULT_MODEL
    return m.calc_cost(direction, shares, price)


def apply_slippage(
    direction: str,
    price: float,
    model: Optional[CostModel] = None,
) -> float:
    """兼容旧接口：应用滑点。"""
    m = model or _DEFAULT_MODEL
    return m.fill_price(direction, price)


# ═══ risk: exposure_policy（时间边界 + 尾盘强制了结） ═══
"""
敞口策略与尾盘风控（P0-3 整改）
================================
将 t_risk_guard.py 中"只标记状态不执行"的尾盘检查改为
真正的风险处置：强制了结超时敞口、限制尾盘新建仓。

核心规则：
  - 14:20 后禁止新建无法当日处理的风险腿
  - 14:40 后只允许退出或降低风险
  - 单方向最多一个未配对腿
  - 超过 max_holding_bars 的腿自动标记 expired
  - 日终不平衡必须生成明确的风险事件

注意：本模块只决定信号是否允许执行和执行上限，不生成信号。
"""


@dataclass
class ExposurePolicy:
    """
    敞口与尾盘风控策略。

    bar_idx 参考（5分钟线，48根/天）：
      - 14:20 ≈ 第 200 根（4小时 = 48根，9:30~14:20 约 4.8小时 ≈ 57根...
        实际 5min: 9:30-11:30=24根, 13:00-15:00=24根, 共48根
        14:20 = 第 40 根（13:00起第20根）
        14:40 = 第 44 根
        14:50 = 第 46 根
      对于 1分钟线（240根/天）：
        14:20 = 第 200 根
        14:40 = 第 220 根
        14:50 = 第 230 根
    """
    max_open_legs_per_direction: int = 1      # 每方向最多未配对腿数
    max_holding_bars: int = 12                # 单笔最大持仓 K 线数
    no_new_after_bar_ratio: float = 0.83      # 14:20（占全天K线的比例）
    exit_only_after_bar_ratio: float = 0.92   # 14:40
    eod_check_bar_ratio: float = 0.96         # 14:50
    require_opposite_direction: bool = True   # 有未配对腿时只允许反方向

    def no_new_after_bar(self, bars_count: int) -> int:
        """14:20 后禁止新建风险腿。"""
        return int(bars_count * self.no_new_after_bar_ratio)

    def exit_only_after_bar(self, bars_count: int) -> int:
        """14:40 后只允许退出。"""
        return int(bars_count * self.exit_only_after_bar_ratio)

    def eod_check_bar(self, bars_count: int) -> int:
        """14:50 尾盘检查。"""
        return int(bars_count * self.eod_check_bar_ratio)


@dataclass
class RiskDecision:
    """风控决策结果。"""
    approved: bool
    reason: str = ""
    adjusted_shares: int = 0
    checks: list[str] = field(default_factory=list)


def approve_signal(
    direction: str,
    requested_shares: int,
    open_legs: list[dict],
    bar_idx: int,
    bars_count: int,
    policy: ExposurePolicy,
    sellable_shares: int = 0,
    t_trades_today: int = 0,
    max_t_trades_per_day: int = 4,
    max_t_size_ratio: float = 0.25,
    base_shares: int = 3000,
    l1_systemic_risk: bool = False,
    theme_retreated: bool = False,
) -> RiskDecision:
    """
    交易前风控批准（P0-4: 从 _try_execute 拆出）。

    检查项：
      1. 每日T次数限制
      2. L1/L2 熔断
      3. 尾盘时段限制（14:20后不新建，14:40后只退出）
      4. 单方向未配对腿数限制
      5. require_opposite_direction 约束
      6. 仓位比例限制
      7. T+1 可卖底仓（卖出时）

    :return: RiskDecision
    """
    checks: list[str] = []
    approved = True
    reason = ""

    # 1. 每日T次数
    if t_trades_today >= max_t_trades_per_day:
        return RiskDecision(
            approved=False,
            reason=f"已达每日最大T次数 {max_t_trades_per_day}",
            checks=[f"每日T次数：{t_trades_today}/{max_t_trades_per_day} ✗"],
        )
    checks.append(f"每日T次数：{t_trades_today}/{max_t_trades_per_day} ✓")

    # 2. L1 熔断
    if l1_systemic_risk and direction == "buy":
        return RiskDecision(
            approved=False,
            reason="L1 系统性风险日：禁止加仓/买回",
            checks=checks + ["L1 熔断：SYSTEMIC_RISK 禁买 ✗"],
        )

    # 3. L2 熔断
    if theme_retreated and direction == "buy":
        return RiskDecision(
            approved=False,
            reason="L2 题材退潮：禁止加仓/买回",
            checks=checks + ["L2 熔断：退潮 禁买 ✗"],
        )

    # 4. 尾盘时段限制
    no_new_bar = policy.no_new_after_bar(bars_count)
    exit_only_bar = policy.exit_only_after_bar(bars_count)

    if bar_idx >= exit_only_bar:
        # 14:40 后只允许退出（卖出已有买腿 / 买回已有卖腿）
        if not open_legs:
            return RiskDecision(
                approved=False,
                reason=f"14:40后无未配对腿，不允许新建仓（bar={bar_idx}）",
                checks=checks + [f"尾盘限制：14:40后仅退出 ✗"],
            )
        required_dir = "sell" if open_legs[0]["direction"] == "buy" else "buy"
        if direction != required_dir:
            return RiskDecision(
                approved=False,
                reason=f"14:40后只允许{required_dir}（退出已有敞口）",
                checks=checks + [f"尾盘限制：14:40后仅退出 ✗"],
            )
        checks.append("尾盘限制：14:40后退出已有敞口 ✓")
    elif bar_idx >= no_new_bar:
        # 14:20 后禁止新建无法当日处理的风险腿
        if not open_legs:
            return RiskDecision(
                approved=False,
                reason=f"14:20后禁止新建风险腿（bar={bar_idx}）",
                checks=checks + [f"尾盘限制：14:20后不新建 ✗"],
            )
        checks.append("尾盘限制：14:20后仅配对已有敞口 ✓")
    else:
        checks.append("尾盘限制：正常时段 ✓")

    # 5. 单方向未配对腿数限制
    same_dir_open = sum(
        1 for leg in open_legs if leg["direction"] == direction
    )
    if same_dir_open >= policy.max_open_legs_per_direction:
        return RiskDecision(
            approved=False,
            reason=f"已有 {same_dir_open} 个 {direction} 方向未配对腿，"
                   f"超过上限 {policy.max_open_legs_per_direction}",
            checks=checks + [f"单方向未配对腿：{same_dir_open}/{policy.max_open_legs_per_direction} ✗"],
        )
    checks.append(f"单方向未配对腿：{same_dir_open}/{policy.max_open_legs_per_direction} ✓")

    # 6. require_opposite_direction
    if policy.require_opposite_direction and open_legs:
        required_dir = "sell" if open_legs[0]["direction"] == "buy" else "buy"
        if direction != required_dir:
            return RiskDecision(
                approved=False,
                reason=f"有未配对腿时只允许反方向（需 {required_dir}）",
                checks=checks + [f"方向约束：需{required_dir} ✗"],
            )
        checks.append(f"方向约束：{direction} 配对已有敞口 ✓")

    # 7. 仓位比例
    max_shares = int(base_shares * max_t_size_ratio)
    max_shares = (max_shares // 100) * 100
    if max_shares <= 0:
        max_shares = 100
    if requested_shares > max_shares:
        requested_shares = max_shares
        checks.append(f"仓位比例：调整至 {max_shares} 股（≤{max_t_size_ratio*100:.0f}%）")
    else:
        checks.append(f"仓位比例：{requested_shares} ≤ {max_shares} ✓")

    # 8. T+1 可卖底仓（卖出时）
    if direction == "sell":
        if sellable_shares <= 0:
            return RiskDecision(
                approved=False,
                reason=f"无可用底仓（T+1锁定）",
                checks=checks + ["可用底仓：0 股 ✗"],
            )
        if requested_shares > sellable_shares:
            requested_shares = (sellable_shares // 100) * 100
            if requested_shares <= 0:
                return RiskDecision(
                    approved=False,
                    reason=f"可用底仓不足1手（sellable={sellable_shares}）",
                    checks=checks + [f"可用底仓：{sellable_shares} 不足1手 ✗"],
                )
            checks.append(f"可用底仓：调整至 {requested_shares} 股（sellable={sellable_shares}）")
        else:
            checks.append(f"可用底仓：{requested_shares} ≤ {sellable_shares} ✓")

    if approved:
        reason = "所有风控检查通过"
    return RiskDecision(
        approved=approved,
        reason=reason,
        adjusted_shares=requested_shares,
        checks=checks,
    )


@dataclass
class EodRiskEvent:
    """尾盘风险事件。"""
    code: str
    date: str
    event_type: str          # "expired" / "forced_close" / "unbalanced"
    direction: str           # 事件涉及的方向
    shares: int
    fill_price: float        # 建仓价
    last_close: float        # 收盘价
    unrealized_pnl: float    # 浮盈浮亏
    holding_bars: int        # 持仓时长
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "date": self.date,
            "event_type": self.event_type,
            "direction": self.direction,
            "shares": self.shares,
            "fill_price": round(self.fill_price, 4),
            "last_close": round(self.last_close, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "holding_bars": self.holding_bars,
            "description": self.description,
        }


def eod_risk_disposal(
    code: str,
    date: str,
    open_legs: list[dict],
    last_close: float,
    lifecycle,  # TradeLifecycle
) -> list[EodRiskEvent]:
    """
    尾盘风险处置（P0-3: 替代旧的 eod_balance_check）。

    不再只是"标记状态"，而是：
      1. 将超时腿标记为 expired
      2. 对所有 open leg 计算浮盈浮亏
      3. 生成明确的风险事件记录

    注意：本函数不执行强制平仓交易（不产生反向 Fill），
    只标记状态并记录风险事件。是否真正强制平仓由上层决定。
    """
    events = []
    for leg_dict in open_legs:
        direction = leg_dict.get("direction", "")
        shares = leg_dict.get("shares", 0)
        fill_price = leg_dict.get("fill_price", 0.0)
        holding_bars = leg_dict.get("holding_bars", 0)

        # 计算浮盈浮亏
        if direction == "buy":
            unrealized = (last_close - fill_price) * shares
        else:
            unrealized = (fill_price - last_close) * shares

        # 判断事件类型
        if holding_bars >= lifecycle.max_holding_bars:
            event_type = "expired"
            desc = f"持仓 {holding_bars} 根K线超过上限 {lifecycle.max_holding_bars}，标记过期"
        else:
            event_type = "unbalanced"
            desc = f"日终未配对敞口 {shares} 股，持仓 {holding_bars} 根K线"

        events.append(EodRiskEvent(
            code=code,
            date=date,
            event_type=event_type,
            direction=direction,
            shares=shares,
            fill_price=fill_price,
            last_close=last_close,
            unrealized_pnl=unrealized,
            holding_bars=holding_bars,
            description=desc,
        ))

    return events


# ═══ risk: t_risk_guard（pre_trade 检查 + L1/L2 熔断联动） ═══
"""
L5 T+0 风控守卫
=================
下单前最后一道闸门。检查所有风控约束:
  - 单次T仓位比例 ≤ 50% 底仓
  - 每日最大T次数 ≤ 4 次/只
  - 最小预期价差 ≥ 0.6%
  - 可用底仓股数（T+1 约束硬性校验）
  - L1 系统性风险日熔断（可选联动）
  - L2 题材退潮熔断（可选联动）
  - 尾盘平衡检查（14:50 强制）

独立性：风控本身不依赖 L1/L2。L1/L2 联动通过传入参数实现，
  如果上游状态文件不存在则按默认值（允许）处理，保证 L5 可独立运行。
"""

# ═══════════════════════════════════════════════════════════════
# 路径配置（L1/L2 软联动）
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # src/at0/ -> src/ -> 项目根
L1_GATE_FILE = PROJECT_ROOT / "data" / "l1_gate.json"
# P1-2: L2 题材文件发现已统一收敛到 l2_theme_reader.py


# ═══════════════════════════════════════════════════════════════
# 风控参数（合成数据网格搜索调优结果，待真实分钟数据验证）
# 调优来源: outputs/l5_backtest/param_tuning_report.json
# ═══════════════════════════════════════════════════════════════
@dataclass
class RiskParams:
    """L5 风控参数。

    P0-1 整改（2026-07-24）：默认值对齐 config/thresholds.yaml，不再使用
    整改前的旧值（0.5/0.006/0.001）。来回成本统一收敛到 CostModel，
    RiskParams 不再持有 round_trip_cost 字段（见 check_risk 的 cost_model 参数）。
    当前值为起始参考值，需用真实分钟数据复验。
    """
    max_t_size_ratio: float = 0.25        # 单次T仓位比例上限（底仓的 25%）
    max_t_trades_per_day: int = 4         # 每日最大T次数
    min_capture_spread: float = 0.0075    # 最小预期捕获空间（0.75%，扣除0.3%成本后净0.45%）
    eod_check_time: str = "14:50"         # 尾盘平衡检查时间


DEFAULT_RISK_PARAMS = RiskParams()


# ═══════════════════════════════════════════════════════════════
# 风控检查结果
# ═══════════════════════════════════════════════════════════════
@dataclass
class RiskCheckResult:
    """风控检查结果。"""
    approved: bool                         # 是否通过
    reason: str = ""                       # 通过/拒绝原因
    adjusted_shares: int = 0               # 调整后的建议股数（可能小于请求值）
    checks: list[str] = field(default_factory=list)  # 各项检查明细

    def __repr__(self) -> str:
        status = "✓ APPROVED" if self.approved else "✗ REJECTED"
        return f"[{status}] {self.reason} (shares={self.adjusted_shares})"


# ═══════════════════════════════════════════════════════════════
# L1 / L2 软联动读取
# ═══════════════════════════════════════════════════════════════
def read_l1_gate() -> dict:
    """
    读取 L1 宏观门控状态。文件不存在时返回默认值（允许 T）。

    独立性保证：L1 不存在不影响 L5 运行。
    """
    try:
        if L1_GATE_FILE.exists():
            with open(L1_GATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {"regime": "RANGE_BOUND", "research_allowed": True}


def is_l1_systemic_risk() -> bool:
    """L1 是否处于系统性风险日。"""
    gate = read_l1_gate()
    return (
        gate.get("regime") == "SYSTEMIC_RISK"
        or not gate.get("research_allowed", True)
    )


def read_theme_state(theme_name: Optional[str]) -> str:
    """
    读取关联题材的状态。文件不存在或无关联题材返回 "unknown"（视为非退潮）。

    P1-2: 改为委托 l2_theme_reader.get_theme_state()，
    统一文件发现逻辑（不再各自猜测文件名）。

    独立性保证：L2 不存在不影响 L5 运行。
    """
    return get_theme_state(theme_name)


def is_theme_retreated(theme_name: Optional[str]) -> bool:
    """关联题材是否处于退潮期。"""
    state = read_theme_state(theme_name)
    return state == "退潮"


# ═══════════════════════════════════════════════════════════════
# 风控主检查
# ═══════════════════════════════════════════════════════════════
def check_risk(
    code: str,
    direction: str,
    requested_shares: int,
    signal_price: float,
    reference_price: float,
    params: Optional[RiskParams] = None,
    cost_model: Optional[CostModel] = None,
    positions_path: Path = POSITIONS_FILE,
    bar_idx: Optional[int] = None,
    bars_count: Optional[int] = None,
    open_legs: Optional[list[dict]] = None,
    exposure_policy: Optional[ExposurePolicy] = None,
) -> RiskCheckResult:
    """
    下单前风控检查。

    P0-4 整改（2026-07-24）：成本口径统一到 CostModel，删除 RiskParams.round_trip_cost
    字段。预期价差检查的成本显示改用 cost_model.round_trip_cost_rate()。
    若未传 cost_model，默认用 CostModel.base()（与 thresholds.yaml 的 cost 段对齐）。

    P0-8 整改（2026-07-24）：补齐尾盘时段 / 单方向未配对腿 / require_opposite
    三项检查，与回测 approve_signal 接口对齐。这三项为可选检查——仅当传入
    bar_idx/bars_count/exposure_policy/open_legs 时才执行，保持向后兼容。
    实盘 monitor_single_stock 应传入这些参数以获得与回测一致的风控口径。

    参数:
      code: 股票代码
      direction: "buy"（加仓/买回）或 "sell"（减仓/卖出）
      requested_shares: 请求操作的股数
      signal_price: 信号触发价（用于计算预期价差）
      reference_price: 参考价（VWAP 或昨收，用于计算预期捕获空间）
      params: 风控参数
      cost_model: 统一成本模型（None 时用 CostModel.base()）
      positions_path: positions.json 路径
      bar_idx: 当前K线索引（P0-8 尾盘时段检查用，None 跳过）
      bars_count: 当日K线总数（P0-8 尾盘时段检查用，None 跳过）
      open_legs: 未配对腿列表（P0-8 单方向/require_opposite 检查用，None 跳过）
      exposure_policy: 敞口策略（P0-8 尾盘时段检查用，None 跳过）

    返回: RiskCheckResult
    """
    params = params or DEFAULT_RISK_PARAMS
    cm = cost_model or CostModel.base()
    if direction not in {"buy", "sell"}:
        return RiskCheckResult(approved=False, reason=f"invalid direction: {direction}")
    if requested_shares <= 0:
        return RiskCheckResult(approved=False, reason="requested_shares must be positive")

    pos = get_position(code, positions_path)
    if not pos:
        return RiskCheckResult(approved=False, reason=f"position not found: {code}")

    if not pos.get("t_eligible", True):
        return RiskCheckResult(approved=False, reason="t_eligible=false（手动或联动禁用）")

    base_shares = int(pos.get("base_shares", 0))
    if base_shares <= 0:
        return RiskCheckResult(approved=False, reason="base_shares <= 0")

    checks: list[str] = []
    approved = True
    reason = ""

    # ── 检查 1: 单次T仓位比例 ──
    max_shares = int(base_shares * params.max_t_size_ratio)
    # 取整到 100 股的倍数（A股最小交易单位）
    max_shares = (max_shares // 100) * 100
    if max_shares <= 0:
        max_shares = 100  # 至少允许 1 手
    if requested_shares > max_shares:
        requested_shares = max_shares
        checks.append(f"仓位比例限制：调整至 {max_shares} 股（≤{params.max_t_size_ratio*100:.0f}%底仓）")
    else:
        checks.append(f"仓位比例：{requested_shares} 股（≤{max_shares}）✓")

    # ── 检查 2: 每日最大T次数 ──
    t_trades_today = get_t_trades_today(code, positions_path)
    if t_trades_today >= params.max_t_trades_per_day:
        approved = False
        reason = f"已达每日最大T次数 {params.max_t_trades_per_day} 次"
        checks.append(f"每日T次数：{t_trades_today}/{params.max_t_trades_per_day} ✗")
        return RiskCheckResult(approved=False, reason=reason, checks=checks)
    checks.append(f"每日T次数：{t_trades_today}/{params.max_t_trades_per_day} ✓")

    # ── 检查 3: 最小预期价差 ──
    # P0-4: 成本口径统一到 CostModel，不再用 RiskParams.round_trip_cost
    if reference_price > 0:
        expected_spread = abs(signal_price - reference_price) / reference_price
        if expected_spread < params.min_capture_spread:
            approved = False
            round_trip = cm.round_trip_cost_rate()
            reason = (
                f"预期价差 {expected_spread*100:.2f}% < {params.min_capture_spread*100:.1f}%"
                f"（成本 {round_trip*100:.1f}%）"
            )
            checks.append(f"预期价差：{expected_spread*100:.2f}% ✗")
            return RiskCheckResult(approved=False, reason=reason, checks=checks)
        checks.append(f"预期价差：{expected_spread*100:.2f}% ≥ {params.min_capture_spread*100:.1f}% ✓")

    # ── 检查 4: 可用底仓股数（T+1 约束硬性校验，仅对卖出）──
    if direction == "sell":
        sellable = get_sellable_shares(code, positions_path)
        if requested_shares > sellable:
            if sellable <= 0:
                approved = False
                reason = f"无可用底仓（base={base_shares}, locked={base_shares - sellable}）"
                checks.append(f"可用底仓：0 股 ✗")
                return RiskCheckResult(approved=False, reason=reason, checks=checks)
            # 部分成交：调整到可用量
            requested_shares = (sellable // 100) * 100
            if requested_shares <= 0:
                approved = False
                reason = f"可用底仓不足 1 手（sellable={sellable}）"
                checks.append(f"可用底仓：{sellable} 股（不足 1 手）✗")
                return RiskCheckResult(approved=False, reason=reason, checks=checks)
            checks.append(f"可用底仓：调整至 {requested_shares} 股（sellable={sellable}）")
        else:
            checks.append(f"可用底仓：{requested_shares} ≤ {get_sellable_shares(code, positions_path)} ✓")

    # ── 检查 4b: 尾盘时段限制（P0-8: 与 approve_signal 对齐）──
    # 仅当传入 bar_idx/bars_count/exposure_policy 时执行（实盘 monitor 传入）
    if (bar_idx is not None and bars_count is not None and exposure_policy is not None):
        no_new_bar = exposure_policy.no_new_after_bar(bars_count)
        exit_only_bar = exposure_policy.exit_only_after_bar(bars_count)
        if bar_idx >= exit_only_bar:
            if not open_legs:
                return RiskCheckResult(
                    approved=False,
                    reason=f"14:40后无未配对腿，不允许新建仓（bar={bar_idx}）",
                    checks=checks + [f"尾盘限制：14:40后仅退出 ✗"],
                )
            required_dir = "sell" if open_legs[0].get("direction") == "buy" else "buy"
            if direction != required_dir:
                return RiskCheckResult(
                    approved=False,
                    reason=f"14:40后只允许{required_dir}（退出已有敞口）",
                    checks=checks + [f"尾盘限制：14:40后仅退出 ✗"],
                )
            checks.append("尾盘限制：14:40后退出已有敞口 ✓")
        elif bar_idx >= no_new_bar:
            if not open_legs:
                return RiskCheckResult(
                    approved=False,
                    reason=f"14:20后禁止新建风险腿（bar={bar_idx}）",
                    checks=checks + [f"尾盘限制：14:20后不新建 ✗"],
                )
            checks.append("尾盘限制：14:20后仅配对已有敞口 ✓")
        else:
            checks.append("尾盘限制：正常时段 ✓")

    # ── 检查 4c: 单方向未配对腿 + require_opposite（P0-8: 与 approve_signal 对齐）──
    if open_legs is not None and exposure_policy is not None:
        same_dir_open = sum(1 for leg in open_legs if leg.get("direction") == direction)
        if same_dir_open >= exposure_policy.max_open_legs_per_direction:
            return RiskCheckResult(
                approved=False,
                reason=f"已有 {same_dir_open} 个 {direction} 方向未配对腿，"
                       f"超过上限 {exposure_policy.max_open_legs_per_direction}",
                checks=checks + [f"单方向未配对腿：{same_dir_open}/{exposure_policy.max_open_legs_per_direction} ✗"],
            )
        checks.append(f"单方向未配对腿：{same_dir_open}/{exposure_policy.max_open_legs_per_direction} ✓")

        if exposure_policy.require_opposite_direction and open_legs:
            required_dir = "sell" if open_legs[0].get("direction") == "buy" else "buy"
            if direction != required_dir:
                return RiskCheckResult(
                    approved=False,
                    reason=f"有未配对腿时只允许反方向（需 {required_dir}）",
                    checks=checks + [f"方向约束：需{required_dir} ✗"],
                )
            checks.append(f"方向约束：{direction} 配对已有敞口 ✓")

    # ── 检查 5: L1 系统性风险熔断（软联动）──
    l1_risk = is_l1_systemic_risk()
    if l1_risk and direction == "buy":
        approved = False
        reason = "L1 系统性风险日：禁止加仓/买回（仅允许减仓）"
        checks.append("L1 熔断：SYSTEMIC_RISK 禁买 ✗")
        return RiskCheckResult(approved=False, reason=reason, checks=checks)
    checks.append(f"L1 熔断：{'SYSTEMIC_RISK（仅卖允许）' if l1_risk else '正常'} ✓")

    # ── 检查 6: L2 题材退潮熔断（软联动）──
    theme_name = pos.get("sector_tag")
    retreated = is_theme_retreated(theme_name)
    if retreated and direction == "buy":
        approved = False
        reason = f"L2 题材退潮：禁止加仓/买回（theme={theme_name}）"
        checks.append(f"L2 熔断：{theme_name} 退潮 禁买 ✗")
        return RiskCheckResult(approved=False, reason=reason, checks=checks)
    checks.append(f"L2 熔断：{'退潮（仅卖允许）' if retreated else '正常/未知'} ✓")

    if approved:
        reason = "所有风控检查通过"
    return RiskCheckResult(
        approved=approved,
        reason=reason,
        adjusted_shares=requested_shares,
        checks=checks,
    )


# ═══════════════════════════════════════════════════════════════
# 尾盘平衡检查
# ═══════════════════════════════════════════════════════════════
def eod_balance_check(
    code: str,
    positions_path: Path = POSITIONS_FILE,
) -> dict:
    """
    14:50 尾盘平衡检查。检查 net_position_delta，明确标记操作状态。

    返回:
    {
        "code": str,
        "net_position_delta": int,
        "status": "balanced" | "net_reduce" | "net_add",
        "action": str,  # 后续动作建议
    }
    """
    pos = get_position(code, positions_path)
    if not pos:
        return {"code": code, "status": "no_position"}

    delta = get_net_position_delta(code, positions_path)
    if delta == 0:
        status = "balanced"
        action = "T已完成，无需额外动作"
    elif delta < 0:
        status = "net_reduce"
        action = f"主动减仓 {-delta} 股，记录并接受减仓事实"
    else:
        status = "net_add"
        action = f"主动加仓 {delta} 股（T+1 锁定），记录并接受加仓事实"

    return {
        "code": code,
        "net_position_delta": delta,
        "status": status,
        "action": action,
    }


def eod_balance_check_all(positions_path: Path = POSITIONS_FILE) -> list[dict]:
    """对所有持仓执行尾盘平衡检查。"""
    positions = load_positions(positions_path)
    return [eod_balance_check(code, positions_path) for code in positions]


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # 写入测试持仓
    init_sample_positions()

    print("=== Test 1: 正常卖出（应通过）===")
    result = check_risk(
        code="600xxx.SH",
        direction="sell",
        requested_shares=1000,
        signal_price=12.50,
        reference_price=12.35,
    )
    print(result)
    for c in result.checks:
        print(f"  - {c}")

    print("\n=== Test 2: 卖出超量（应调整）===")
    result = check_risk(
        code="600xxx.SH",
        direction="sell",
        requested_shares=5000,  # 超过底仓 3000
        signal_price=12.50,
        reference_price=12.35,
    )
    print(result)
    for c in result.checks:
        print(f"  - {c}")

    print("\n=== Test 3: 预期价差不足（应拒绝）===")
    result = check_risk(
        code="600xxx.SH",
        direction="sell",
        requested_shares=1000,
        signal_price=12.36,  # 仅 0.08% 价差
        reference_price=12.35,
    )
    print(result)
    for c in result.checks:
        print(f"  - {c}")

    print("\n=== Test 4: 尾盘平衡检查 ===")
    # 模拟已做T（卖出 1000，买回 500 → 净减 500）
    positions = load_positions()
    positions["600xxx.SH"]["today_t_state"]["net_position_delta"] = -500
    save_positions(positions)
    eod = eod_balance_check("600xxx.SH")
    print(eod)
