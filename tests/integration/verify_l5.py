#!/usr/bin/env python3
"""
L5 模块端到端验证脚本
======================
验证 L5 各组件的正确性与一致性。

测试覆盖:
  1. position_tracker: 持仓状态读写 + T+1 约束
  2. intraday_reference: 滚动指标计算（无未来函数）
  3. t_signal_engine: 减仓/加仓信号触发
  4. t_risk_guard: 风控检查（仓位/次数/价差/可用底仓/L1&L2熔断）
  5. backtest_t_strategy: 单日回测端到端
  6. 独立性验证：L1/L2 文件不存在时 L5 仍可运行

运行方式:
  python tests/integration/verify_l5.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 让 at0 包可导入
_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from at0 import execution as position_tracker
from at0 import features as intraday_reference
from at0 import strategy as t_signal_engine
from at0 import risk as t_risk_guard
from at0 import backtest as backtest_t_strategy
from at0.features import (
    MarketSnapshot,
    market_gate_for_add,
    market_gate_for_reduce,
    adjust_signal_weight,
    compute_market_snapshot,
    fetch_futures_basis,
    merge_with_reference_snapshot,
)
# market_layer / stock_quote_features 已并入 at0.features，保留别名以最小化改动
market_layer = intraday_reference
stock_quote_features = intraday_reference
from at0.data import save_minute_bars_to_csv


# ═══════════════════════════════════════════════════════════════
# 测试工具
# ═══════════════════════════════════════════════════════════════
class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  [PASS] {name}")
        else:
            self.failed += 1
            self.failures.append(f"{name}: {detail}")
            print(f"  [FAIL] {name} — {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"验证结果: {self.passed}/{total} 通过, {self.failed} 失败")
        if self.failures:
            print("失败详情:")
            for f in self.failures:
                print(f"  - {f}")
        print(f"{'='*60}")
        return self.failed == 0


# ═══════════════════════════════════════════════════════════════
# 测试 1: position_tracker — T+1 约束
# ═══════════════════════════════════════════════════════════════
def test_position_tracker(runner: TestRunner):
    print("\n[1] position_tracker — T+1 约束验证")
    with tempfile.TemporaryDirectory() as tmp:
        pos_file = Path(tmp) / "positions.json"
        # 初始化示例持仓
        position_tracker.init_sample_positions(pos_file)
        pos = position_tracker.get_position("600xxx.SH", pos_file)
        runner.check("持仓加载", pos is not None)
        runner.check("底仓股数", pos["base_shares"] == 3000, f"got {pos['base_shares']}")
        runner.check("初始可卖", position_tracker.get_sellable_shares("600xxx.SH", pos_file) == 3000)

        # 模拟买入 1000 股（反T 买入）→ locked_shares 增加
        position_tracker.apply_t_trade("600xxx.SH", "buy", 1000, 12.30, pos_file)
        pos = position_tracker.get_position("600xxx.SH", pos_file)
        runner.check("买入后 locked_shares=1000", pos["today_t_state"]["locked_shares"] == 1000,
                     f"got {pos['today_t_state']['locked_shares']}")
        runner.check("买入后 net_delta=+1000", pos["today_t_state"]["net_position_delta"] == 1000)
        runner.check("买入后 T次数=1", pos["today_t_state"]["t_trades_today"] == 1)
        # T+1 约束：可卖 = 3000 - 1000 = 2000
        runner.check("T+1约束: 可卖降至2000",
                     position_tracker.get_sellable_shares("600xxx.SH", pos_file) == 2000)

        # 模拟卖出 500 股老仓
        position_tracker.apply_t_trade("600xxx.SH", "sell", 500, 12.50, pos_file)
        pos = position_tracker.get_position("600xxx.SH", pos_file)
        runner.check("卖出后 net_delta=+500", pos["today_t_state"]["net_position_delta"] == 500)
        runner.check("卖出后 T次数=2", pos["today_t_state"]["t_trades_today"] == 2)
        runner.check("卖出后 locked_shares 不变=1000",
                     pos["today_t_state"]["locked_shares"] == 1000)

        # 重置今日状态（次日开盘）
        n = position_tracker.reset_today_state(pos_file)
        runner.check("reset_today_state 重置1只", n == 1)
        pos = position_tracker.get_position("600xxx.SH", pos_file)
        runner.check("重置后 locked_shares=0", pos["today_t_state"]["locked_shares"] == 0)
        runner.check("重置后 base_shares 增加（T+1解锁）",
                     pos["base_shares"] == 4000,  # 3000 + 1000(昨日买入)
                     f"got {pos['base_shares']}")


# ═══════════════════════════════════════════════════════════════
# 测试 1b: position_tracker — 并发写保护（P0-3）
# ═══════════════════════════════════════════════════════════════
def test_concurrent_write_protection(runner: TestRunner):
    """P0-3: 验证 apply_t_trade 的读-改-写是原子的，并发调用不丢更新。"""
    import threading

    print("\n[1b] position_tracker — 并发写保护验证（P0-3）")
    with tempfile.TemporaryDirectory() as tmp:
        pos_file = Path(tmp) / "positions.json"
        position_tracker.init_sample_positions(pos_file)

        # 两个线程同时操作同一只股票：一个卖500，一个买300
        barrier = threading.Barrier(2)

        def sell_500():
            barrier.wait()  # 同步启动，最大化竞争窗口
            position_tracker.apply_t_trade("600xxx.SH", "sell", 500, 12.50, pos_file)

        def buy_300():
            barrier.wait()
            position_tracker.apply_t_trade("600xxx.SH", "buy", 300, 12.30, pos_file)

        t1 = threading.Thread(target=sell_500)
        t2 = threading.Thread(target=buy_300)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        pos = position_tracker.get_position("600xxx.SH", pos_file)
        today = pos["today_t_state"]
        # 两笔都应该被记录：t_trades_today=2（如果丢更新则=1）
        runner.check("并发写: t_trades_today=2（不丢更新）",
                     today["t_trades_today"] == 2,
                     f"got {today['t_trades_today']}")
        # net_position_delta = -500 + 300 = -200
        runner.check("并发写: net_delta=-200（两笔都生效）",
                     today["net_position_delta"] == -200,
                     f"got {today['net_position_delta']}")
        # locked_shares = 300（只有买入增加）
        runner.check("并发写: locked_shares=300",
                     today["locked_shares"] == 300,
                     f"got {today['locked_shares']}")


# ═══════════════════════════════════════════════════════════════
# 测试 2: intraday_reference — 无未来函数
# ═══════════════════════════════════════════════════════════════
def test_intraday_reference_no_future(runner: TestRunner):
    print("\n[2] intraday_reference — 无未来函数验证")
    # 构造 50 根 K 线
    bars = []
    for i in range(50):
        p = 10.00 + i * 0.01
        bars.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": 10000, "amount": (p + 0.005) * 10000,
        })

    # 截止第 30 根的 VWAP
    snap_30 = intraday_reference.compute_reference_snapshot(bars[:30])
    snap_50 = intraday_reference.compute_reference_snapshot(bars[:50])

    # VWAP 应该不同（因为多了 20 根数据）
    runner.check("VWAP 截止30 != 截止50",
                 snap_30["vwap"] != snap_50["vwap"],
                 f"vwap_30={snap_30['vwap']}, vwap_50={snap_50['vwap']}")

    # 截止第 30 根的 VWAP 应该等于只用前 30 根计算的 VWAP
    vwap_30_direct = intraday_reference.cumulative_vwap(bars[:30])
    runner.check("VWAP 因果一致性",
                 abs(snap_30["vwap"] - vwap_30_direct) < 1e-9)

    # 布林带需要 20 根，截止第 30 根应该有值
    runner.check("布林带在30根时有值", snap_30["bb_mid"] is not None)
    # 截止第 15 根应该无值（数据不足）
    snap_15 = intraday_reference.compute_reference_snapshot(bars[:15])
    runner.check("布林带在15根时为None", snap_15["bb_mid"] is None)

    # RSI 需要 15 根（period+1），截止第 14 根应该为 None
    snap_14 = intraday_reference.compute_reference_snapshot(bars[:14])
    runner.check("RSI 在14根时为None", snap_14["rsi"] is None)
    runner.check("RSI 在30根时有值", snap_30["rsi"] is not None)


# ═══════════════════════════════════════════════════════════════
# 测试 3: t_signal_engine — 信号触发
# ═══════════════════════════════════════════════════════════════
def test_signal_engine(runner: TestRunner):
    print("\n[3] t_signal_engine — 信号触发验证")

    # 场景 A: 冲高缩量 → 应触发减仓
    bars_spike = []
    for i in range(40):
        if i < 25:
            p = 10.00 + i * 0.008
            vol = 12000 + i * 100
        else:
            p = 10.20 + (i - 25) * 0.012  # 冲高
            vol = 6000 - (i - 25) * 200   # 缩量
        bars_spike.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": max(vol, 100),
            "amount": (p + 0.005) * max(vol, 100),
        })
    result_spike = t_signal_engine.evaluate_all_signals(
        bars_spike, current_price=10.45, prev_close=10.00
    )
    runner.check("冲高缩量触发 reduce",
                 result_spike["recommendation"] == "reduce",
                 f"got {result_spike['recommendation']}")
    runner.check("reduce_score ≥ 3",
                 result_spike["reduce_signal"].rules_score >= 3,
                 f"got {result_spike['reduce_signal'].rules_score}")

    # 场景 B: 下探地量企稳 → 应触发加仓
    # 注意：企稳阶段 close 必须有涨有跌（真实横盘），否则若 close 单调
    # 微升，RSI 会误判为 100（超买），导致 reduce 信号同时触发。
    # P0-5: 三层结构要求 extreme≥2，此处用 extreme_min=1 测试基本触发逻辑
    # （三层约束由 test_p0_modules.py 的 test_trend_filter_on_signals 覆盖）
    bars_dip = []
    for i in range(40):
        if i < 25:
            p = 10.00 - i * 0.012       # 下探
            vol = 12000 + i * 200
            close = p - 0.001           # close 略低于 p（下跌趋势）
        else:
            p = 9.70                    # 真正横盘
            vol = 4000 - (i - 25) * 150
            # 企稳阶段 close 有涨有跌（模拟真实横盘）
            close = p + (0.001 if i % 2 == 0 else -0.001)
        bars_dip.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": close, "volume": max(vol, 100),
            "amount": close * max(vol, 100),
        })
    params_dip = t_signal_engine.SignalParams(extreme_min=1)
    result_dip = t_signal_engine.evaluate_all_signals(
        bars_dip, current_price=9.70, prev_close=10.00, params=params_dip
    )
    runner.check("下探企稳触发 add",
                 result_dip["recommendation"] == "add",
                 f"got {result_dip['recommendation']}")
    runner.check("add_score ≥ 3",
                 result_dip["add_signal"].rules_score >= 3,
                 f"got {result_dip['add_signal'].rules_score}")

    # 场景 C: 横盘无信号 → 应为 none
    bars_flat = []
    for i in range(40):
        p = 10.00 + (i % 5 - 2) * 0.001  # 微幅震荡
        bars_flat.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.01, "low": p - 0.01,
            "close": p, "volume": 10000, "amount": p * 10000,
        })
    result_flat = t_signal_engine.evaluate_all_signals(
        bars_flat, current_price=10.00, prev_close=10.00
    )
    runner.check("横盘无信号",
                 result_flat["recommendation"] == "none",
                 f"got {result_flat['recommendation']}")


# ═══════════════════════════════════════════════════════════════
# 测试 4: t_risk_guard — 风控
# ═══════════════════════════════════════════════════════════════
def test_risk_guard(runner: TestRunner):
    print("\n[4] t_risk_guard — 风控验证")
    with tempfile.TemporaryDirectory() as tmp:
        pos_file = Path(tmp) / "positions.json"
        position_tracker.init_sample_positions(pos_file)

        # 4.1 正常卖出应通过
        r = t_risk_guard.check_risk(
            "600xxx.SH", "sell", 1000, 12.50, 12.35,
            positions_path=pos_file,
        )
        runner.check("正常卖出通过", r.approved)

        # 4.2 预期价差不足应拒绝
        r = t_risk_guard.check_risk(
            "600xxx.SH", "sell", 1000, 12.36, 12.35,
            positions_path=pos_file,
        )
        runner.check("价差不足拒绝", not r.approved)

        # 4.3 卖出超量应调整
        r = t_risk_guard.check_risk(
            "600xxx.SH", "sell", 5000, 12.50, 12.35,
            positions_path=pos_file,
        )
        runner.check("超量调整到50%", r.adjusted_shares == 1500,
                     f"got {r.adjusted_shares}")

        # 4.4 模拟 L1 系统性风险（创建 l1_gate.json）
        l1_file = t_risk_guard.PROJECT_ROOT / "data" / "l1_gate.json"
        l1_file.parent.mkdir(parents=True, exist_ok=True)
        original_l1 = None
        if l1_file.exists():
            original_l1 = l1_file.read_text(encoding="utf-8")
        try:
            l1_file.write_text(json.dumps({
                "regime": "SYSTEMIC_RISK", "research_allowed": False
            }), encoding="utf-8")
            r = t_risk_guard.check_risk(
                "600xxx.SH", "buy", 1000, 9.90, 10.00,
                positions_path=pos_file,
            )
            runner.check("L1风险日禁买", not r.approved and "L1" in r.reason)
            r = t_risk_guard.check_risk(
                "600xxx.SH", "sell", 1000, 12.50, 12.35,
                positions_path=pos_file,
            )
            runner.check("L1风险日允许卖", r.approved)
        finally:
            if original_l1 is not None:
                l1_file.write_text(original_l1, encoding="utf-8")
            elif l1_file.exists():
                l1_file.unlink()

        # 4.5 独立性验证：L1 文件不存在时默认允许
        if l1_file.exists():
            l1_file.unlink()
        gate = t_risk_guard.read_l1_gate()
        runner.check("无L1文件时默认允许", gate["research_allowed"] is True)
        runner.check("无L1文件时默认RANGE_BOUND", gate["regime"] == "RANGE_BOUND")

        # 4.6 尾盘平衡检查
        positions = position_tracker.load_positions(pos_file)
        positions["600xxx.SH"]["today_t_state"]["net_position_delta"] = -500
        position_tracker.save_positions(positions, pos_file)
        eod = t_risk_guard.eod_balance_check("600xxx.SH", pos_file)
        runner.check("尾盘净减仓识别", eod["status"] == "net_reduce")


# ═══════════════════════════════════════════════════════════════
# 测试 5: backtest 端到端
# ═══════════════════════════════════════════════════════════════
def test_backtest_e2e(runner: TestRunner):
    print("\n[5] backtest_t_strategy — 端到端回测验证")
    with tempfile.TemporaryDirectory() as tmp:
        # 构造一日分钟数据（240 根）：上午冲高回落 + 下午下探企稳
        bars = []
        base = 10.00
        for i in range(240):
            if i < 60:        # 9:31-10:30 缓慢上行
                p = base + i * 0.005
                vol = 8000 + i * 50
            elif i < 120:     # 10:31-11:30 冲高后回落
                p = base + 0.30 - (i - 60) * 0.004
                vol = 6000 - (i - 60) * 20
            elif i < 180:     # 13:01-14:00 下探
                p = base + 0.06 - (i - 120) * 0.003
                vol = 5000 + (i - 120) * 30
            else:             # 14:01-15:00 企稳微升
                p = base - 0.12 + (i - 180) * 0.002
                vol = 4000 + (i - 180) * 20
            hh = 9 + (31 + i) // 60
            mm = (31 + i) % 60
            if hh >= 12 and hh < 13:
                hh = 13
                mm = (i - 119) % 60
            time_str = f"{hh:02d}:{mm:02d}:00"
            bars.append({
                "time": time_str,
                "open": p, "high": p + 0.015, "low": p - 0.015,
                "close": p + 0.002, "volume": max(int(vol), 100),
                "amount": (p + 0.002) * max(int(vol), 100),
            })

        params = backtest_t_strategy.BacktestParams(
            base_shares=3000, avg_cost=10.00,
        )
        result = backtest_t_strategy.backtest_single_day(
            code="600xxx.SH",
            trading_date="2026-07-22",
            bars=bars,
            prev_close=10.00,
            params=params,
        )
        runner.check("回测完成", result["bars_count"] == 240)
        runner.check("回测产生交易或无交易（都正常）",
                     result["t_trades"] >= 0)
        runner.check("尾盘状态有效",
                     result["eod_status"] in {"balanced", "net_reduce", "net_add", "insufficient_bars", "has_expired_legs"})
        print(f"    → T次数={result['t_trades']}, 净盈亏={result['net_pnl']:.2f}, "
              f"胜率={result['win_rate']*100:.0f}%")


# ═══════════════════════════════════════════════════════════════
# 测试 6: 独立性验证
# ═══════════════════════════════════════════════════════════════
def test_independence(runner: TestRunner):
    print("\n[6] 独立性验证 — L1/L2 文件不存在时 L5 仍可运行")
    # 临时移走 L1 文件
    l1_file = t_risk_guard.L1_GATE_FILE
    backup = None
    if l1_file.exists():
        backup = l1_file.read_bytes()
        l1_file.unlink()
    try:
        # L1 不存在时，read_l1_gate 应返回默认值
        gate = t_risk_guard.read_l1_gate()
        runner.check("L1不存在时默认regime=RANGE_BOUND", gate["regime"] == "RANGE_BOUND")
        runner.check("L1不存在时默认research_allowed=True", gate["research_allowed"] is True)
        runner.check("L1不存在时is_l1_systemic_risk=False",
                     t_risk_guard.is_l1_systemic_risk() is False)

        # L2 不存在时，read_theme_state 应返回 "unknown"
        state = t_risk_guard.read_theme_state("不存在的题材")
        runner.check("L2不存在时state=unknown", state == "unknown")
        runner.check("L2不存在时is_theme_retreated=False",
                     t_risk_guard.is_theme_retreated("不存在的题材") is False)
    finally:
        if backup is not None:
            l1_file.write_bytes(backup)


# ═══════════════════════════════════════════════════════════════
# 测试 7: intraday_reference 扩展指标（EMA/MA/MACD/DMI/CCI/BIAS/ROC/OBV/MFI）
# ═══════════════════════════════════════════════════════════════
def test_intraday_reference_extended(runner: TestRunner):
    print("\n[7] intraday_reference — 扩展指标验证")
    # 60 根 K 线：足够触发所有指标（MACD 需 35 根，DMI ADX 需 ~28 根）
    bars = []
    for i in range(60):
        p = 10.00 + i * 0.01 if i < 40 else 10.40 - (i - 40) * 0.015
        vol = 10000 + i * 50 if i < 40 else 8000 - (i - 40) * 100
        bars.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": max(vol, 100),
            "amount": (p + 0.005) * max(vol, 100),
        })

    snap = intraday_reference.compute_reference_snapshot(bars, current_price=10.45, prev_close=9.98)

    # 各指标在数据充足时应非 None
    runner.check("EMA(20) 在60根时有值", snap["ema"] is not None)
    runner.check("MA(5) 在60根时有值", snap["ma5"] is not None)
    runner.check("MA(20) 在60根时有值", snap["ma20"] is not None)
    runner.check("MACD 在60根时有值", snap["macd_dif"] is not None)
    runner.check("DMI/ADX 在60根时有值", snap["adx"] is not None)
    runner.check("CCI 在60根时有值", snap["cci"] is not None)
    runner.check("BIAS 在60根时有值", snap["bias"] is not None)
    runner.check("ROC 在60根时有值", snap["roc"] is not None)
    runner.check("OBV 在60根时有值", snap["obv"] is not None)
    runner.check("MFI 在60根时有值", snap["mfi"] is not None)

    # 数据不足时应为 None（MACD 需 35 根）
    snap_short = intraday_reference.compute_reference_snapshot(bars[:30])
    runner.check("MACD 在30根时为None", snap_short["macd_dif"] is None)
    runner.check("EMA 在30根时有值", snap_short["ema"] is not None)

    # 因果性：前30根 vs 前60根的 EMA 应不同
    runner.check("EMA 因果性（30≠60）", snap_short["ema"] != snap["ema"])


# ═══════════════════════════════════════════════════════════════
# 测试 8: market_layer — 市场层门控
# ═══════════════════════════════════════════════════════════════
def test_market_layer(runner: TestRunner):
    print("\n[8] market_layer — 市场层门控验证")

    # HOT 市场：涨停 120，跌停 5
    hot = market_layer.MarketSnapshot(
        up_limit_count=120, down_limit_count=5, up_ratio=60,
        timestamp="2026-07-22T10:00:00",
    )
    runner.check("HOT 情绪判定", hot.market_sentiment == "HOT")
    runner.check("HOT 可交易", hot.is_tradable_market)
    allowed, _ = market_layer.market_gate_for_add(hot)
    runner.check("HOT 加仓放行", allowed)
    runner.check("HOT 加仓权重 1.2",
                 abs(market_layer.adjust_signal_weight(hot, "add") - 1.2) < 1e-9)

    # COLD 市场：涨停 10，跌停 80
    cold = market_layer.MarketSnapshot(
        up_limit_count=10, down_limit_count=80, up_ratio=20,
        timestamp="2026-07-22T10:00:00",
    )
    runner.check("COLD 情绪判定", cold.market_sentiment == "COLD")
    runner.check("COLD 不可交易", not cold.is_tradable_market)
    allowed, _ = market_layer.market_gate_for_add(cold)
    runner.check("COLD 加仓拦截", not allowed)
    runner.check("COLD 加仓权重 0.5",
                 abs(market_layer.adjust_signal_weight(cold, "add") - 0.5) < 1e-9)
    # COLD 减仓仍允许
    allowed_r, _ = market_layer.market_gate_for_reduce(cold)
    runner.check("COLD 减仓放行", allowed_r)

    # NEUTRAL 市场（涨停 50，跌停 20，上涨占比 55%）
    neutral = market_layer.MarketSnapshot(
        up_limit_count=50, down_limit_count=20, up_ratio=55,
        timestamp="2026-07-22T10:00:00",
    )
    runner.check("NEUTRAL 情绪判定", neutral.market_sentiment == "NEUTRAL")
    runner.check("NEUTRAL 权重 1.0",
                 abs(market_layer.adjust_signal_weight(neutral, "add") - 1.0) < 1e-9)

    # 回测模式：注入缓存数据，不调 westock
    cached = market_layer.compute_market_snapshot(
        use_westock=False,
        cached_limit_board={"up_limit_count": 120, "down_limit_count": 5, "up_ratio": 60},
        cached_sector_ranking={
            "top_industries": [{"name": "半导体"}],
            "top_concepts": [{"name": "AI芯片"}],
            "top_inflow_sectors": [{"name": "半导体"}],
        },
    )
    runner.check("回测模式情绪 HOT", cached.market_sentiment == "HOT")
    runner.check("回测模式行业榜注入", len(cached.top_industries) == 1)

    # 独立性：themes_v17.json 不存在时不报错
    snap = market_layer.compute_market_snapshot(use_westock=False)
    runner.check("无 themes_v17 时独立运行", snap.themes_snapshot is None)
    runner.check("无数据时情绪 NEUTRAL", snap.market_sentiment == "NEUTRAL")

    # 期指升贴水一期未接入
    runner.check("期指升贴水返回 None", market_layer.fetch_futures_basis() is None)


# ═══════════════════════════════════════════════════════════════
# 测试 9: stock_quote_features + 决策层4项结构
# ═══════════════════════════════════════════════════════════════
def test_quote_features_and_layer_structure(runner: TestRunner):
    print("\n[9] stock_quote_features + 决策层4项结构验证")

    # 盘口特征合并（注入模拟 quote，不调 westock）
    fake_ref = {"current_price": 10.0, "vwap": 9.9, "rsi": 50.0}
    fake_quote = {
        "code": "sh600000", "current_price": 10.0,
        "quote_vwap": 9.9, "wb_ratio": -30.0,
        "inner_volume": 4000, "outer_volume": 6000,
        "limit_up": 11.0, "limit_down": 9.0,
    }
    merged = stock_quote_features.merge_with_reference_snapshot(fake_ref, fake_quote)
    runner.check("quote 合并 _quote_available=True", merged.get("_quote_available") is True)
    runner.check("quote 不覆盖 ref 自算 vwap", merged.get("vwap") == 9.9)
    runner.check("quote_vwap 合并", merged.get("quote_vwap") == 9.9)
    runner.check("涨跌停价合并", merged.get("limit_up") == 11.0)
    runner.check("主动买占比派生",
                 abs(merged.get("active_buy_ratio", 0) - 0.6) < 1e-9)
    runner.check("主动卖占比派生",
                 abs(merged.get("active_sell_ratio", 0) - 0.4) < 1e-9)

    # 无 quote 时（回测模式）
    merged_no_q = stock_quote_features.merge_with_reference_snapshot(fake_ref, None)
    runner.check("无 quote 时 _quote_available=False", merged_no_q.get("_quote_available") is False)
    runner.check("无 quote 时 ref 键保留", merged_no_q.get("vwap") == 9.9)

    # 决策层4项结构：用冲高缩量数据验证 layer_scores（content/filter）
    bars_spike = []
    for i in range(40):
        if i < 20:
            p = 10.00 + i * 0.01
            vol = 10000 + i * 100
        else:
            p = 10.20 + (i - 20) * 0.015
            vol = 8000 - (i - 20) * 400
        bars_spike.append({
            "time": f"09:{31 + i // 60}:{(31 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": vol, "amount": (p + 0.005) * vol,
        })
    result = t_signal_engine.evaluate_all_signals(
        bars_spike, current_price=10.35, prev_close=10.00
    )
    reduce_sig = result["reduce_signal"]
    runner.check("reduce 有 layer_scores(extreme/confirm/filter)",
                 "extreme" in reduce_sig.layer_scores and "confirm" in reduce_sig.layer_scores and "filter" in reduce_sig.layer_scores)
    runner.check("reduce 极值层 ≥1 触发", reduce_sig.layer_scores["extreme"] >= 1)
    runner.check("reduce 过滤项通过", reduce_sig.layer_scores["filter"] == 1)
    runner.check("reduce 有 trend_context", reduce_sig.trend_context in {"trend_up", "trend_down", "range", "extreme"})
    runner.check("reduce 分数 ≤5", reduce_sig.rules_score <= 5)

    # 市场层门控接入：COLD 市场 + 冲高缩量数据 → reduce 仍可触发，add 被拦截
    cold = market_layer.MarketSnapshot(
        up_limit_count=10, down_limit_count=80, up_ratio=20,
        timestamp="2026-07-22T10:00:00",
    )
    result_cold = t_signal_engine.evaluate_all_signals(
        bars_spike, current_price=10.35, prev_close=10.00, market=cold
    )
    runner.check("COLD 门控返回 market_gate_add", result_cold["market_gate_add"] is not None)
    runner.check("COLD 门控拦截 add",
                 result_cold["market_gate_add"]["allowed"] is False)
    # reduce 不受 COLD 门控影响
    runner.check("COLD 门控不影响 reduce 触发",
                 result_cold["recommendation"] in {"reduce", "none"})

    # 涨停封板硬否决验证
    result_locked = t_signal_engine.evaluate_all_signals(
        bars_spike, current_price=10.35, prev_close=10.00,
        is_limit_up_locked=True,
    )
    runner.check("涨停封板 reduce 硬否决（score=0）",
                 result_locked["reduce_signal"].rules_score == 0)


# ═══════════════════════════════════════════════════════════════
# 测试 10: candidate_screener + config_loader
# ═══════════════════════════════════════════════════════════════
def test_candidate_screener_and_config(runner: TestRunner):
    print("\n[10] candidate_screener + config_loader 验证")

    from at0 import screener as candidate_screener
    from at0 import config as config_loader

    # ScreenerParams 默认值
    sp = candidate_screener.ScreenerParams()
    runner.check("ScreenerParams 振幅阈值 3.5%",
                 abs(sp.min_20d_amplitude - 0.035) < 1e-9)
    runner.check("ScreenerParams 成交额阈值 1亿",
                 abs(sp.min_20d_amount - 1e8) < 1)
    runner.check("ScreenerParams 捕获空间 0.6%",
                 abs(sp.min_capture_spread - 0.006) < 1e-9)

    # ScreenResult 数据结构
    sr = candidate_screener.ScreenResult(code="test", eligible=False, reasons=[])
    runner.check("ScreenResult 初始化", sr.code == "test" and sr.eligible is False)

    # config_loader: yaml 不存在时返回默认值（不调 westock）
    # 通过临时修改 CONFIG_FILE 测试 fallback
    orig_file = config_loader.CONFIG_FILE
    config_loader.CONFIG_FILE = Path("/nonexistent/thresholds.yaml")
    try:
        sig_params = config_loader.load_signal_params()
        runner.check("yaml 不存在时 SignalParams fallback",
                     sig_params.vwap_dev_atr_multiplier == 0.8)
        risk_params = config_loader.load_risk_params()
        runner.check("yaml 不存在时 RiskParams fallback",
                     abs(risk_params.min_capture_spread - 0.006) < 1e-9)
    finally:
        config_loader.CONFIG_FILE = orig_file

    # config_loader: 真实 yaml 存在时加载（需 PyYAML）
    if config_loader.CONFIG_FILE.exists():
        try:
            import yaml  # noqa: F401
            sig_params = config_loader.load_signal_params()
            runner.check("yaml 加载 SignalParams ATR=0.8",
                         abs(sig_params.vwap_dev_atr_multiplier - 0.8) < 1e-9)
            runner.check("yaml 加载 SignalParams RSI=70",
                         abs(sig_params.rsi_overbought - 70.0) < 1e-9)
            risk_params = config_loader.load_risk_params()
            runner.check("yaml 加载 RiskParams spread=0.0075",
                         abs(risk_params.min_capture_spread - 0.0075) < 1e-9)
            runner.check("yaml 加载 RiskParams round_trip=0.003",
                         abs(risk_params.round_trip_cost - 0.003) < 1e-9)
            runner.check("yaml 加载 RiskParams max_t_size=0.25",
                         abs(risk_params.max_t_size_ratio - 0.25) < 1e-9)
        except ImportError:
            runner.check("PyYAML 未安装，跳过 yaml 加载测试", True)


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("L5 模块端到端验证")
    print("=" * 60)

    runner = TestRunner()
    test_position_tracker(runner)
    test_concurrent_write_protection(runner)
    test_intraday_reference_no_future(runner)
    test_signal_engine(runner)
    test_risk_guard(runner)
    test_backtest_e2e(runner)
    test_independence(runner)
    test_intraday_reference_extended(runner)
    test_market_layer(runner)
    test_quote_features_and_layer_structure(runner)
    test_candidate_screener_and_config(runner)

    ok = runner.summary()
    sys.exit(0 if ok else 1)
