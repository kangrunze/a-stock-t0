#!/usr/bin/env python3
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

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# 同目录导入
sys.path.insert(0, str(Path(__file__).resolve().parent))
from position_tracker import (
    POSITIONS_FILE,
    get_position,
    get_sellable_shares,
    get_t_trades_today,
    get_net_position_delta,
    load_positions,
)
from l2_theme_reader import get_theme_state

# ═══════════════════════════════════════════════════════════════
# 路径配置（L1/L2 软联动）
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
L1_GATE_FILE = PROJECT_ROOT / "data" / "l1_gate.json"
# P1-2: L2 题材文件发现已统一收敛到 l2_theme_reader.py


# ═══════════════════════════════════════════════════════════════
# 风控参数（合成数据网格搜索调优结果，待真实分钟数据验证）
# 调优来源: outputs/l5_backtest/param_tuning_report.json
# ═══════════════════════════════════════════════════════════════
@dataclass
class RiskParams:
    """L5 风控参数。当前值为合成数据调优结果，需用真实分钟数据复验。"""
    max_t_size_ratio: float = 0.5         # 单次T仓位比例上限（底仓的 50%）
    max_t_trades_per_day: int = 4         # 每日最大T次数
    min_capture_spread: float = 0.006     # 最小预期捕获空间（0.6%，方案 v0.2 起始值）
    round_trip_cost: float = 0.001        # 来回成本（0.1%）
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
    positions_path: Path = POSITIONS_FILE,
) -> RiskCheckResult:
    """
    下单前风控检查。

    参数:
      code: 股票代码
      direction: "buy"（加仓/买回）或 "sell"（减仓/卖出）
      requested_shares: 请求操作的股数
      signal_price: 信号触发价（用于计算预期价差）
      reference_price: 参考价（VWAP 或昨收，用于计算预期捕获空间）
      params: 风控参数
      positions_path: positions.json 路径

    返回: RiskCheckResult
    """
    params = params or DEFAULT_RISK_PARAMS
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
    if reference_price > 0:
        expected_spread = abs(signal_price - reference_price) / reference_price
        if expected_spread < params.min_capture_spread:
            approved = False
            reason = (
                f"预期价差 {expected_spread*100:.2f}% < {params.min_capture_spread*100:.1f}%"
                f"（成本 {params.round_trip_cost*100:.1f}%）"
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
    from position_tracker import init_sample_positions, save_positions, load_positions

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
