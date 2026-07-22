#!/usr/bin/env python3
"""
T-eligible 候选筛选器
====================
方案 v0.2 第二节：对持仓/候选标的进行 T-eligible 筛选。

筛选条件:
  1. 20日平均振幅 ≥ 3.5%（振幅太小扣完成本无利可图）
  2. 20日日均成交额 ≥ 1亿元（保证挂单能成交，滑点可控）
  3. 当日非一字板/一字跌停（封死无法成交）
  4. 单笔预期捕获空间 ≥ 0.6%（覆盖来回交易成本后正边际）

数据源: westock kline（日级，取最近20日）+ quote（当日状态）

独立性: 仅依赖 westock-data CLI，不依赖 L1-L4。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WESTOCK_NODE = os.environ.get(
    "WESTOCK_NODE",
    str(Path.home() / ".workbuddy/binaries/node/versions/22.22.2/node.exe"),
)
WESTOCK_DIR = os.environ.get(
    "WESTOCK_DIR",
    "D:/Users/kangrunze/AppData/Local/Programs/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/westock-data",
)
WESTOCK_SCRIPT = os.path.join(WESTOCK_DIR, "scripts", "index.js")


@dataclass
class ScreenerParams:
    """候选筛选参数（方案 v0.2 第二节起始值）。"""
    min_20d_amplitude: float = 0.035       # 20日平均振幅 ≥ 3.5%
    min_20d_amount: float = 1.0e8          # 20日日均成交额 ≥ 1亿元
    min_capture_spread: float = 0.006      # 单笔预期捕获空间 ≥ 0.6%


DEFAULT_SCREENER_PARAMS = ScreenerParams()


@dataclass
class ScreenResult:
    """单只股票的筛选结果。"""
    code: str
    eligible: bool
    reasons: list[str]
    avg_amplitude_20d: Optional[float] = None    # 20日平均振幅
    avg_amount_20d: Optional[float] = None       # 20日日均成交额
    is_one_word_board: Optional[bool] = None     # 当日是否一字板
    expected_capture: Optional[float] = None     # 预期捕获空间


def _to_westock_symbol(code: str) -> str:
    if code.startswith(("sh", "sz", "bj")):
        return code
    if len(code) == 6 and code[0] == "6":
        return f"sh{code}"
    if len(code) == 6 and code[0] in {"0", "2", "3"}:
        return f"sz{code}"
    if len(code) == 6 and code[0] in {"4", "8"}:
        return f"bj{code}"
    return code


def _run_westock(cmd: str, timeout: int = 45) -> Optional[object]:
    """调用 westock CLI，返回解析后的 JSON。"""
    import json
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(WESTOCK_DIR, "node_modules")
    env["PYTHONIOENCODING"] = "utf-8"
    full_cmd = [WESTOCK_NODE, WESTOCK_SCRIPT] + cmd.split() + ["--raw"]
    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None
        return json.loads(stdout)
    except Exception as e:
        print(f"[WARN] candidate_screener westock call failed: {e}", file=sys.stderr)
        return None


def screen_candidate(code: str, params: ScreenerParams = DEFAULT_SCREENER_PARAMS) -> ScreenResult:
    """
    对单只股票进行 T-eligible 筛选。

    返回 ScreenResult。任何数据源失败时对应检查项跳过（不阻塞其他项）。
    """
    symbol = _to_westock_symbol(code)
    result = ScreenResult(code=code, eligible=False, reasons=[])

    # ── 检查 1+2: 20日振幅 + 日均成交额（从日 K 线获取）──
    kline_data = _run_westock(f"kline {symbol} --period daily --count 20")
    if isinstance(kline_data, list) and len(kline_data) >= 20:
        amplitudes = []
        amounts = []
        for bar in kline_data[-20:]:
            high = float(bar.get("high", 0))
            low = float(bar.get("low", 0))
            prev_close = float(bar.get("prev_close", 0))
            amount = float(bar.get("amount", 0))
            if prev_close > 0:
                amplitudes.append((high - low) / prev_close)
            amounts.append(amount)
        if amplitudes:
            result.avg_amplitude_20d = sum(amplitudes) / len(amplitudes)
            if result.avg_amplitude_20d >= params.min_20d_amplitude:
                result.reasons.append(f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% ≥ {params.min_20d_amplitude*100:.1f}% ✓")
            else:
                result.reasons.append(f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% < {params.min_20d_amplitude*100:.1f}% ✗")
        if amounts:
            result.avg_amount_20d = sum(amounts) / len(amounts)
            if result.avg_amount_20d >= params.min_20d_amount:
                result.reasons.append(f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 ≥ 1亿 ✓")
            else:
                result.reasons.append(f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 < 1亿 ✗")
    else:
        result.reasons.append("日K线数据不足，跳过振幅/成交额检查 ⚠")

    # ── 检查 3: 当日非一字板（从 quote 获取）──
    quote_data = _run_westock(f"quote {symbol}")
    if isinstance(quote_data, list) and quote_data:
        q = quote_data[0]
        open_price = float(q.get("open", 0))
        high = float(q.get("high", 0))
        low = float(q.get("low", 0))
        ceiling = float(q.get("price_ceiling", 0))
        floor = float(q.get("price_floor", 0))
        if ceiling > 0 and open_price >= ceiling and high == low:
            result.is_one_word_board = True
            result.reasons.append("当日一字涨停板，无法做T ✗")
        elif floor > 0 and open_price <= floor and high == low:
            result.is_one_word_board = True
            result.reasons.append("当日一字跌停板，无法做T ✗")
        else:
            result.is_one_word_board = False
            result.reasons.append("非一字板，可成交 ✓")

        # ── 检查 4: 单笔预期捕获空间（用当日振幅近似）──
        prev_close = float(q.get("prev_close", 0))
        if prev_close > 0 and high > low:
            result.expected_capture = (high - low) / prev_close
            if result.expected_capture >= params.min_capture_spread:
                result.reasons.append(f"预期捕获 {result.expected_capture*100:.2f}% ≥ {params.min_capture_spread*100:.1f}% ✓")
            else:
                result.reasons.append(f"预期捕获 {result.expected_capture*100:.2f}% < {params.min_capture_spread*100:.1f}% ✗")
    else:
        result.reasons.append("无实时报价，跳过一字板/捕获空间检查 ⚠")

    # ── 综合判定：所有 ✗ 项不出现即为 eligible ──
    has_fail = any("✗" in r for r in result.reasons)
    result.eligible = not has_fail and len(result.reasons) >= 3
    return result


if __name__ == "__main__":
    print("=== T-eligible 候选筛选自检（实盘 sh600000）===")
    r = screen_candidate("sh600000")
    print(f"  code: {r.code}")
    print(f"  eligible: {r.eligible}")
    print(f"  20日均振幅: {r.avg_amplitude_20d*100:.2f}%" if r.avg_amplitude_20d else "  20日均振幅: N/A")
    print(f"  20日均额: {r.avg_amount_20d/1e8:.2f}亿" if r.avg_amount_20d else "  20日均额: N/A")
    print(f"  一字板: {r.is_one_word_board}")
    print(f"  预期捕获: {r.expected_capture*100:.2f}%" if r.expected_capture else "  预期捕获: N/A")
    for reason in r.reasons:
        print(f"    {reason}")
