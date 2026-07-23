#!/usr/bin/env python3
"""
P0 整改模块单元测试
===================
覆盖 Section 7.1 要求的单元测试：
  1. 成本模型：佣金、印花税、滑点、不同场景
  2. FIFO 配对：同日、跨日、部分配对、同方向连续交易
  3. T+1：新买入锁定、旧底仓可卖、跨日解锁
  4. 最大持仓时长：超时标记 expired
  5. 时间边界：14:20、14:40、14:50
  6. 尾盘风险处置：浮盈浮亏、风险事件生成

运行方式:
  python tests/unit/test_p0_modules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让 at0 包可导入（用于 monkey-patch at0.strategy 模块）
_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from at0.risk import CostModel, ExposurePolicy, RiskDecision, approve_signal, eod_risk_disposal
from at0.execution import TradeLifecycle, TradeLeg, LegStatus
from at0.backtest import (
    BacktestParams,
    BacktestState,
    compute_unrealized_pnl,
    compute_daily_stats,
    summarize_one_stock,
    aggregate_batch,
)
from at0.features import detect_market_regime
from at0.strategy import SignalParams, evaluate_reduce_signal, evaluate_add_signal


# ═══════════════════════════════════════════════════════════════
# 测试工具（与 verify_l5.py 风格一致）
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

    def summary(self) -> bool:
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"P0 模块测试结果: {self.passed}/{total} 通过, {self.failed} 失败")
        if self.failures:
            print("失败详情:")
            for f in self.failures:
                print(f"  - {f}")
        print(f"{'='*60}")
        return self.failed == 0


# ═══════════════════════════════════════════════════════════════
# 测试 1: CostModel — 成本计算
# ═══════════════════════════════════════════════════════════════
def test_cost_model_basic(runner: TestRunner):
    print("\n[1] CostModel — 基础成本计算")
    m = CostModel.base()

    # 买入：佣金 only（无印花税）
    # 1000股 × 10元 = 10000元，佣金 = 10000 × 0.00025 = 2.5
    buy_cost = m.calc_cost("buy", 1000, 10.0)
    runner.check("买入成本=佣金2.5元",
                 abs(buy_cost - 2.5) < 0.01, f"got {buy_cost}")

    # 卖出：佣金 + 印花税
    # 1000股 × 10元 = 10000元，佣金=2.5，印花税=10000×0.0005=5.0，合计=7.5
    sell_cost = m.calc_cost("sell", 1000, 10.0)
    runner.check("卖出成本=佣金2.5+印花税5.0=7.5元",
                 abs(sell_cost - 7.5) < 0.01, f"got {sell_cost}")

    # 滑点：买入上浮
    buy_fill = m.fill_price("buy", 10.0)
    runner.check("买入滑点上浮 10→10.01",
                 abs(buy_fill - 10.01) < 0.001, f"got {buy_fill}")

    # 滑点：卖出下浮
    sell_fill = m.fill_price("sell", 10.0)
    runner.check("卖出滑点下浮 10→9.99",
                 abs(sell_fill - 9.99) < 0.001, f"got {sell_fill}")


def test_cost_model_round_trip(runner: TestRunner):
    print("\n[2] CostModel — 来回总成本率")
    m = CostModel.base()
    # 来回 = 佣金×2 + 印花税 + 滑点×2 = 0.00025×2 + 0.0005 + 0.001×2 = 0.003
    rate = m.round_trip_cost_rate()
    runner.check("来回成本率=0.003（0.3%）",
                 abs(rate - 0.003) < 0.0001, f"got {rate}")

    # 最小价差门槛应大于来回成本
    # 0.4% > 0.3% → 净收益为正
    net = m.expected_net_return("sell", 10.04, 10.0)
    runner.check("0.4%价差净收益≈0.001（正）",
                 net > 0 and abs(net - 0.001) < 0.0001, f"got {net}")

    # 0.2% < 0.3% → 净收益为负
    net_neg = m.expected_net_return("sell", 10.02, 10.0)
    runner.check("0.2%价差净收益为负",
                 net_neg < 0, f"got {net_neg}")


def test_cost_model_scenarios(runner: TestRunner):
    print("\n[3] CostModel — 三种场景")
    opt = CostModel.optimistic()
    base = CostModel.base()
    pess = CostModel.pessimistic()

    # 乐观：滑点减半 0.0005
    runner.check("optimistic 滑点=0.0005",
                 abs(opt.slippage_rate - 0.0005) < 1e-9, f"got {opt.slippage_rate}")
    runner.check("optimistic 无冲击",
                 opt.impact_rate == 0.0)

    # 基准：标准
    runner.check("base 滑点=0.001",
                 abs(base.slippage_rate - 0.001) < 1e-9)

    # 悲观：滑点加倍 0.002 + 冲击 0.0005
    runner.check("pessimistic 滑点=0.002",
                 abs(pess.slippage_rate - 0.002) < 1e-9)
    runner.check("pessimistic 冲击=0.0005",
                 abs(pess.impact_rate - 0.0005) < 1e-9)

    # 来回成本率：pessimistic > base > optimistic
    runner.check("来回成本 pess > base > opt",
                 pess.round_trip_cost_rate() > base.round_trip_cost_rate() > opt.round_trip_cost_rate())

    # from_scenario 工厂
    runner.check("from_scenario('pessimistic') 正确",
                 CostModel.from_scenario("pessimistic").scenario == "pessimistic")
    runner.check("from_scenario('unknown') fallback base",
                 CostModel.from_scenario("unknown").scenario == "base")


# ═══════════════════════════════════════════════════════════════
# 测试 4: TradeLifecycle — FIFO 配对（同日）
# ═══════════════════════════════════════════════════════════════
def test_fifo_same_day(runner: TestRunner):
    print("\n[4] TradeLifecycle — FIFO 同日配对")
    lc = TradeLifecycle(max_holding_bars=12)

    # 先买 1000 股 @10.0
    r1 = lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5, cost=2.5)
    runner.check("买入1000股 未配对",
                 r1["paired"] is False and r1["pnl"] == 0.0)
    runner.check("open_legs=1", lc.open_legs_count == 1)

    # 后卖 1000 股 @10.2 → 配对，盈亏=(10.2-10.0)×1000=200
    r2 = lc.add_fill("sell", 1000, 10.2, "10:00", "2026-07-15", 10, cost=7.5)
    runner.check("卖出1000股 配对成功",
                 r2["paired"] is True)
    runner.check("配对盈亏=200元",
                 abs(r2["pnl"] - 200.0) < 0.01, f"got {r2['pnl']}")
    runner.check("open_legs=0（全部配对）", lc.open_legs_count == 0)
    runner.check("paired_count=1", lc.paired_count == 1)


def test_fifo_partial_pairing(runner: TestRunner):
    print("\n[5] TradeLifecycle — 部分配对")
    lc = TradeLifecycle(max_holding_bars=12)

    # 买 1000 股 @10.0
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)

    # 只卖 300 股 @10.1 → 部分配对，剩 700 股 open
    r2 = lc.add_fill("sell", 300, 10.1, "10:00", "2026-07-15", 10)
    runner.check("部分配对成功",
                 r2["paired"] is True)
    runner.check("部分配对盈亏=(10.1-10.0)×300=30",
                 abs(r2["pnl"] - 30.0) < 0.01, f"got {r2['pnl']}")
    runner.check("剩余 open leg 700股",
                 lc.open_legs_count == 1 and lc.open_legs[0].shares == 700,
                 f"got shares={lc.open_legs[0].shares if lc.open_legs else 'N/A'}")


def test_fifo_same_direction_no_pair(runner: TestRunner):
    print("\n[6] TradeLifecycle — 同方向不配对")
    lc = TradeLifecycle(max_holding_bars=12)

    # 买 1000
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)
    # 再买 500 → 同方向，不应配对
    r2 = lc.add_fill("buy", 500, 10.1, "09:50", "2026-07-15", 8)
    runner.check("同方向买入不配对",
                 r2["paired"] is False)
    runner.check("open_legs=2（两个独立买腿）",
                 lc.open_legs_count == 2)


# ═══════════════════════════════════════════════════════════════
# 测试 7: TradeLifecycle — 跨日配对（不按日重置）
# ═══════════════════════════════════════════════════════════════
def test_fifo_cross_day(runner: TestRunner):
    print("\n[7] TradeLifecycle — 跨日配对（FIFO 队列不重置）")
    lc = TradeLifecycle(max_holding_bars=100)  # 设大避免过期

    # 第1日：买 1000 股 @10.0
    lc.add_fill("buy", 1000, 10.0, "14:30", "2026-07-15", 40, cost=2.5)
    runner.check("第1日建仓 open_legs=1", lc.open_legs_count == 1)

    # 第1日日终：导出 open legs
    exported = lc.export_open_legs()
    runner.check("导出1个未配对腿", len(exported) == 1)
    runner.check("导出方向=buy", exported[0]["direction"] == "buy")

    # 第2日：新建 lifecycle，导入延续
    lc2 = TradeLifecycle(max_holding_bars=100)
    lc2.import_open_legs(exported)
    runner.check("第2日导入1个open leg", lc2.open_legs_count == 1)

    # 第2日：卖 1000 股 @10.3 → 应与第1日的买腿配对
    r = lc2.add_fill("sell", 1000, 10.3, "09:35", "2026-07-16", 5, cost=7.5)
    runner.check("跨日配对成功",
                 r["paired"] is True, f"got paired={r['paired']}")
    runner.check("跨日配对盈亏=(10.3-10.0)×1000=300",
                 abs(r["pnl"] - 300.0) < 0.01, f"got {r['pnl']}")
    runner.check("配对后 open_legs=0", lc2.open_legs_count == 0)


def test_cross_day_holding_bars_continuity(runner: TestRunner):
    print("\n[8] TradeLifecycle — 跨日 holding_bars 连续性")
    lc = TradeLifecycle(max_holding_bars=100)

    # 第1日：bar_idx=40 建仓，bar_idx=47 时 holding_bars=7
    lc.add_fill("buy", 1000, 10.0, "14:30", "2026-07-15", 40)
    lc.update_holding(47, 10.1)
    runner.check("第1日日终 holding_bars=7",
                 lc.open_legs[0].holding_bars == 7,
                 f"got {lc.open_legs[0].holding_bars}")

    # 导出 → 第2日导入
    exported = lc.export_open_legs()
    lc2 = TradeLifecycle(max_holding_bars=100)
    lc2.import_open_legs(exported)

    # 第2日 bar_idx=0 时，holding_bars 应继续从 7 开始
    lc2.update_holding(0, 10.1)
    runner.check("跨日 holding_bars 连续=7（不归零）",
                 lc2.open_legs[0].holding_bars == 7,
                 f"got {lc2.open_legs[0].holding_bars}")

    # bar_idx=3 时，holding_bars=10
    lc2.update_holding(3, 10.2)
    runner.check("holding_bars=10（7+3）",
                 lc2.open_legs[0].holding_bars == 10,
                 f"got {lc2.open_legs[0].holding_bars}")


# ═══════════════════════════════════════════════════════════════
# 测试 9: TradeLifecycle — 最大持仓时长
# ═══════════════════════════════════════════════════════════════
def test_max_holding_duration(runner: TestRunner):
    print("\n[9] TradeLifecycle — 最大持仓时长超时 expired")
    lc = TradeLifecycle(max_holding_bars=12)

    # bar_idx=5 建仓
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)
    runner.check("建仓后 open_legs=1", lc.open_legs_count == 1)

    # 更新到 bar_idx=16（holding=11），未超时
    lc.update_holding(16, 10.1)
    expired = lc.check_expiry(16)
    runner.check("holding=11 未超时", len(expired) == 0)

    # 更新到 bar_idx=17（holding=12），超时
    lc.update_holding(17, 10.1)
    expired = lc.check_expiry(17)
    runner.check("holding=12 标记过期", len(expired) == 1)
    runner.check("过期腿状态=EXPIRED",
                 expired[0].status == LegStatus.EXPIRED)
    runner.check("过期后 open_legs=0", lc.open_legs_count == 0)
    runner.check("过期腿进入 closed_legs", len(lc.closed_legs) == 1)


def test_max_favorable_adverse(runner: TestRunner):
    print("\n[10] TradeLifecycle — 最大有利/不利偏移跟踪")
    lc = TradeLifecycle(max_holding_bars=100)

    # 买腿 @10.0
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)

    # 价格涨到 10.5 → 有利 +0.5
    lc.update_holding(10, 10.5)
    runner.check("买腿 max_favorable=0.5",
                 abs(lc.open_legs[0].max_favorable - 0.5) < 0.01)
    runner.check("买腿 max_adverse=0",
                 lc.open_legs[0].max_adverse == 0.0)

    # 价格跌到 9.8 → 不利 +0.2（但 max_favorable 仍为 0.5）
    lc.update_holding(15, 9.8)
    runner.check("买腿 max_favorable 保持 0.5",
                 abs(lc.open_legs[0].max_favorable - 0.5) < 0.01)
    runner.check("买腿 max_adverse=0.2",
                 abs(lc.open_legs[0].max_adverse - 0.2) < 0.01)


# ═══════════════════════════════════════════════════════════════
# 测试 11: TradeLifecycle — 未配对浮盈浮亏
# ═══════════════════════════════════════════════════════════════
def test_unrealized_pnl(runner: TestRunner):
    print("\n[11] TradeLifecycle — 未配对敞口浮盈浮亏")
    lc = TradeLifecycle(max_holding_bars=100)

    # 买 1000 @10.0
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)
    # 当前价 10.5 → 浮盈 +500
    pnl = lc.unrealized_pnl(10.5)
    runner.check("买腿浮盈=+500",
                 abs(pnl - 500.0) < 0.01, f"got {pnl}")

    # 卖腿 1000 @10.0（先清空再建卖腿）
    lc2 = TradeLifecycle(max_holding_bars=100)
    lc2.add_fill("sell", 1000, 10.0, "09:35", "2026-07-15", 5)
    # 当前价 9.5 → 卖腿浮盈 +500
    pnl2 = lc2.unrealized_pnl(9.5)
    runner.check("卖腿浮盈=+500（价格下跌有利）",
                 abs(pnl2 - 500.0) < 0.01, f"got {pnl2}")

    # 当前价 10.5 → 卖腿浮亏 -500
    pnl3 = lc2.unrealized_pnl(10.5)
    runner.check("卖腿浮亏=-500（价格上涨不利）",
                 abs(pnl3 - (-500.0)) < 0.01, f"got {pnl3}")


# ═══════════════════════════════════════════════════════════════
# 测试 12: T+1 可卖底仓
# ═══════════════════════════════════════════════════════════════
def test_t_plus_1_sellable(runner: TestRunner):
    print("\n[12] BacktestState — T+1 可卖底仓约束")
    state = BacktestState(base_shares=3000, avg_cost=10.0, lifecycle=TradeLifecycle())

    # 初始可卖 = 底仓 3000
    runner.check("初始可卖=3000", state.sellable_shares == 3000)

    # 模拟反T买入 1000 股 → locked +1000
    state.locked_shares = 1000
    runner.check("买入后可卖=2000（T+1锁定）",
                 state.sellable_shares == 2000)

    # 卖出 500 股老仓 → locked 不变（卖的是底仓）
    state.net_position_delta = -500
    runner.check("卖出后 locked 不变=1000",
                 state.locked_shares == 1000)
    runner.check("卖出后可卖仍=2000",
                 state.sellable_shares == 2000)

    # 次日重置：locked 清零，底仓增加
    state2 = BacktestState(base_shares=4000, avg_cost=10.0, lifecycle=TradeLifecycle())
    runner.check("次日底仓增至4000（T+1解锁）",
                 state2.sellable_shares == 4000)


# ═══════════════════════════════════════════════════════════════
# 测试 13: ExposurePolicy — 时间边界
# ═══════════════════════════════════════════════════════════════
def test_exposure_policy_time_boundaries(runner: TestRunner):
    print("\n[13] ExposurePolicy — 时间边界（5分钟线48根/天）")
    p = ExposurePolicy()
    bars_count = 48  # 5分钟线

    # 14:20 ≈ 0.83 × 48 = 39.84 → 39
    no_new = p.no_new_after_bar(bars_count)
    runner.check("14:20 阈值=39（5min线）",
                 no_new == 39, f"got {no_new}")

    # 14:40 ≈ 0.92 × 48 = 44.16 → 44
    exit_only = p.exit_only_after_bar(bars_count)
    runner.check("14:40 阈值=44（5min线）",
                 exit_only == 44, f"got {exit_only}")

    # 14:50 ≈ 0.96 × 48 = 46.08 → 46
    eod = p.eod_check_bar(bars_count)
    runner.check("14:50 阈值=46（5min线）",
                 eod == 46, f"got {eod}")

    # 1分钟线 240 根
    bars_count_1m = 240
    no_new_1m = p.no_new_after_bar(bars_count_1m)
    runner.check("14:20 阈值=199（1min线）",
                 no_new_1m == 199, f"got {no_new_1m}")


def test_approve_signal_normal(runner: TestRunner):
    print("\n[14] approve_signal — 正常时段放行")
    p = ExposurePolicy()
    decision = approve_signal(
        direction="sell",
        requested_shares=300,
        open_legs=[],
        bar_idx=10,
        bars_count=48,
        policy=p,
        sellable_shares=3000,
        t_trades_today=0,
        max_t_trades_per_day=4,
        max_t_size_ratio=0.25,
        base_shares=3000,
    )
    runner.check("正常时段放行", decision.approved)
    runner.check("股数不调整=300", decision.adjusted_shares == 300)


def test_approve_signal_14_20_no_new(runner: TestRunner):
    print("\n[15] approve_signal — 14:20 后禁止新建风险腿")
    p = ExposurePolicy()
    # bar_idx=40 >= 39（14:20阈值），open_legs 为空
    decision = approve_signal(
        direction="sell",
        requested_shares=300,
        open_legs=[],  # 无未配对腿 → 禁止新建
        bar_idx=40,
        bars_count=48,
        policy=p,
        sellable_shares=3000,
    )
    runner.check("14:20后无敞口时拒绝新建",
                 not decision.approved)
    runner.check("拒绝原因含'14:20'",
                 "14:20" in decision.reason)


def test_approve_signal_14_40_exit_only(runner: TestRunner):
    print("\n[16] approve_signal — 14:40 后只允许退出")
    p = ExposurePolicy()
    # 已有买腿，14:40后只允许卖出（退出）
    open_legs = [{"direction": "buy", "shares": 500, "fill_price": 10.0}]
    decision_sell = approve_signal(
        direction="sell",
        requested_shares=300,
        open_legs=open_legs,
        bar_idx=45,  # >= 44（14:40阈值）
        bars_count=48,
        policy=p,
        sellable_shares=3000,
    )
    runner.check("14:40后卖出（退出买腿）放行",
                 decision_sell.approved)

    # 14:40后买入（同方向加仓）应拒绝
    decision_buy = approve_signal(
        direction="buy",
        requested_shares=300,
        open_legs=open_legs,
        bar_idx=45,
        bars_count=48,
        policy=p,
    )
    runner.check("14:40后买入（非退出）拒绝",
                 not decision_buy.approved)


def test_approve_signal_opposite_direction(runner: TestRunner):
    print("\n[17] approve_signal — require_opposite_direction 约束")
    p = ExposurePolicy(require_opposite_direction=True)
    open_legs = [{"direction": "buy", "shares": 500, "fill_price": 10.0}]

    # 有买腿时，只允许卖出
    decision_sell = approve_signal(
        direction="sell", requested_shares=300, open_legs=open_legs,
        bar_idx=10, bars_count=48, policy=p, sellable_shares=3000,
    )
    runner.check("有买腿时卖出放行", decision_sell.approved)

    # 有买腿时，买入应拒绝
    decision_buy = approve_signal(
        direction="buy", requested_shares=300, open_legs=open_legs,
        bar_idx=10, bars_count=48, policy=p,
    )
    runner.check("有买腿时买入拒绝", not decision_buy.approved)


def test_approve_signal_t1_sellable(runner: TestRunner):
    print("\n[18] approve_signal — T+1 可卖底仓检查")
    p = ExposurePolicy()
    # sellable=0 → 卖出应拒绝
    decision = approve_signal(
        direction="sell", requested_shares=300, open_legs=[],
        bar_idx=10, bars_count=48, policy=p,
        sellable_shares=0,
    )
    runner.check("可卖=0 时卖出拒绝", not decision.approved)
    runner.check("拒绝原因含'T+1'或'可用底仓'",
                 "T+1" in decision.reason or "底仓" in decision.reason)


# ═══════════════════════════════════════════════════════════════
# 测试 19: EOD 风险处置
# ═══════════════════════════════════════════════════════════════
def test_eod_risk_disposal(runner: TestRunner):
    print("\n[19] eod_risk_disposal — 尾盘风险事件生成")
    lc = TradeLifecycle(max_holding_bars=12)

    # 建一个未配对买腿
    lc.add_fill("buy", 1000, 10.0, "14:30", "2026-07-15", 40)
    # 日终 holding_bars = 47 - 40 = 7（未超时）
    lc.update_holding(47, 10.3)

    open_legs = lc.export_open_legs()
    events = eod_risk_disposal("600000", "2026-07-15", open_legs, 10.3, lc)

    runner.check("生成1个风险事件", len(events) == 1)
    ev = events[0]
    runner.check("事件类型=unbalanced（未超时）",
                 ev.event_type == "unbalanced")
    runner.check("浮盈浮亏=(10.3-10.0)×1000=+300",
                 abs(ev.unrealized_pnl - 300.0) < 0.01, f"got {ev.unrealized_pnl}")
    runner.check("方向=buy", ev.direction == "buy")
    runner.check("股数=1000", ev.shares == 1000)


def test_eod_risk_disposal_expired(runner: TestRunner):
    print("\n[20] eod_risk_disposal — 超时腿标记 expired")
    lc = TradeLifecycle(max_holding_bars=5)

    # 建仓后 holding 超过 5
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)
    lc.update_holding(20, 9.8)  # holding=15 > 5
    lc.check_expiry(20)  # 标记过期

    # 此时 open_legs 已清空，手动构造一个超时场景
    expired_leg = [{
        "direction": "buy", "shares": 1000, "fill_price": 10.0,
        "holding_bars": 15,
    }]
    events = eod_risk_disposal("600000", "2026-07-15", expired_leg, 9.8, lc)
    runner.check("超时腿事件类型=expired",
                 events[0].event_type == "expired")
    runner.check("浮亏=(9.8-10.0)×1000=-200",
                 abs(events[0].unrealized_pnl - (-200.0)) < 0.01,
                 f"got {events[0].unrealized_pnl}")


# ═══════════════════════════════════════════════════════════════
# 测试 21: BacktestParams — CostModel/ExposurePolicy 注入
# ═══════════════════════════════════════════════════════════════
def test_backtest_params_injection(runner: TestRunner):
    print("\n[21] BacktestParams — CostModel/ExposurePolicy 注入")
    # 默认从旧字段构造
    p = BacktestParams()
    cm = p.get_cost_model()
    runner.check("默认 CostModel 场景=base", cm.scenario == "base")
    runner.check("默认佣金=0.00025",
                 abs(cm.commission_rate - 0.00025) < 1e-9)

    ep = p.get_exposure_policy()
    runner.check("默认 ExposurePolicy max_holding_bars=12",
                 ep.max_holding_bars == 12)
    runner.check("默认 require_opposite_direction=True",
                 ep.require_opposite_direction is True)

    # 显式注入 pessimistic 场景
    p2 = BacktestParams(cost_model=CostModel.pessimistic())
    cm2 = p2.get_cost_model()
    runner.check("注入 pessimistic 场景",
                 cm2.scenario == "pessimistic")
    runner.check("pessimistic 滑点=0.002",
                 abs(cm2.slippage_rate - 0.002) < 1e-9)

    # 显式注入自定义 ExposurePolicy
    p3 = BacktestParams(
        exposure_policy=ExposurePolicy(max_holding_bars=6, require_opposite_direction=False),
    )
    ep3 = p3.get_exposure_policy()
    runner.check("自定义 max_holding_bars=6", ep3.max_holding_bars == 6)
    runner.check("自定义 require_opposite_direction=False",
                 ep3.require_opposite_direction is False)


# ═══════════════════════════════════════════════════════════════
# 测试 22: FIFO 跨日不变量守卫（最高优先级 — 守护历史 84% 漏统计 bug）
# ═══════════════════════════════════════════════════════════════
def test_fifo_cross_day_invariant_guard(runner: TestRunner):
    """
    守护历史教训：按日重置 FIFO 队列导致 84% 跨日交易被排除统计。
    本测试断言：跨日配对（正确实现）与按日重置（错误实现）结果不同。
    """
    print("\n[22] FIFO 跨日不变量守卫 — 跨日配对 vs 按日重置")

    # ── 正确实现：FIFO 队列跨日不重置 ──
    lc_correct = TradeLifecycle(max_holding_bars=100)
    # 第1日尾盘买入 1000 @10.0
    lc_correct.add_fill("buy", 1000, 10.0, "14:30", "2026-07-15", 40)
    exported = lc_correct.export_open_legs()
    # 第2日导入并卖出 1000 @10.3 → 配对
    lc_day2 = TradeLifecycle(max_holding_bars=100)
    lc_day2.import_open_legs(exported)
    r_correct = lc_day2.add_fill("sell", 1000, 10.3, "09:35", "2026-07-16", 5)

    runner.check("跨日配对: paired=True", r_correct["paired"] is True)
    runner.check("跨日配对: pnl=300",
                 abs(r_correct["pnl"] - 300.0) < 0.01, f"got {r_correct['pnl']}")
    runner.check("跨日配对: open_legs=0", lc_day2.open_legs_count == 0)

    # ── 错误实现：按日重置（模拟历史 bug）──
    lc_buggy = TradeLifecycle(max_holding_bars=100)
    # 第1日买入
    lc_buggy.add_fill("buy", 1000, 10.0, "14:30", "2026-07-15", 40)
    # 第2日：不导入 open legs，直接新建空 lifecycle（模拟按日重置）
    lc_day2_buggy = TradeLifecycle(max_holding_bars=100)
    r_buggy = lc_day2_buggy.add_fill("sell", 1000, 10.3, "09:35", "2026-07-16", 5)

    runner.check("按日重置(bug): paired=False", r_buggy["paired"] is False)
    runner.check("按日重置(bug): pnl=0", r_buggy["pnl"] == 0.0)
    runner.check("按日重置(bug): 产生未配对卖腿", lc_day2_buggy.open_legs_count == 1)

    # ── 关键断言：两种实现结果不同 ──
    runner.check("跨日配对 vs 按日重置 结果不同",
                 r_correct["paired"] != r_buggy["paired"]
                 and r_correct["pnl"] != r_buggy["pnl"])


# ═══════════════════════════════════════════════════════════════
# 测试 23: Position 分仓不变量（T+0 核心）
# ═══════════════════════════════════════════════════════════════
def test_position_split_invariant(runner: TestRunner):
    """
    A股 T+1 约束下 Position 必须区分底仓和锁定仓。
    不变量：sellable = base - locked；买入增加 locked；卖出不改变 locked；
    次日 locked 清零、base 增加（T+1 解锁）。
    """
    print("\n[23] Position 分仓不变量 — T+1 底仓/锁定仓分离")

    # 初始：底仓 3000，无锁定
    state = BacktestState(base_shares=3000, avg_cost=10.0, lifecycle=TradeLifecycle())
    runner.check("初始 sellable=3000（=base-locked）",
                 state.sellable_shares == 3000)

    # 反T买入 1000 → locked +1000，sellable 不变（买的是新仓不是底仓）
    state.locked_shares += 1000
    runner.check("买入后 locked=1000", state.locked_shares == 1000)
    runner.check("买入后 sellable=2000（base不变，locked增加）",
                 state.sellable_shares == 2000)

    # 卖出 500 老仓 → net_delta -500，locked 不变
    state.net_position_delta -= 500
    runner.check("卖出后 locked 仍=1000（卖的是底仓）",
                 state.locked_shares == 1000)
    runner.check("卖出后 sellable 仍=2000",
                 state.sellable_shares == 2000)

    # 买入不超 sellable 的约束由 approve_signal 保证（已在测试18验证）

    # 次日：locked 清零，base 增加（T+1 解锁）
    state_next = BacktestState(
        base_shares=3000 + 1000,  # 昨日 locked 转为今日 base
        avg_cost=10.0,
        lifecycle=TradeLifecycle(),
    )
    runner.check("次日 base=4000（locked 解锁）",
                 state_next.base_shares == 4000)
    runner.check("次日 locked=0", state_next.locked_shares == 0)
    runner.check("次日 sellable=4000",
                 state_next.sellable_shares == 4000)

    # 边界：locked > base 时 sellable 不为负
    state_edge = BacktestState(base_shares=1000, avg_cost=10.0, lifecycle=TradeLifecycle())
    state_edge.locked_shares = 1500  # 异常但需防护
    runner.check("locked>base 时 sellable=0（不为负）",
                 state_edge.sellable_shares == 0)


# ═══════════════════════════════════════════════════════════════
# 测试 24: backtest_metrics — 统计口径一致性
# ═══════════════════════════════════════════════════════════════
def test_backtest_metrics_consistency(runner: TestRunner):
    """
    验证 backtest_metrics 的统计函数与 trade_lifecycle 口径一致。
    """
    print("\n[24] backtest_metrics — 统计口径一致性")

    # ── compute_unrealized_pnl 与 TradeLifecycle.unrealized_pnl 一致 ──
    lc = TradeLifecycle(max_holding_bars=100)
    lc.add_fill("buy", 1000, 10.0, "09:35", "2026-07-15", 5)
    lc.add_fill("sell", 500, 9.8, "10:00", "2026-07-15", 10)
    last_close = 10.5

    pnl_from_lc = lc.unrealized_pnl(last_close)
    open_legs = lc.export_open_legs()
    pnl_from_metrics = compute_unrealized_pnl(open_legs, last_close)
    runner.check("compute_unrealized_pnl 与 lifecycle 一致",
                 abs(pnl_from_lc - pnl_from_metrics) < 0.01,
                 f"lc={pnl_from_lc}, metrics={pnl_from_metrics}")

    # ── compute_daily_stats ──
    trades = [
        {"pnl": 100.0, "paired": True},
        {"pnl": -50.0, "paired": True},
        {"pnl": 0.0, "paired": False},
    ]
    stats = compute_daily_stats(trades, net_position_delta=0, risk_events=[])
    runner.check("daily win_rate=0.5（1赢1亏，不含未配对）",
                 abs(stats["win_rate"] - 0.5) < 0.001, f"got {stats['win_rate']}")
    runner.check("daily eod_status=balanced（delta=0）",
                 stats["eod_status"] == "balanced")

    stats2 = compute_daily_stats(trades, net_position_delta=-100, risk_events=[])
    runner.check("daily eod_status=net_reduce（delta<0）",
                 stats2["eod_status"] == "net_reduce")

    stats3 = compute_daily_stats(trades, net_position_delta=0,
                                 risk_events=[{"type": "expired"}])
    runner.check("daily eod_status=has_expired_legs",
                 stats3["eod_status"] == "has_expired_legs")

    # ── summarize_one_stock ──
    result = {
        "code": "600000",
        "daily_results": [
            {"trades": [{"pnl": 100, "cost": 5, "paired": True},
                        {"pnl": -30, "cost": 5, "paired": True}]},
            {"trades": [{"pnl": 0, "cost": 5, "paired": False}]},
        ],
        "unrealized_pnl": -200.0,
        "final_open_legs_count": 1,
    }
    summary = summarize_one_stock("600000", result)
    runner.check("summarize: total_trades=3", summary["total_trades"] == 3)
    runner.check("summarize: paired_trades=2", summary["paired_trades"] == 2)
    runner.check("summarize: win_trades=1", summary["win_trades"] == 1)
    runner.check("summarize: win_rate=0.5",
                 abs(summary["win_rate"] - 0.5) < 0.001)
    runner.check("summarize: gross_pnl=70", abs(summary["gross_pnl"] - 70) < 0.01)
    runner.check("summarize: total_cost=15", abs(summary["total_cost"] - 15) < 0.01)
    runner.check("summarize: net_pnl=55", abs(summary["net_pnl"] - 55) < 0.01)
    runner.check("summarize: unrealized=-200", abs(summary["unrealized_pnl"] - (-200)) < 0.01)
    runner.check("summarize: net_with_unrealized=-145",
                 abs(summary["net_pnl_with_unrealized"] - (-145)) < 0.01)

    # ── aggregate_batch ──
    per_stock = [
        summary,
        {"code": "600001", "total_trades": 2, "paired_trades": 2,
         "unpaired_trades": 0, "win_trades": 2, "loss_trades": 0,
         "win_rate": 1.0, "gross_pnl": 200.0, "total_cost": 10.0,
         "net_pnl": 190.0, "unrealized_pnl": 0.0,
         "net_pnl_with_unrealized": 190.0, "final_open_legs_count": 0},
    ]
    overall = aggregate_batch(per_stock, all_trades_count=5)
    runner.check("batch: stocks=2", overall["stocks"] == 2)
    runner.check("batch: paired_trades=4", overall["paired_trades"] == 4)
    runner.check("batch: win_trades=3", overall["win_trades"] == 3)
    runner.check("batch: win_rate=0.75",
                 abs(overall["win_rate"] - 0.75) < 0.001)
    runner.check("batch: net_pnl=245", abs(overall["net_pnl"] - 245) < 0.01)
    # stock1 net_with_unrealized=-145(亏损), stock2 net_with_unrealized=190(盈利) → profitable=1
    runner.check("batch: profitable_stocks=1", overall["profitable_stocks"] == 1)
    runner.check("batch: losing_stocks=1", overall["losing_stocks"] == 1)
    runner.check("batch: final_open_legs_count=1", overall["final_open_legs_count"] == 1)


# ═══════════════════════════════════════════════════════════════
# 测试 25: P0-6 — detect_market_regime 市场状态识别
# ═══════════════════════════════════════════════════════════════
def test_detect_market_regime(runner: TestRunner):
    """验证 features 层的 regime 检测逻辑。"""
    print("\n[25] detect_market_regime — 市场状态识别")

    # 数据不足（< 60 根）→ range
    snap_short = {"bars_count": 40, "adx": 80.0, "macd_dif": 1, "macd_dea": 0,
                  "pdi": 30, "mdi": 10, "vwap": 10.0, "vwap_dev": 0.03, "atr": 0.04}
    runner.check("bars<60 → range（数据不足保守返回）",
                 detect_market_regime(snap_short) == "range",
                 f"got {detect_market_regime(snap_short)}")

    # ADX < 25 → range（无趋势）
    snap_range = {"bars_count": 100, "adx": 15.0, "macd_dif": 1, "macd_dea": 0,
                  "pdi": 20, "mdi": 18, "vwap": 10.0, "vwap_dev": 0.01, "atr": 0.04}
    runner.check("ADX<25 → range（无趋势）",
                 detect_market_regime(snap_range) == "range",
                 f"got {detect_market_regime(snap_range)}")

    # 上升趋势：ADX≥25 + dif>dea + pdi>mdi
    snap_up = {"bars_count": 100, "adx": 30.0, "macd_dif": 0.05, "macd_dea": 0.03,
               "pdi": 28, "mdi": 12, "vwap": 10.0, "vwap_dev": 0.01, "atr": 0.04}
    runner.check("上升趋势 → trend_up",
                 detect_market_regime(snap_up) == "trend_up",
                 f"got {detect_market_regime(snap_up)}")

    # 下降趋势：ADX≥25 + dif<dea + mdi>pdi
    snap_down = {"bars_count": 100, "adx": 30.0, "macd_dif": -0.05, "macd_dea": -0.03,
                 "pdi": 12, "mdi": 28, "vwap": 10.0, "vwap_dev": -0.01, "atr": 0.04}
    runner.check("下降趋势 → trend_down",
                 detect_market_regime(snap_down) == "trend_down",
                 f"got {detect_market_regime(snap_down)}")

    # 极端趋势：ADX≥40 + |vwap_dev| ≥ 2×ATR_relative
    # ATR_relative = 0.04/10.0 = 0.004, extreme_dev = 2×0.004 = 0.008
    # |vwap_dev| = 0.03 > 0.008 → extreme
    snap_extreme = {"bars_count": 100, "adx": 45.0, "macd_dif": 0.05, "macd_dea": 0.03,
                    "pdi": 35, "mdi": 5, "vwap": 10.0, "vwap_dev": 0.03, "atr": 0.04}
    runner.check("极端趋势 → extreme（ADX高+价格远离VWAP）",
                 detect_market_regime(snap_extreme) == "extreme",
                 f"got {detect_market_regime(snap_extreme)}")

    # ADX≥40 但 vwap_dev 不够大 → 不是 extreme，走 trend_up
    snap_high_adx_normal = {"bars_count": 100, "adx": 42.0, "macd_dif": 0.05, "macd_dea": 0.03,
                            "pdi": 28, "mdi": 12, "vwap": 10.0, "vwap_dev": 0.005, "atr": 0.04}
    runner.check("ADX≥40但偏离不够 → trend_up（非extreme）",
                 detect_market_regime(snap_high_adx_normal) == "trend_up",
                 f"got {detect_market_regime(snap_high_adx_normal)}")

    # 数据缺失 → range
    snap_missing = {"bars_count": 100, "adx": None, "macd_dif": 1, "macd_dea": 0}
    runner.check("指标缺失 → range",
                 detect_market_regime(snap_missing) == "range")


# ═══════════════════════════════════════════════════════════════
# 测试 26: P0-6 — 趋势过滤对信号的影响
# ═══════════════════════════════════════════════════════════════
def test_trend_filter_on_signals(runner: TestRunner):
    """验证 trend_up/trend_down/extreme 对信号阈值的调整。

    测试 25 已覆盖 detect_market_regime 本身；此处用 monkey-patch
    直接注入 regime，隔离测试信号引擎对 regime 的响应逻辑，
    避免合成 K 线数据的 ADX 饱和问题。
    """
    import at0.strategy as _strategy_mod

    print("\n[26] P0-6 趋势过滤 — 信号阈值调整")

    # 构造 65 根 K 线（满足 min_bars_for_trend=60 的数据量要求）
    bars_up = []
    for i in range(65):
        if i < 40:
            p = 10.00 + i * 0.006
            vol = 10000 + i * 50
        else:
            p = 10.24 + (i - 40) * 0.010
            vol = 6000 - (i - 40) * 150
        bars_up.append({
            "time": f"09:{30 + i // 60}:{(30 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + 0.005, "volume": max(vol, 100),
            "amount": (p + 0.005) * max(vol, 100),
        })

    bars_down = []
    for i in range(65):
        if i < 40:
            p = 10.00 - i * 0.006
            vol = 10000 + i * 50
        else:
            p = 9.76 + (i % 3 - 1) * 0.002
            vol = 4000 - (i - 40) * 100
        bars_down.append({
            "time": f"09:{30 + i // 60}:{(30 + i) % 60:02d}",
            "open": p, "high": p + 0.02, "low": p - 0.02,
            "close": p + (0.001 if i % 2 == 0 else -0.001),
            "volume": max(vol, 100),
            "amount": (p + 0.001) * max(vol, 100),
        })

    params_with_filter = SignalParams(trend_filter_enabled=True)
    params_no_filter = SignalParams(trend_filter_enabled=False)

    # ── 场景1: 趋势过滤关闭 → trend_context=range ──
    sig_unfiltered = evaluate_reduce_signal(
        bars_up, current_price=10.65, prev_close=10.00, params=params_no_filter
    )
    runner.check("趋势过滤关闭 → trend_context=range",
                 sig_unfiltered.trend_context == "range",
                 f"got {sig_unfiltered.trend_context}")

    # ── 场景2: mock trend_up → reduce 阈值 +1 ──
    # 注意：patch at0.strategy 模块（_judge_trend_context 的 __globals__ 指向它）
    original_regime = _strategy_mod.detect_market_regime
    try:
        _strategy_mod.detect_market_regime = lambda snap, **kw: "trend_up"
        sig_up = evaluate_reduce_signal(
            bars_up, current_price=10.65, prev_close=10.00, params=params_with_filter
        )
        runner.check("trend_up → trend_context=trend_up",
                     sig_up.trend_context == "trend_up",
                     f"got {sig_up.trend_context}")
        runner.check("trend_up → trigger_threshold=4（默认3+1）",
                     sig_up.trigger_threshold == 4,
                     f"got {sig_up.trigger_threshold}")

        # ── 场景3: mock extreme → reduce 硬否决 score=0 ──
        _strategy_mod.detect_market_regime = lambda snap, **kw: "extreme"
        sig_extreme = evaluate_reduce_signal(
            bars_up, current_price=10.65, prev_close=10.00, params=params_with_filter
        )
        runner.check("extreme → trend_context=extreme",
                     sig_extreme.trend_context == "extreme",
                     f"got {sig_extreme.trend_context}")
        runner.check("extreme → rules_score=0（硬否决）",
                     sig_extreme.rules_score == 0,
                     f"got {sig_extreme.rules_score}")

        # ── 场景4: mock trend_down → add 阈值 +1 ──
        _strategy_mod.detect_market_regime = lambda snap, **kw: "trend_down"
        sig_down = evaluate_add_signal(
            bars_down, current_price=9.76, prev_close=10.00, params=params_with_filter
        )
        runner.check("trend_down → trend_context=trend_down",
                     sig_down.trend_context == "trend_down",
                     f"got {sig_down.trend_context}")
        runner.check("trend_down → trigger_threshold=4（默认3+1）",
                     sig_down.trigger_threshold == 4,
                     f"got {sig_down.trigger_threshold}")

        # ── 场景5: mock extreme → add 硬否决 score=0 ──
        _strategy_mod.detect_market_regime = lambda snap, **kw: "extreme"
        sig_add_extreme = evaluate_add_signal(
            bars_down, current_price=9.76, prev_close=10.00, params=params_with_filter
        )
        runner.check("add extreme → trend_context=extreme",
                     sig_add_extreme.trend_context == "extreme",
                     f"got {sig_add_extreme.trend_context}")
        runner.check("add extreme → rules_score=0（硬否决）",
                     sig_add_extreme.rules_score == 0,
                     f"got {sig_add_extreme.rules_score}")
    finally:
        _strategy_mod.detect_market_regime = original_regime


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
def main():
    print("=" * 60)
    print("P0 整改模块单元测试 — 成本/配对/T+1/持仓时长/时间边界")
    print("=" * 60)

    runner = TestRunner()

    # CostModel
    test_cost_model_basic(runner)
    test_cost_model_round_trip(runner)
    test_cost_model_scenarios(runner)

    # TradeLifecycle FIFO
    test_fifo_same_day(runner)
    test_fifo_partial_pairing(runner)
    test_fifo_same_direction_no_pair(runner)
    test_fifo_cross_day(runner)
    test_cross_day_holding_bars_continuity(runner)

    # 持仓时长与偏移
    test_max_holding_duration(runner)
    test_max_favorable_adverse(runner)
    test_unrealized_pnl(runner)

    # T+1
    test_t_plus_1_sellable(runner)

    # ExposurePolicy
    test_exposure_policy_time_boundaries(runner)
    test_approve_signal_normal(runner)
    test_approve_signal_14_20_no_new(runner)
    test_approve_signal_14_40_exit_only(runner)
    test_approve_signal_opposite_direction(runner)
    test_approve_signal_t1_sellable(runner)

    # EOD 风险处置
    test_eod_risk_disposal(runner)
    test_eod_risk_disposal_expired(runner)

    # 参数注入
    test_backtest_params_injection(runner)

    # P0-4/P0-8 回归守卫
    test_fifo_cross_day_invariant_guard(runner)
    test_position_split_invariant(runner)
    test_backtest_metrics_consistency(runner)

    # P0-6: regime 检测与趋势过滤
    test_detect_market_regime(runner)
    test_trend_filter_on_signals(runner)

    return runner.summary()


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
