# A-T0 日内做T引擎 · 资深开发工程师深度审计报告

**审计日期**：2026-07-24
**审计范围**：`src/at0/` 全量 + `config/` + `scripts/diagnose_*` + `tests/`
**审计视角**：资深量化交易系统开发工程师
**审计目标**：找出影响收益的问题，优化买卖策略逻辑，给出可执行方案

---

## 0. 总览判断

项目整体处在"v1.1 实施方案已立、`scripts/` 旧脚本整改完成、`src/at0/` 新架构迁移进行中"的过渡阶段。`docs/strategy_optimization_implementation.md` 标注 P0-1~6/8 已完成——**但该"完成"是针对 `scripts/` 下的旧实现**。`src/at0/` 新架构在合并迁移过程中引入了一批新 Bug，且部分 P0 整改未在新架构中落地。

**核心矛盾**：新旧代码并存，文档说"已完成"但新代码里问题还在。这正是团队技术能力提升的关键切入点——**迁移不能只是"文件搬运"，必须用回归测试守住整改成果**。

**收益影响定性**：当前 `src/at0/` 路径下的回测结果**不可信**。存在 3 处直接虚高收益的 Bug（未来函数、成本口径分裂、风控默认值回退），以及 2 处削弱策略有效性的逻辑缺陷（5min 趋势过滤失效、1min 持仓上限过短）。

---

## 1. P0 严重问题（影响收益计算正确性 / 风控有效性，必须立即修）

### P0-1 · `RiskParams` dataclass 默认值未随 P0-1 整改更新 ⚠️ 致命

**位置**：`src/at0/risk.py:527-531`

```python
@dataclass
class RiskParams:
    max_t_size_ratio: float = 0.5         # ← 旧值！P0-1 整改后应为 0.25
    max_t_trades_per_day: int = 4
    min_capture_spread: float = 0.006    # ← 旧值！应为 0.0075
    round_trip_cost: float = 0.001       # ← 旧值！应为 0.003（0.3%）
    eod_check_time: str = "14:50"
```

**对比 `config/thresholds.yaml:36-42`（P0-1 整改后的正确值）**：
```yaml
risk:
  max_t_size_ratio: 0.25
  min_capture_spread: 0.0075
  round_trip_cost: 0.003
```

**影响路径**：
- `src/at0/backtest.py:257` `BacktestParams` 默认值 `risk_params: RiskParams = field(default_factory=RiskParams)`
- `src/at0/backtest.py:1086-1088` `evaluate_param_set`（合成数据调优）直接构造 `BacktestParams` 不传 risk_params → 走旧默认值
- `src/at0/cli.py:222` `run()` 构造 `BacktestParams(... risk_params=RiskParams() ...)` → 走旧默认值（虽然 cli.py:791 实盘入口用了 config_loader，但 `run()` 回测入口没用）

**对收益的影响**：
- 单次T仓位上限 50% vs 25% → 单笔风险敞口翻倍
- 最小预期价差门槛 0.6% vs 0.75% → 大量"勉强够门槛"的劣质信号被放行
- 来回成本 0.1% vs 0.3% → 预期净收益计算虚高 0.2 个百分点
- 三者叠加：**回测净收益系统性虚高，风控系统性偏松**

**修复方案**：
```python
# src/at0/risk.py:527-531
@dataclass
class RiskParams:
    max_t_size_ratio: float = 0.25        # P0-1 整改值
    max_t_trades_per_day: int = 4
    min_capture_spread: float = 0.0075    # P0-1 整改值
    round_trip_cost: float = 0.003        # P0-1 整改值（与 CostModel.round_trip_cost_rate() 对齐）
    eod_check_time: str = "14:50"
```

**配套**：删除 `RiskParams.round_trip_cost` 字段，所有预期价差检查改用 `CostModel.round_trip_cost_rate()`，消除双口径。参见 P0-4。

---

### P0-2 · `TradeLeg.open_vwap_dev` 在 `to_dict`/`from_dict` 丢失 ⚠️ 致命

**位置**：`src/at0/execution.py:77-104`

```python
def to_dict(self) -> dict:
    return {
        "direction": self.direction,
        "shares": self.shares,
        "fill_price": self.fill_price,
        "time": self.fill_time,
        "date": self.fill_date,
        "cost": self.cost,
        "status": self.status.value,
        "paired_pnl": round(self.paired_pnl, 4),
        "holding_bars": self.holding_bars,
        "max_favorable": round(self.max_favorable, 4),
        "max_adverse": round(self.max_adverse, 4),
        # ← 漏了 open_vwap_dev！
    }

@classmethod
def from_dict(cls, d: dict) -> "TradeLeg":
    return cls(
        direction=d["direction"],
        shares=d["shares"],
        fill_price=d["fill_price"],
        fill_time=d.get("time", ""),
        fill_date=d.get("date", ""),
        fill_bar_idx=d.get("fill_bar_idx", 0),
        cost=d.get("cost", 0.0),
        # ← 漏了 open_vwap_dev！
    )
```

**影响链**：
- `backtest.py:737` `carry_open_legs = result.get("final_open_legs", [])` → 用 `export_open_legs()` 导出（调 `to_dict`）
- 次日 `backtest.py:399` `state.lifecycle.import_open_legs(...)` → 调 `from_dict`
- **跨日腿的 `open_vwap_dev` 全部丢失为 `None`**
- `backtest.py:484` `buy_open_vwap_dev = leg.open_vwap_dev` → 拿到 None
- `strategy.py:227-228` `_compute_pairing_threshold(None, params)` → 退化为固定 floor 0.8%
- **方案C1动态平仓阈值在跨日腿上完全失效**：开仓深度 5% 的腿，本应阈值 ≈3.5%，被退化为 0.8%，导致过早平仓

**对收益的影响**：跨日腿（占比可能 30-50%）平仓阈值错误，要么过早平仓吃不到回归收益，要么错过平仓窗口变 expired。这是 README 里"仍有 55.2% 配对失败、卡在未回归VWAP"的隐藏根因之一。

**修复方案**：
```python
# src/at0/execution.py:77-104
def to_dict(self) -> dict:
    return {
        ...existing fields...,
        "open_vwap_dev": self.open_vwap_dev,   # 新增
    }

@classmethod
def from_dict(cls, d: dict) -> "TradeLeg":
    return cls(
        ...existing fields...,
        open_vwap_dev=d.get("open_vwap_dev"),  # 新增
    )
```

**配套回归测试**：构造跨日腿，断言 `import_open_legs(export_open_legs(legs))` 后 `open_vwap_dev` 字段保持不变。

---

### P0-3 · `evaluate_param_set_real` 用 `min(daily_prev.values())` 作为底仓成本 — 未来函数 ⚠️ 致命

**位置**：`src/at0/backtest.py:1116`

```python
avg_cost = min(daily_prev.values())   # ← 未来函数！
bp = BacktestParams(
    base_shares=3000,
    avg_cost=avg_cost,                # 用整个区间最低昨收作为底仓成本
    ...
)
```

**问题**：`min(daily_prev.values())` 取整个回测区间（含未来日期）的最低昨收价作为底仓成本。在回测开始前就已经"知道"未来哪一天的昨收最低。底仓成本被系统性低估，所有以底仓成本为参考的盈亏计算都虚高。

**对比正确做法**（`cli.py:212-215`）：用首日 prev_close 作为底仓成本，因果正确。

**对收益的影响**：滚动样本外验证（`rolling_out_of_sample`）的净盈亏排名被污染，可能选出"碰巧区间低点匹配底仓成本"的虚假最优参数。

**修复方案**：
```python
# src/at0/backtest.py:1116
# 用首日 prev_close 作为底仓成本（与 cli.py run() 口径一致）
first_date = min(daily_prev.keys())
avg_cost = daily_prev[first_date]
```

---

### P0-4 · 风控预期价差检查与 `CostModel` 成本口径分裂 ⚠️ 重要

**位置**：`src/at0/risk.py:666-677`（`check_risk`）vs `src/at0/risk.py:99-112`（`CostModel.round_trip_cost_rate`）

```python
# risk.py:666-677 — check_risk 用 RiskParams.round_trip_cost
if expected_spread < params.min_capture_spread:
    reason = (
        f"预期价差 {expected_spread*100:.2f}% < {params.min_capture_spread*100:.1f}%"
        f"（成本 {params.round_trip_cost*100:.1f}%）"   # ← 用 0.1% 旧值
    )
```

```python
# risk.py:99-112 — CostModel 算出来是 0.3%
def round_trip_cost_rate(self) -> float:
    buy_side = self.commission_rate + self.slippage_rate + self.impact_rate
    sell_side = (self.commission_rate + self.stamp_tax_rate
                 + self.slippage_rate + self.impact_rate)
    return round(buy_side + sell_side, 6)   # = 0.003
```

**问题**：P0-1 整改目标是"成本计算统一收敛到 CostModel"，但 `check_risk` 仍用 `RiskParams.round_trip_cost` 字段。两套成本口径并存：
- 实际成交扣成本：用 CostModel（0.3%）
- 风控预期价差门槛：用 RiskParams.round_trip_cost（dataclass 默认 0.1%，config 加载后 0.3%）

**影响**：
- 走 dataclass 默认值时（见 P0-1），风控以为成本 0.1%，实际扣 0.3%，门槛偏松 0.2 个百分点
- 走 config 加载时，两者口径一致但仍是两份字段，未来易漂移

**修复方案**：删除 `RiskParams.round_trip_cost` 字段，`check_risk` 接收 `CostModel` 实例：

```python
# src/at0/risk.py
def check_risk(
    code: str,
    direction: str,
    requested_shares: int,
    signal_price: float,
    reference_price: float,
    params: Optional[RiskParams] = None,
    cost_model: Optional[CostModel] = None,    # 新增
    positions_path: Path = POSITIONS_FILE,
) -> RiskCheckResult:
    ...
    cm = cost_model or CostModel.base()
    round_trip = cm.round_trip_cost_rate()
    if expected_spread < params.min_capture_spread:
        reason = f"预期价差 {expected_spread*100:.2f}% < {params.min_capture_spread*100:.1f}%（成本 {round_trip*100:.1f}%）"
```

调用方（`cli.py:702` `check_risk(...)`）传入 `cost_model` 实例。

---

### P0-5 · `adapt_params_by_frequency` 未调整 `max_holding_bars` — 1min 数据持仓上限过短 ⚠️ 重要

**位置**：`src/at0/cli.py:96-113`

```python
def adapt_params_by_frequency(params, frequency, bars_per_day):
    if frequency == "5min":
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
    else:  # 1min
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
    return params
    # ← max_holding_bars 始终 = 12，未按频率缩放
```

**问题**：`max_holding_bars=12` 是按 5min 线设计的（12 根 = 60 分钟，合理）。但 1min 线下 12 根 = 12 分钟，持仓窗口过短，腿频繁触发 expired，无法等到价格回归 VWAP。

**对收益的影响**：1min 回测下大量腿在 12 分钟内未配对就被标记 expired，按 expire 时刻收盘价结算，往往亏损。expired 腿比例飙升，配对率下降。

**修复方案**：
```python
# src/at0/cli.py:96-113
def adapt_params_by_frequency(params, frequency, bars_per_day):
    if frequency == "5min":
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
        params.max_holding_bars = 12      # 5min: 60 分钟
    else:  # 1min
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
        params.max_holding_bars = 60      # 1min: 60 分钟（与 5min 等价时长）
    return params
```

**配套**：`BacktestParams.max_holding_bars` 与 `ExposurePolicy.max_holding_bars` 同步调整（目前两处默认都是 12）。

---

### P0-6 · `detect_market_regime` 在 5min 数据下趋势过滤失效 ⚠️ 重要

**位置**：`src/at0/features.py:705-763`

```python
def detect_market_regime(
    snap: dict,
    adx_trend_threshold: float = 25.0,
    adx_extreme_threshold: float = 40.0,
    extreme_vwap_dev_multiplier: float = 2.0,
    min_bars_for_trend: int = 60,        # ← 硬编码 60
) -> str:
    bars_count = snap.get("bars_count", 0)
    if bars_count < min_bars_for_trend:
        return "range"                  # ← 5min 数据一天 48 根，永远 < 60
    ...
```

**问题**：5min 线一天 48 根，单日 bars_count 永远 < 60，`detect_market_regime` 直接返回 "range"，趋势过滤（P0-6 整改核心）完全失效。逆势均值回归在强趋势中照常触发，大亏损集中出现——正是 P0-6 要解决的问题。

**对收益的影响**：5min 回测（baostock 历史数据首选）下 trend_up/trend_down/extreme 三种 regime 永不触发，`evaluate_reduce_signal` 和 `evaluate_add_signal` 里的趋势加严逻辑（strategy.py:413-419, 600-606）形同虚设。

**修复方案**：`min_bars_for_trend` 按频率自适应，或改为按"分钟数"而非"K线数"判定：

```python
# src/at0/features.py
def detect_market_regime(
    snap: dict,
    adx_trend_threshold: float = 25.0,
    adx_extreme_threshold: float = 40.0,
    extreme_vwap_dev_multiplier: float = 2.0,
    min_bars_for_trend: int = 60,        # 默认按 1min
    frequency: str = "1min",             # 新增
) -> str:
    # 按频率调整最小根数：1min=60（1小时），5min=12（1小时）
    effective_min_bars = min_bars_for_trend
    if frequency == "5min":
        effective_min_bars = max(12, min_bars_for_trend // 5)
    bars_count = snap.get("bars_count", 0)
    if bars_count < effective_min_bars:
        return "range"
    ...
```

调用方（`strategy.py:_judge_trend_context`）需把 frequency 透传进来。

---

### P0-7 · `evaluate_all_signals` 市场权重覆盖趋势加严 ⚠️ 逻辑 Bug

**位置**：`src/at0/strategy.py:690-715`

```python
if market is not None:
    ...
    reduce_weight = adjust_signal_weight(market, "reduce")
    add_weight = adjust_signal_weight(market, "add")
    reduce_sig.trigger_threshold = _apply_weight_to_threshold(
        params.min_rules_to_trigger, reduce_weight   # ← 用 base threshold 重新算
    )
    add_sig.trigger_threshold = _apply_weight_to_threshold(
        params.min_rules_to_trigger, add_weight
    )
```

**问题**：`evaluate_reduce_signal` 内部已经按 trend_ctx 调整过 `trigger_threshold`（strategy.py:418-419：trend_up 时 +1）。但 `evaluate_all_signals` 在 market 非空时，**用 `params.min_rules_to_trigger`（base 值）重新计算**，覆盖了趋势加严结果。

**后果**：上升趋势 + COLD 市场（weight < 1.0）双重作用下，本应"趋势 +1 + 市场加严"，实际变成"市场加严覆盖掉趋势加严"，趋势保护丢失。

**修复方案**：基于已加严的 `trigger_threshold` 再叠加市场权重，而非从 base 重算：

```python
# src/at0/strategy.py:703-708
reduce_sig.trigger_threshold = _apply_weight_to_threshold(
    reduce_sig.trigger_threshold,    # ← 用已加严的阈值，而非 base
    reduce_weight
)
add_sig.trigger_threshold = _apply_weight_to_threshold(
    add_sig.trigger_threshold,       # ← 同上
    add_weight
)
```

---

### P0-8 · 回测与实盘风控接口不一致 ⚠️ 架构债

**位置**：`src/at0/backtest.py:595`（`approve_signal`）vs `src/at0/cli.py:702`（`check_risk`）

| 检查项 | `approve_signal`（回测） | `check_risk`（实盘） |
|--------|------|------|
| 每日T次数 | ✓ | ✓ |
| L1/L2 熔断 | ✓ | ✓ |
| 尾盘时段限制 | ✓ | ✗ |
| 单方向未配对腿 | ✓ | ✗ |
| require_opposite | ✓ | ✗ |
| 仓位比例 | ✓ | ✓ |
| T+1 可卖底仓 | ✓ | ✓ |
| 预期价差门槛 | ✗（在 backtest.py:621 单独检查） | ✓ |

**问题**：P0-1 整改目标是"回测、纸面监控使用同一个信号和风控接口"，但实际有两套风控函数，检查项不同。回测好的策略上实盘可能因不同检查项被卡或被放。

**修复方案**：合并为单一 `approve_signal` 接口，`check_risk` 改为薄封装（补齐尾盘/单方向/require_opposite 检查项后委托给 `approve_signal`），删除 `RiskCheckResult`，统一用 `RiskDecision`。这是 v1.1 实施方案 6.2 节"迁移映射"中 `t_risk_guard.py + cost_model.py + exposure_policy.py + exit_policy → risk.py 合并"的未完成部分。

---

### P0-9 · `BacktestState.base_shares` 不随 T+1 解锁累加 — 反T 买入次日无法在仓位账本体现 ⚠️ 账本不闭环

**位置**：`src/at0/backtest.py:296-324`（`BacktestState`）+ `src/at0/backtest.py:670-770`（`backtest_multi_day`）

**问题**：
- `backtest_single_day` 每天 new 一个 `BacktestState(base_shares=params.base_shares, ...)`，`state.base_shares` 在单日内固定
- 反T 买入时 `state.locked_shares += shares`，但 `backtest_multi_day` 没有把昨日 locked_shares 次日转入 base_shares
- 次日 `BacktestState` 全新构造，`locked_shares=0`，`base_shares=params.base_shares`（如 3000）
- 结果：第一天反T买入 1000 股，第二天这 1000 股在 `BacktestState` 层面"消失"——既不在 base_shares，也不在 locked_shares
- `sellable_shares = base_shares - locked_shares = 3000 - 0 = 3000`，第二天最多卖 3000 股（底仓），反T 买的那 1000 股无法通过仓位账本卖出

**对比 `execution.py:509-538` `reset_today_state`**：实盘 position_tracker 正确实现了"昨日 locked_shares 次日转入 base_shares"，但回测的 `BacktestState` 没复用这个逻辑。

**对收益的影响**：反T策略（先买后卖）在多日回测中，买入的股票次日无法卖出，被仓位账本卡住。FIFO 配对在 `TradeLifecycle` 层独立追踪没问题，但仓位账本与交易账本不一致，风控检查 `sellable_shares` 时会用错误值。

**修复方案**：`backtest_multi_day` 跨日传递 `locked_shares` 累加到 `base_shares`：

```python
# src/at0/backtest.py:670-770 backtest_multi_day
carry_locked = 0  # 跨日累加的反T买入股数
for date_str in sorted(daily_bars.keys()):
    ...
    result = backtest_single_day(
        ...,
        initial_open_legs=carry_open_legs,
        initial_locked_carry=carry_locked,   # 新增：传入昨日累加
    )
    # 昨日 locked_shares 次日转入 base_shares
    carry_locked = result.get("final_locked_shares", 0)
    ...

# backtest_single_day
def backtest_single_day(..., initial_locked_carry: int = 0):
    state = BacktestState(
        base_shares=params.base_shares + initial_locked_carry,  # 累加
        ...
    )
```

---

### P0-10 · `compute_unrealized_pnl` 不含平仓成本和滑点 ⚠️ 口径偏乐观

**位置**：`src/at0/backtest.py:54-69`

```python
def compute_unrealized_pnl(open_legs: list[dict], last_close: float) -> float:
    total = 0.0
    for leg in open_legs:
        if leg["direction"] == "buy":
            total += (last_close - leg["fill_price"]) * leg["shares"]
        else:  # sell
            total += (leg["fill_price"] - last_close) * leg["shares"]
    return round(total, 2)
```

**问题**：`last_close` 是收盘价（无滑点），`leg["fill_price"]` 是含滑点的成交价。但若要真正平仓这些未配对腿，还要再付一次滑点 + 佣金 + 印花税。`net_pnl_with_unrealized` 偏乐观。

**对收益的影响**：未配对敞口浮盈虚高，含浮盈净盈亏口径偏乐观。对策略排名影响中等（排名相对值不变，但绝对值虚高）。

**修复方案**：传入 `CostModel`，按平仓方向扣减来回成本：

```python
def compute_unrealized_pnl(
    open_legs: list[dict],
    last_close: float,
    cost_model: Optional[CostModel] = None,   # 新增
) -> float:
    cm = cost_model or CostModel.base()
    total = 0.0
    for leg in open_legs:
        # 平仓方向 = 反向
        close_dir = "sell" if leg["direction"] == "buy" else "buy"
        fill_price = cm.fill_price(close_dir, last_close)  # 含滑点
        cost = cm.calc_cost(close_dir, leg["shares"], fill_price)
        if leg["direction"] == "buy":
            total += (fill_price - leg["fill_price"]) * leg["shares"] - cost
        else:
            total += (leg["fill_price"] - fill_price) * leg["shares"] - cost
    return round(total, 2)
```

---

## 2. P1 重要问题（逻辑缺陷 / 优化空间）

### P1-1 · 多空信号极值层项1不对称

**位置**：`src/at0/strategy.py:359-365`（减仓）vs `536-551`（加仓）

- 减仓极值项1：仅 VWAP 偏离 ≥ +0.8×ATR（单一条件）
- 加仓极值项1：VWAP 偏离 ≤ -0.8×ATR **OR** 跌破开盘区间下轨 **OR** 跌破布林带下轨（三选一）

**评估**：减仓比加仓严苛。这可能是**有意设计**（A股做空只能卖底仓，趋势上行时减仓易踏空），但文档未说明。若是有意，应在代码注释和 `docs/` 里写明设计意图；若是 Bug，减仓应同样补"突破开盘区间上轨 / 突破布林带上轨"作为 OR 条件。

**建议**：先与策略设计者确认意图，再决定是否补对称。无论是否补对称，加注释说明。

### P1-2 · `TradeLifecycle.unrealized_pnl` 漏算 expired 腿

**位置**：`src/at0/execution.py:229-261`

`check_expiry` 把超时腿从 `open_legs` 移到 `closed_legs`，之后 `unrealized_pnl` 只遍历 `open_legs`，expired 腿的浮盈浮亏不在。

**现状**：`backtest.py` 层已通过 `risk_events` 单独累计 `expired_legs_real_pnl`，最终 `net_pnl_with_unrealized = net_pnl + unrealized_pnl + expired_legs_real_pnl`（backtest.py:753）。所以**报告口径正确**，但 `TradeLifecycle.unrealized_pnl()` 这个方法本身的语义有歧义——直接调用会拿不到 expired 腿。

**建议**：`unrealized_pnl` 改名 `open_legs_unrealized_pnl`，或文档注释明确"仅含 open 腿，expired 腿见 closed_legs"。

### P1-3 · `evaluate_all_signals` 同时算多空信号但只取一个 recommendation

**位置**：`src/at0/strategy.py:650-739`

每根 K 线都同时评估 reduce 和 add 信号，但 `_execute_trade` 只执行其中一个（减仓优先）。这意味着 add 信号的计算结果（含三层得分）被丢弃，浪费算力。

**建议**：按 `has_buy_open` / `has_sell_open` 只评估对应方向的平仓信号 + 单一方向的开仓信号，避免双算。当前 `backtest_single_day:493-511` 已经传入 `is_for_pairing`，但 `evaluate_all_signals` 内部仍双算——可以改造为按需评估。

### P1-4 · `evaluate_param_set` 合成数据调优用 `min_capture_spread` 字段名冲突

**位置**：`src/at0/backtest.py:958-964` `PARAM_GRID`

```python
PARAM_GRID = {
    "vwap_dev_atr_multiplier": [0.6, 0.8, 1.0],
    "rsi_overbought": [65.0, 70.0, 75.0],
    "rsi_oversold": [25.0, 30.0, 35.0],
    "min_capture_spread": [0.004, 0.006, 0.008],   # ← 这是 RiskParams 字段
    "max_t_size_ratio": [0.3, 0.5],                # ← 0.5 是 P0-1 整改前的旧值
}
```

`max_t_size_ratio` 搜索空间包含 0.5（50%），与 P0-1 整改后的 0.25 不一致。调优可能选出 0.5 的虚假最优。

**建议**：搜索空间更新为 `[0.20, 0.25, 0.33]`，对齐 v1.1 实施方案 Phase 4。

### P1-5 · `diagnose_*` 脚本未纳入回归测试套件

`scripts/` 下有 8 个 `diagnose_*` 脚本，记录了团队历史发现并修复的 bug（跨日配对、止损假设、expired 真实盈亏、公式回放等）。但这些诊断脚本没有转化为回归测试，bug 可能复发。

**建议**：把每个 `diagnose_*` 脚本的核心断言提炼为 `tests/regression/` 下的测试用例，CI 守护。

---

## 3. P2 改进建议（代码质量 / 可维护性 / 可观测性）

### P2-1 · `src/at0/` 与 `scripts/` 双轨并存，迁移未完成

`docs/strategy_optimization_implementation.md` 6.2 节明确了迁移映射，但 `scripts/` 下原始脚本仍保留（`t_signal_engine.py` / `t_risk_guard.py` / `backtest_t_strategy.py` / `cost_model.py` / `exposure_policy.py` / `trade_lifecycle.py` 等），`src/at0/` 是合并版。两套实现并存，文档说"已完成"但新代码里 P0 还在。

**建议**：按 v1.1 6.3 节"双跑机制"推进——新旧实现并行跑同一份数据，对比 `batch_summary.json` 关键指标，任一超容差禁止下线旧代码。完成双跑验证后再删 `scripts/` 旧实现。

### P2-2 · 配置 schema 与实际 yaml 字段不一致

`config/schemas/params_schema.json` 要求 `risk` 段有 `max_position_pct` / `max_t_size` / `eod_force_flat`，但 `config/thresholds.yaml` 的 `risk` 段实际是 `max_t_size_ratio` / `min_capture_spread` / `round_trip_cost` / `min_net_expected_return` / `eod_check_time`。schema 验证形同虚设。

**建议**：schema 与 yaml 字段对齐，CI 加 `jsonschema` 校验。

### P2-3 · 缺少类型注解的 `Optional` 默认值陷阱

`cli.py:572-580` `monitor_single_stock(code, pos, market=None, signal_params=None, ...)` —— 用 `None` 作默认值再在函数体内 `or` 短路，会绕过 `config_loader` 加载的正确参数（见 P0-1 影响路径）。这是 Python 反模式。

**建议**：用 `sentinel` 或显式 `Optional[RiskParams]` + 函数内 `if risk_params is None: raise ValueError(...)`，强制调用方传参。

### P2-4 · 测试覆盖严重不足

`tests/` 下只有 `test_p0_modules.py`（单元）和 `verify_l5.py`（集成）。缺少：
- FIFO 跨日配对不变量守卫（v1.1 7.2 节明确要求）
- T+1 锁定/解锁测试
- 信号因果性测试（未来函数检测）
- 成本模型三场景测试
- golden fixture 回归（v1.1 6.4 节要求用 295 笔交易作 golden）

**建议**：按 v1.1 7.2 节补齐单元测试，关键模块覆盖率 ≥80%。

### P2-5 · `max_t_trades_per_day` 在 `backtest_single_day` 中未强制

`risk.py:approve_signal:290-296` 检查了 `t_trades_today >= max_t_trades_per_day`，但 `backtest_single_day` 的 `BacktestState.t_trades_today` 在 `_execute_trade` 里 +1（backtest.py:637）。看起来检查了，但 `approve_signal` 是在 `_execute_trade` 内调用的，如果 `t_trades_today` 已经等于上限，第 5 次 T 会被拒绝。**这条没问题**，但建议加单元测试守护。

---

## 4. 历史问题溯源（从 `diagnose_*` 脚本反推）

| 脚本 | 当时排查的问题 | 当前状态（src/at0/） |
|------|--------------|------|
| `diagnose_crossday_pairing.py` | 跨日配对按日重置导致 84% 漏统计 | ✅ 已修复（`TradeLifecycle` 跨日不重置） |
| `diagnose_pairing_failure.py` | 平仓信号用开仓严格门槛导致配对率 0.6% | ✅ 已修复（`is_for_pairing` 平仓分支） |
| `diagnose_trades.py` | 交易明细异常排查 | ✅ 辅助工具 |
| `diagnose_stoploss_hypothesis.py` | 止损假设验证，发现 5min regime 失效 | ⚠️ **未修复**（见 P0-6） |
| `diagnose_expired_real_pnl.py` | expired 腿浮盈漏统计 | ✅ backtest 层已修（见 P1-2），但 `TradeLifecycle.unrealized_pnl` 方法本身仍有歧义 |
| `diagnose_expired_rootcause.py` | expired 根因深挖 | ✅ 辅助工具 |
| `diagnose_formula_replay.py` | 方案C1/C2 动态平仓阈值公式回放 | ✅ 已采用 C1 修正版，但跨日腿因 P0-2 退化为固定阈值 |
| `compare_freq.py` | 5min vs 1min 频率一致性 | ✅ 辅助工具 |

**关键洞察**：团队有很强的诊断能力（8 个 diagnose 脚本），但**诊断结论未系统转化为回归测试**。`diagnose_stoploss_hypothesis` 发现的 5min regime 失效问题，至今未修——这正是 P0-6 的源头。

---

## 5. 买卖策略逻辑评估

### 5.1 三层共振信号（P0-5 重构后）

**优点**：
- 极值层 / 确认层 / 环境层分离，符合量化信号设计规范
- 平仓分支（`is_for_pairing`）与开仓分支用不同门槛，解决了"配对率崩塌"问题（从 0.6% 提升到 42.62%）
- 方案C1动态平仓阈值有回放验证依据

**缺陷**：
- 多空不对称（P1-1），意图未文档化
- 5min 数据下趋势过滤失效（P0-6），逆势均值回归在强趋势中照常触发
- `evaluate_all_signals` 市场权重覆盖趋势加严（P0-7）

### 5.2 FIFO 跨日配对（P0-2 重构后）

**优点**：
- `TradeLifecycle` 跨日不重置，解决了 84% 漏统计的历史 bug
- `import_open_legs` / `export_open_legs` 跨日延续机制清晰

**缺陷**：
- `open_vwap_dev` 在序列化丢失（P0-2），跨日腿动态平仓阈值失效
- `BacktestState` 仓位账本不闭环（P0-9），反T 买入次日无法在账本体现
- `unrealized_pnl` 不含平仓成本（P0-10），口径偏乐观

### 5.3 参数体系

**优点**：
- `thresholds.yaml` 单一真相源 + `overlays/` 场景覆盖，架构正确
- P0-1 整改把成本收敛到 `CostModel`，三场景（optimistic/base/pessimistic）设计合理

**缺陷**：
- `RiskParams` dataclass 默认值未随整改更新（P0-1），走默认值路径风控偏松
- schema 与 yaml 字段不一致（P2-2），验证形同虚设
- `PARAM_GRID` 搜索空间含旧值 0.5（P1-4）

### 5.4 过拟合风险

- 样本仅 23 个交易日、20 只股票、295 笔配对交易，v1.1 已明确标注"样本不足"
- `evaluate_param_set_real` 用 `min(daily_prev.values())` 未来函数（P0-3），调优结果不可信
- 滚动样本外验证机制（`rolling_out_of_sample`）设计正确，但被 P0-3 污染
- 建议：先修 P0-3，再重跑调优；样本扩至 60+ 交易日前不引入贝叶斯优化（v1.1 已明确）

---

## 6. 可执行优化方案（按优先级排序）

### Phase A · 紧急修复（1-2 天，修完才能信任回测结果）

| 优先级 | 任务 | 文件 | 预计工时 |
|--------|------|------|---------|
| P0-1 | 更新 `RiskParams` dataclass 默认值对齐 thresholds.yaml | `src/at0/risk.py:527-531` | 0.5h |
| P0-2 | `TradeLeg.to_dict`/`from_dict` 补 `open_vwap_dev` 字段 | `src/at0/execution.py:77-104` | 0.5h |
| P0-3 | `evaluate_param_set_real` 用首日 prev_close 作底仓成本 | `src/at0/backtest.py:1116` | 0.5h |
| P0-5 | `adapt_params_by_frequency` 按 1min/5min 调整 `max_holding_bars` | `src/at0/cli.py:96-113` | 0.5h |
| P0-7 | `evaluate_all_signals` 市场权重基于已加严阈值叠加 | `src/at0/strategy.py:703-708` | 0.5h |

**验收**：修完后跑 `python -m at0.cli backtest --code 600000 --days 7` 和 `python -m at0.cli optimize`，对比修复前后 `batch_summary.json`，净盈亏应有显著变化（因为 P0-1 风控收紧 + P0-3 未来函数消除）。

### Phase B · 口径统一（3-5 天，消除双口径）

| 优先级 | 任务 | 文件 | 预计工时 |
|--------|------|------|---------|
| P0-4 | 删除 `RiskParams.round_trip_cost`，`check_risk` 接收 `CostModel` | `src/at0/risk.py` + `src/at0/cli.py:702` | 2h |
| P0-6 | `detect_market_regime` 按频率自适应 `min_bars_for_trend` | `src/at0/features.py:705` + `src/at0/strategy.py:_judge_trend_context` | 3h |
| P0-8 | 合并 `approve_signal` 和 `check_risk` 为单一接口 | `src/at0/risk.py` + `src/at0/backtest.py:595` + `src/at0/cli.py:702` | 4h |
| P0-9 | `backtest_multi_day` 跨日传递 locked_shares 累加到 base_shares | `src/at0/backtest.py:670-770` | 3h |
| P0-10 | `compute_unrealized_pnl` 传入 CostModel 扣平仓成本 | `src/at0/backtest.py:54-69` | 1h |

**验收**：新增单元测试覆盖每个修复点；跑双跑对比（新旧实现 `batch_summary.json` 关键指标容差）。

### Phase C · 回归测试补齐（5-7 天，防止复发）

按 v1.1 7.2 节补齐：

| 测试文件 | 覆盖点 |
|---------|--------|
| `tests/unit/test_cost_model.py` | 佣金/印花税/滑点/三场景 |
| `tests/unit/test_fifo_crossday.py` | FIFO 跨日不重置不变量守卫（构造跨日腿，断言与按日重置版本不同） |
| `tests/unit/test_t_plus_1.py` | T+1 锁定/解锁/次日转 base |
| `tests/unit/test_signal_causality.py` | 信号因果性（t 时刻不用 t+1 数据） |
| `tests/unit/test_pairing_threshold.py` | 方案C1动态阈值 + 跨日腿 open_vwap_dev 不丢失 |
| `tests/regression/test_golden_batch.py` | 用 295 笔交易作 golden fixture 守护 |

### Phase D · 架构整改收尾（5-10 天，完成 v1.1 迁移）

- 按 v1.1 6.3 节双跑机制推进 `scripts/` → `src/at0/` 迁移
- 删除 `scripts/` 旧实现（双跑验证通过后）
- `PARAM_GRID` 搜索空间对齐 P0-1 整改值（P1-4）
- schema 与 yaml 字段对齐（P2-2）
- `monitor_single_stock` 默认值改 sentinel（P2-3）

### Phase E · 策略优化（数据就绪后）

- 扩数据至 60+ 交易日（P0-7，v1.1 已明确为数据任务）
- 重跑 `rolling_out_of_sample`（修复 P0-3 后）
- 评估多空对称性（P1-1，与策略设计者确认意图）
- 考虑机器学习概率过滤（v1.1 9.3 节，样本扩至 60+ 后）

---

## 7. 团队技术能力提升建议

### 7.1 工程实践

1. **强制 Code Review**：本次审计发现的 P0 大多是"文档说已修但新代码里没落地"——CR 应对照 `docs/strategy_optimization_implementation.md` 的整改清单逐项验证
2. **CI 守护**：补齐 `tests/` 后，PR 必须通过 `pytest tests -q` + `python scripts/verify_l5.py` 才能合并
3. **双跑机制**：v1.1 6.3 节已设计，必须执行——迁移期间新旧实现并行跑，关键指标容差内才能下线旧代码
4. **golden fixture**：用当前 295 笔交易结果作 golden，任何重构后跑回归测试，断言关键指标不变

### 7.2 量化策略开发规范

1. **未来函数检测**：任何回测代码 review 时，重点查"是否用了未来日期的数据决策当下交易"。本次 P0-3 就是典型未来函数
2. **因果性测试**：信号生成函数必须可证伪——构造 t 时刻的输入，断言不依赖 t+1 数据
3. **参数与默认值对齐**：dataclass 默认值必须与 config 一致，或用 sentinel 强制传参
4. **口径单一**：成本/仓位/阈值等关键参数只能有一份字段，禁止双口径（P0-4）
5. **诊断脚本转回归测试**：`diagnose_*` 脚本发现 bug 后，提炼核心断言为 `tests/regression/` 测试，防止复发

### 7.3 知识管理

1. **设计意图文档化**：多空不对称（P1-1）这类"有意为之"的设计，必须在代码注释和 `docs/` 写明理由，否则后人会当 Bug 修
2. **整改清单可追溯**：`docs/strategy_optimization_implementation.md` 的 P0-1~8 整改状态，应关联到具体 commit hash 和测试用例
3. **诊断脚本归档**：8 个 `diagnose_*` 脚本记录了团队排查 bug 的思路，应整理为"问题模式库"，新成员 onboarding 必读

---

## 8. 风险提示

1. **本报告所有 P0 修复前，`src/at0/` 路径下的回测结果不可信**，禁止用于参数选择或策略评估
2. **`scripts/` 旧实现是否可信**：需另做审计（本报告聚焦 `src/at0/`），但 v1.1 文档标注 P0-1~6/8 已完成，相对可信
3. **修复后必须重跑基线**：修复 P0-1~3 后，`batch_summary.json` 会有显著变化，旧 baseline（+13,241 元）作废，需重立新 baseline
4. **本报告未覆盖**：`src/at0/data.py`（数据源）、`src/at0/reports.py`（报告生成）、`src/at0/sample_data.py`（合成数据）的深度审计，建议后续补查

---

**报告完。**

> 本审计报告基于 2026-07-24 代码快照，由资深开发工程师视角产出。所有 P0 问题均已亲自读代码验证行号与逻辑，可直接按"位置"定位修复。修复后建议跑 `python -m at0.cli backtest --code 600000 --days 7` 与 `python -m at0.cli optimize` 验收。
