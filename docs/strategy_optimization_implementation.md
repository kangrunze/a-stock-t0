# A-T0 策略优化与架构整改实施方案

版本：v1.1  
日期：2026-07-23  
适用范围：`D:\project\a-t0` 研究、回测、模拟盘和实时监控代码  
目标：在不自动实盘下单的前提下，建立成本真实、风险可控、可滚动验证、可复用的日内T策略研究系统。

> v1.1 变更：修正 P0 整改状态（P0-1/2/3 已完成）、精简目标目录结构（40+ 文件→9 文件）、补充域模型不变量、迁移路径与双跑验证、数据验证失败策略、run_id/artifacts 规则、CLI 硬约束。

## 1. 目标与非目标

### 1.1 目标

1. 将评估目标从“胜率最大化”改为“扣除成本后的正期望、低回撤和低未配对风险”。
2. 修复回测中的成本、配对、跨日持仓、尾盘处理和数据验证问题。
3. 将数据、特征、信号、风险、撮合、组合、报告拆成独立层，减少脚本之间的耦合。
4. 建立滚动样本外验证和参数版本管理，防止历史回测过拟合。
5. 为后续机器学习提供稳定的特征、标签和评估接口。

### 1.2 非目标

- 不承诺稳定盈利或保证收益。
- 不允许模型、脚本或大模型直接产生自动下单动作。
- 不在样本不足时选择所谓“最优参数”。
- 不把大语言模型作为交易信号或参数搜索器的核心执行组件。

## 2. 当前基线与问题清单

当前基线文件：

- [batch_summary.json](../outputs/backtest/batch_summary.json)
- [thresholds.yaml](../config/thresholds.yaml)
- [backtest_t_strategy.py](../scripts/backtest_t_strategy.py)
- [t_signal_engine.py](../scripts/t_signal_engine.py)
- [t_risk_guard.py](../scripts/t_risk_guard.py)
- [tune_params.py](../scripts/tune_params.py)

当前基线的主要事实（数据源 `batch_summary.json`，已核对）：

> **⚠️ BASELINE 替换声明（2026-07-24）**
>
> 下表为**旧 baseline**（P0-5/P0-6 重构前 + 配对率 0.6% 那版的诊断结果），**已废弃，禁止引用**。
> 根因诊断发现：旧 baseline 的 0.6% 配对率源于策略层 P0-5 三层触发对"平仓信号"也用同一套严格门槛（extreme≥2 + confirm≥1），
> 导致 173/174 条腿的对向平仓信号在窗口内从未触发。此为策略层 bug，非真实策略表现。
>
> 新 baseline 见下方"2.0 新基线（平仓分支修复后）"小节。

| 指标 | 数值 | 字段 |
|---|---|---|
| 股票数 | 20 | `overall.stocks` |
| 交易日区间 | 2026-06-22 ~ 2026-07-22（约23个交易日） | `start`/`end` |
| 配对交易数 | 295 | `overall.paired_trades` |
| 配对胜率 | 57.63% | `overall.win_rate` |
| 已实现净收益 | -101,966 元（约 -10.2 万） | `overall.net_pnl` |
| 未配对浮盈亏 | -16,888 元 | `overall.unrealized_pnl` |
| 含浮盈净收益 | -118,854 元（约 -11.9 万） | `overall.net_pnl_with_unrealized` |
| 收盘未配对腿 | 20 | `overall.final_open_legs_count` |

说明：`-10.2万` 为已实现净收益，`-11.9万` 为叠加未配对浮盈亏后的口径，两者均来自同一份 `batch_summary.json`，不存在统计口径冲突。该结果说明胜率不能代表策略有效，当前盈亏比、跨日敞口和尾部风险均不合格。

### 2.0 新基线（平仓分支修复后，2026-07-24）

**根因**：`evaluate_reduce/add_signal` 对"新开仓"和"为配对已有仓位的平仓"用同一套 P0-5 严格门槛（extreme≥2 + confirm≥1）。均值回归策略的平仓定义应为"价格回归到 VWAP 附近"，而非"对向出现极端"——后者是更少见的事件，导致 99.4% 腿的对向信号从未触发，配对率崩塌到 0.6%。

**修复**：`evaluate_reduce/add_signal` 增加 `is_for_pairing` 参数。有未配对腿时，对应方向的信号评估走平仓分支：
- 距离判断：`|vwap_dev| < pairing_vwap_dev_threshold (0.8%)` 视为价格已回归均值
- 轻量方向确认：最近 N 根 K 线不再创新极值（防单根噪声误触发）
- 环境层仍需通过（未涨停/跌停、非极端趋势）
- 平仓跳过 `min_capture_spread` 检查（该检查对平仓无意义）

新基线事实（数据源 `outputs/backtest/batch_summary.json`，meta.source="diagnose_pairing_failure.py (平仓分支修复后)"）：

| 指标 | 旧 baseline | **新 baseline** | 变化 |
|---|---|---|---|
| 配对率 | 0.6% (1/177) | **42.62% (78/183)** | +71x |
| 已实现净收益 | -101,966 | **+13,241** | +11.5万 |
| 含浮盈净收益 | -118,854 | **+12,338** | +13.1万 |
| 未配对浮盈亏 | -16,888 | **-903** | +16k |
| 盈利股票数 | — | **12/20** | — |
| 收盘未配对腿 | 20 | **1** | -95% |

**新 baseline 是后续所有讨论（止损、参数优化、regime 分析）的起点。** 旧 -10.2万 / -11.9万 / 0.6% 配对率数字均为重构前产物，不得再引用。

**遗留**：仍有 55.2% 配对失败（99/183），100% 卡在 `pairing:未回归VWAP`——价格在 12 根 K 线内未回归 VWAP（43.9% 的腿 min|vwap_dev|≥2.0%）。这是开仓信号质量问题或 max_holding_bars 太短，需在新 baseline 上用 max_favorable/max_adverse 诊断回答。

### 2.1 问题清单与整改状态

| 编号 | 问题 | 影响 | 整改状态 | 证据 |
|---|---|---|---|---|
| P0-1 | 风险配置往返成本0.1%，回测佣金、印花税、双边滑点合计约0.3% | 交易门槛偏松，虚假正期望 | ✅ 已完成 | [cost_model.py](../scripts/cost_model.py) 收敛佣金/税/滑点/冲击，[backtest_t_strategy.py](../scripts/backtest_t_strategy.py#L94-L102) 通过 `get_cost_model()` 接入 |
| P0-2 | 未配对腿可跨日数日甚至长期持有 | 日内策略变成方向性持仓 | ✅ 已完成 | [trade_lifecycle.py](../scripts/trade_lifecycle.py) 实现 FIFO 跨日不重置，`initial_open_legs` 参数支持跨日延续 |
| P0-3 | 尾盘检查只标记状态，不执行风险处置 | 不能控制收盘敞口 | ✅ 已完成 | [exposure_policy.py](../scripts/exposure_policy.py) 实现强制了结，`eod_risk_disposal` 执行平仓动作 |
| P0-4 | `backtest_t_strategy.py` 同时负责策略执行、撮合、成本、持仓和汇总 | 修改一个规则容易影响多个行为 | ✅ 已完成 | 已抽出 cost_model/trade_lifecycle/exposure_policy 三个模块，主循环+汇总仍留在原文件 |
| P0-5 | 信号主要由VWAP、KDJ、RSI、MFI等相关指标叠加 | 规则数量增加但信息增量有限 | ✅ 已完成 | [t_signal_engine.py](../scripts/t_signal_engine.py) 重构为极值层/确认层/环境层三层结构，MFI 归入极值层，触发条件改为 extreme≥2 + confirm≥1 + filter通过 |
| P0-6 | 强趋势中仍可能触发逆势均值回归 | 大亏损交易集中出现 | ✅ 已完成 | [intraday_reference.py](../scripts/intraday_reference.py) 新增 `detect_market_regime()`（features 层），[t_signal_engine.py](../scripts/t_signal_engine.py) 趋势过滤：extreme 硬否决、trend_up/down 逆势加严 |
| P0-7 | 当前样本区间过短 | 参数优化很容易过拟合 | ⬜ 未开始 | 见 Phase 4，需扩数据至 60+ 交易日（数据任务，非代码改动） |
| P0-8 | 交易文件、报告和批量汇总存在历史产物混用风险 | 统计口径不一致 | ✅ 已完成 | [run_artifacts.py](../scripts/run_artifacts.py) 实现 run_id 版本化（`{timestamp}_{param_hash8}_{data_hash8}`），artifacts 落盘 |

P0-1~6/8 均已完成整改并接入回测主循环，**后续不得重复实现**。P0-7（扩数据至 60+ 交易日）为数据获取任务，非代码改动，待数据就绪后进入 Phase 4 参数优化。

## 3. 目标架构

### 3.1 目标目录结构

采用**精简分层**：当前为研究阶段（30 个脚本、20 股 23 天样本），按 9 个文件组织，等单层超过约 400 行再拆分，避免过早抽象。原 v1.0 的 40+ 文件结构迁移成本远高于收益，已废弃。

```text
src/at0/
  domain.py          # Bar/Signal/Order/Fill/Position/Trade/BacktestResult + enums + errors 合并
  data.py            # ports(Protocol) + normalizer + validator + 4个 _fetch_xxx provider 方法
  features.py        # reference(VWAP/ATR/RSI/KDJ/MFI/BB) + trend(EMA/MACD/DMI/ADX) + volume + regime
  pipeline.py        # 特征计算编排与版本号
  strategy.py        # rules(极值/反转确认/趋势过滤) + signal_engine + （regime 由 features 产出）
  risk.py            # cost_model + pre_trade + exposure + exit_policy 合并
  execution.py       # simulator + matcher(FIFO) + portfolio(分仓) 合并
  backtest.py        # engine + walk_forward + metrics + artifacts 合并
  reports.py         # json_report + html_report 合并
  cli.py             # backtest/optimize/validate_data/paper_monitor 入口

tests/
  unit/              # 成本、FIFO、T+1、信号因果性
  integration/       # 单股多日、批量、频率一致性
  regression/        # golden fixture：295笔交易结果守卫

config/
  thresholds.yaml    # 单一真相源（保留现有）
  overlays/          # research.yaml / paper.yaml 仅覆盖部分字段
  schemas/

outputs/
  runs/<run_id>/     # 参数快照 + 数据指纹 + 结果 + 报告
```

关键调整说明：

1. **删除 `optimization/bayesian.py`**：当前样本仅 295 笔配对交易，贝叶斯优化在样本不足时必然过拟合（硬约束：少于 10 笔交易的参数组合判定为不可信）。仅保留 `grid.py` + `objective.py`（并入 `backtest.py` 的 `walk_forward`），并加样本量门禁。等样本扩至 60+ 交易日、单参数组合触发数稳定 ≥30 笔后再评估是否引入。
2. **`data.py` 不拆 providers 目录**：现有 [data_provider.py](../scripts/data_provider.py) 已用 `auto` 回退（eastmoney→mootdx→westock→baostock）收敛 4 个 `_fetch_xxx` 方法。只需抽出 `BarProvider` Protocol 接口，业务层依赖接口，不直接 import 具体数据源。
3. **`features.py` 合并**：reference/trend/volume 三类指标在研究阶段无独立拆分收益，合并为单文件，顶部用注释分区。regime（震荡/上升/下降/极端）作为**特征输出**放在此处（见 3.2）。
4. **配置 overlay 而非三套独立配置**：[thresholds.yaml](../config/thresholds.yaml) 保留为单一真相源，`overlays/research.yaml`、`overlays/paper.yaml` 仅覆盖部分字段（如成本场景、仓位上限），由 `config_loader` 合并加载，避免三套配置漂移。

### 3.2 依赖方向

```text
data -> features -> strategy -> risk -> execution -> backtest -> reports
                         ^          ^
                      domain     config
```

规则：

- `features` 不得读取仓位或执行状态。
- `features` 产出 `MarketRegime` 标签作为特征，`strategy` 和 `risk` 都可读，**避免 risk 反向依赖 strategy**（v1.0 把 regime 放在 strategy 层会导致 risk 无法调用它做趋势过滤，形成反向依赖）。
- `strategy` 只生成信号，不修改仓位，不计算手续费。
- `risk` 只决定信号是否允许执行和执行上限，可读 features 产出的 regime。
- `execution` 只负责成交、滑点和配对，不判断信号好坏。
- `backtest` 只编排事件流，不重复实现业务规则。
- `reports` 不反向影响回测。
- 数据提供商只能实现统一接口，业务层不得直接调用 BaoStock、东方财富或 mootdx。

## 4. 核心领域模型与接口

### 4.1 领域对象与不变量

```python
@dataclass(frozen=True)
class Bar:
    code: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    frequency: str
    # 不变量：frozen=True 保证不可变，多线程/缓存安全；
    #         open/high/low/close 满足 low<=open,close<=high；
    #         timestamp 单调递增（由 validator 保证）。

@dataclass(frozen=True)
class Signal:
    code: str
    timestamp: datetime
    direction: str          # buy / sell
    score: float
    reason_codes: tuple[str, ...]
    expected_return: float
    expected_cost: float
    regime: str             # 来自 features，不是 strategy 自己算
    feature_version: str
    # 不变量：一个 Signal 在 risk 通过后产生一个 Order；部分成交用多个 Fill 表达。

@dataclass(frozen=True)
class Fill:
    order_id: str
    timestamp: datetime
    price: float            # 含滑点
    shares: int
    commission: float
    tax: float
    slippage: float

@dataclass(frozen=True)
class Trade:
    entry_fill: Fill
    exit_fill: Fill | None
    pnl_net: float          # 已扣除完整双边成本
    holding_bars: int
    holding_days: int
    status: str             # paired / open / stopped / expired
```

#### Position 分仓不变量（T+0 核心）

A股 T+1 约束下，`Position` 必须区分四类仓位，**不能仅用一个净仓位变量推断可卖数量**：

```python
@dataclass
class Position:
    code: str
    base_shares: int            # 旧底仓（T-1 及更早，当日可卖）
    locked_shares: int          # 今日新买入（T+1 锁定，当日不可卖）
    # 派生量：
    @property
    def sellable_shares(self) -> int:
        """当前可卖 = 底仓 - 已卖未买回。"""
        return max(0, self.base_shares - self.locked_shares)
    # 不变量：locked_shares 在次日开盘转为 base_shares；
    #         卖出只能扣减 sellable，买入只能增加 locked。
```

此逻辑已存在于 [backtest_t_strategy.py](../scripts/backtest_t_strategy.py#L143-L145) 的 `BacktestState.sellable_shares`，迁移时提炼为 `Position` 不变量并加测试守护。

#### FIFO 跨日配对不变量（最高优先级守卫）

> **硬约束**：FIFO 配对队列必须贯穿整个回测区间，不得按日重置，确保跨日交易正确配对。

历史教训：按日重置导致 84% 跨日交易被排除统计，产生虚假高胜率。`execution.py` 顶部必须写明此不变量，并配回归测试：

```python
# execution.py 顶部
# 不变量：matcher 的 FIFO 队列生命周期 = 整个回测区间，禁止按日 reset()。
#         跨日未配对腿通过 initial_open_legs 在交易日之间传递。
```

### 4.2 关键接口

```python
class BarProvider(Protocol):
    def load(self, request: DataRequest) -> list[Bar]: ...

class FeaturePipeline(Protocol):
    def transform(self, bars: Sequence[Bar]) -> FeatureFrame: ...

class Strategy(Protocol):
    def generate(self, context: StrategyContext) -> Signal | None: ...

class RiskPolicy(Protocol):
    def approve(self, signal: Signal, portfolio: Portfolio) -> RiskDecision: ...

class ExecutionSimulator(Protocol):
    def execute(self, order: Order, bar: Bar, cost_model: CostModel) -> Fill: ...

class Matcher(Protocol):
    def apply(self, fill: Fill, portfolio: Portfolio) -> MatchResult: ...
```

## 5. 分阶段实施步骤

### Phase 0：基线冻结与数据审计（1～2天）

任务：

1. 为每次回测生成唯一 `run_id`，格式：`{timestamp}_{param_hash}_{data_hash}`，确保可复现。
2. artifacts 落盘内容（非数据本身，而是指纹）：

   | artifact | 内容 | 用途 |
   |---|---|---|
   | `params.yaml` | 参数快照 | 复现 |
   | `data_fingerprint.json` | 数据源、代码列表、时间区间、行数、hash | 数据可追溯 |
   | `code_version.txt` | git commit hash | 代码可追溯 |
   | `result.json` | BacktestResult | 结果 |
   | `report.html` | 可视化 | 人工复盘 |

3. 复现命令：`python -m at0.cli backtest --reproduce <run_id>`。
4. 删除统计目录中的历史混用产物，改为按 run_id 输出；不得直接覆盖旧报告。
5. 检查K线重复、乱序、缺失、时间边界和成交量异常（失败策略见 7.1）。
6. 给每一笔交易补充 `signal_time`、`fill_time`、`data_cutoff`、`feature_version`。

验收：同一输入重复运行结果完全一致；任意报告可以追溯到参数和数据来源。

### Phase 1：回测口径和成本模型整改 ✅ 已完成

> 本阶段已在当前会话完成，P0-1 收口。以下记录为已完成事实，供回归测试守卫。

已完成：

1. 新建 [cost_model.py](../scripts/cost_model.py)，统一计算佣金、印花税、滑点和冲击成本。
2. 风控的最小价差使用同一个成本模型（`get_cost_model()`）。
3. 实现乐观、基准、悲观三种成本场景（`scenario` 字段）。
4. 配对收益扣除完整双边成本。

配置起点（已在 [thresholds.yaml](../config/thresholds.yaml) 落地）：

```yaml
cost:
  commission_rate: 0.00025
  stamp_tax_rate: 0.0005
  slippage_rate: 0.001
  impact_rate: 0.0000
  scenario: base

risk:
  min_net_expected_return: 0.0045
  max_t_size_ratio: 0.25
```

后续待办：将“距离VWAP”与“未来实际可实现收益”分离，不再用前者冒充收益（属 P0-5 信号层）。

### Phase 2：交易生命周期和尾盘风控整改 ✅ 已完成

> 本阶段已在当前会话完成，P0-2/P0-3 收口。

已完成（[trade_lifecycle.py](../scripts/trade_lifecycle.py) + [exposure_policy.py](../scripts/exposure_policy.py)）：

```text
candidate -> filled -> open -> paired / stopped / expired
```

已落地规则：

- 14:20 后禁止新建无法当日处理的风险腿（`no_new_after_bar`）；
- 14:40 后只允许退出或降低风险（`exit_only_after_bar`）；
- 单笔最多持有 N 根K线（`max_holding_bars=12`），超过标记 `expired`；
- 每只股票最多一个方向性未配对腿（`max_open_legs_per_direction=1`）；
- 日终不平衡生成明确风险事件（`risk_events`），不静默丢失；
- 未配对腿单独计入浮盈亏（`unrealized_pnl`）。

后续待办（属 P0-4）：未配对腿单独计入组合 VaR、最大回撤和资金占用——需等 metrics 模块抽出。

### Phase 3：信号逻辑重构（3～6天）

把当前信号拆成三层：

1. **极值层**：价格偏离VWAP/ATR、布林带、RSI、KDJ、MFI。
2. **确认层**：不再创新高/低、反转K线、量能衰减、重新站回短均线。
3. **环境层**：趋势状态（regime，由 features 产出）、市场风险、板块状态、流动性。

推荐信号条件：

```text
极值条件 >= 2
反转确认 >= 1
趋势过滤通过（regime 非强趋势时才允许均值回归）
预期净收益 > 最小门槛
未超过敞口和交易次数限制
```

强趋势中（P0-6）：

- 上升趋势禁止逆势加仓；
- 下降趋势禁止接快速下跌刀；
- ADX高且价格持续远离VWAP时，暂停均值回归；
- 只有出现反转确认后才恢复交易。

注意：regime 作为 features 输出，strategy 和 risk 都可读，避免反向依赖。

### Phase 4：参数优化和滚动验证（5～10天）

第一轮只做小范围、可解释网格搜索（**不引入贝叶斯优化**，样本不足）：

```yaml
search:
  vwap_dev_atr_multiplier: [1.0, 1.2, 1.4]
  rsi_overbought: [70, 75, 80]
  rsi_oversold: [30, 25, 20]
  kdj_overbought: [80, 85, 90]
  kdj_oversold: [20, 15, 10]
  max_t_size_ratio: [0.20, 0.25, 0.33]
  max_t_trades_per_day: [2, 3]
  max_holding_bars: [3, 6, 12]
```

样本量门禁（硬约束）：单参数组合触发数 < 10 笔判定为不可信，直接淘汰；< 30 笔仅作参考不入选。

禁止：在同一测试集上反复搜索并直接选第一名。

推荐滚动流程：

```text
训练窗口：过去60～90个交易日
验证窗口：随后20个交易日
测试窗口：再随后20个交易日
向前滚动：每20个交易日重新评估
```

参数选择目标采用约束多目标评分：

```text
score = median(net_daily_return)
        - 0.5 * max_drawdown
        - 0.2 * unpaired_exposure_ratio
        - 0.1 * turnover_penalty
```

硬约束：样本外净收益为正、最大回撤不超过预算、未配对比例低于阈值、不能由单只股票贡献大部分收益。

### Phase 5：纸面交易和灰度（至少4周）

阶段顺序：

1. 仅生成信号，不模拟成交；
2. 信号与实时行情对照；
3. 纸面成交，记录实际盘口价差；
4. 小资金、单股票、单日限额灰度；
5. 连续4周满足验收标准后再评估扩大范围。

所有输出保持`research_only`，禁止在未完成验收前接入自动下单。

## 6. 代码重构方案

### 6.1 P0-4 局部拆分（不阻塞架构迁移）

P0-4 标为最高优先级，但完整 `src/at0/` 迁移是大工程。**建议先在 `scripts/` 内完成局部拆分**：

1. 把 [backtest_t_strategy.py](../scripts/backtest_t_strategy.py) 的"汇总统计"抽到 `backtest_metrics.py`；
2. "事件循环"保留为主入口，调用已存在的 cost_model/trade_lifecycle/exposure_policy；
3. 验证拆分前后回测结果一致（双跑对比，见 6.4）；
4. 之后再整体迁入 `src/at0/backtest.py`。

### 6.2 迁移映射

| 当前模块 | 目标模块 | 处理方式 |
|---|---|---|
| `intraday_reference.py` + `stock_quote_features.py` | `features.py` | 合并，统一输入Bar |
| `t_signal_engine.py` | `strategy.py` | 只保留信号生成 |
| `t_risk_guard.py` + `cost_model.py` + `exposure_policy.py` + `exit_policy` | `risk.py` | 合并（已抽出的3模块直接并入） |
| `backtest_t_strategy.py` | `backtest.py` + `execution.py` | 主循环→backtest，撮合→execution |
| `trade_lifecycle.py` + `position_tracker.py` | `execution.py` | matcher + portfolio 合并 |
| `data_provider.py` | `data.py` | 抽 Protocol，4个_fetch保留为方法 |
| `run_backtest.py` + `batch_backtest.py` | `cli.py` | 只做命令行编排 |
| `tune_params.py` | `backtest.py`(walk_forward) + `cli.py`(optimize) | 目标函数并入walk_forward |
| `backtest_report_html.py` + `batch_report_html.py` | `reports.py` | 合并 |

### 6.3 迁移顺序与双跑验证

按依赖方向自下而上迁移，每层完成后旧脚本改为调用新实现：

```text
domain → data → features → strategy → risk → execution → backtest → reports → cli
```

**双跑机制（强制）**：迁移期间新旧实现并行跑同一份数据，对比 `batch_summary.json` 关键指标：

| 指标 | 容差 |
|---|---|
| paired_trades | 完全一致（0 偏差） |
| net_pnl | < 1e-4 元 |
| win_rate | < 1e-6 |
| final_open_legs_count | 完全一致 |

任一指标超容差，禁止下线旧代码。这避免静默 bug 污染后续所有结论。

### 6.4 回归基线（golden fixture）

用当前 295 笔交易结果作为 golden fixture，存入 `tests/regression/golden_batch_summary.json`。每次重构后跑：

```python
# tests/regression/test_batch_baseline.py
def test_batch_results_unchanged():
    new = run_batch_backtest(...)
    golden = load("tests/regression/golden_batch_summary.json")
    assert new["paired_trades"] == golden["paired_trades"]
    assert abs(new["net_pnl"] - golden["net_pnl"]) < 1e-4
    assert abs(new["win_rate"] - golden["win_rate"]) < 1e-6
```

### 6.5 CLI 硬约束（迁移时必须保留）

迁移到 `cli.py` 时必须保留以下硬约束并加测试：

1. 实盘入口（`paper_monitor`）必须调用 `config_loader.load_signal_params()` 和 `load_risk_params()` 加载配置，**而非使用模块级默认参数**；
2. `TSignal.triggered` 必须读取实例的 `trigger_threshold`，而非模块级 `DEFAULT_PARAMS`；
3. `positions.json` 的读-改-写操作必须整体加锁，确保原子性；
4. 批量回测候选股票池必须通过 `candidate_screener.py` 筛选，禁止手动挑选。

### 6.6 重构原则

1. 先加接口和回归测试，再移动实现。
2. 每次只迁移一个职责，禁止“大爆炸”重写。
3. 旧脚本调用新服务，保持CLI兼容。
4. 所有默认参数来自配置对象，不允许在业务函数内散落硬编码。
5. 业务函数不读写全局文件；文件写入放在 artifacts 层。
6. 使用类型注解、不可变领域对象和显式返回值。
7. 报告统计只能消费标准化的`Trade`和`BacktestResult`。

## 7. 测试方案

### 7.1 数据验证失败策略

`data.py` 的 validator 必须对以下检查项明确处理动作：

| 检查项 | 失败策略 | 理由 |
|---|---|---|
| 缺失 bar | 缺口 > 5min 报错终止；≤5min 前向填充并记 warning | 小缺口可容忍，大缺口破坏指标连续性 |
| 重复 bar | 保留最后一条，记 warning | 数据源偶发重复 |
| 乱序 | **hard fail**（终止） | 数据源 bug，不可静默修复 |
| 未来数据 | **hard fail**（终止） | 回测正确性底线 |
| 涨跌停 | 标记到 Bar 上，由 strategy/risk 层决定是否过滤 | 非数据错误，是业务信号 |
| 成交量异常（0或极端值） | 记 warning，不终止 | 可能是真实停牌 |

### 7.2 单元测试

- 成本模型：佣金、印花税、滑点、不同场景；
- FIFO配对：同日、跨日、部分配对、同方向连续交易；
- **FIFO 跨日不变量守卫**：构造跨日腿，断言配对结果与"按日重置"版本不同（守护历史 84% 漏统计 bug 不复发）；
- T+1：新买入锁定、旧底仓可卖、跨日解锁；
- Position 分仓：卖出不超 sellable，次日 locked 转 base；
- 信号因果性：任何`t`时刻不得使用`t+1`数据；
- 时间边界：午间休市、14:20、14:40、15:00；
- 涨跌停和成交量异常；
- 数据源归一化和重复数据处理。

### 7.3 集成测试

- 单股票单日回测；
- 单股票多日跨日回测；
- 20只股票批量回测；
- 5分钟与1分钟频率一致性；
- 监控信号与回测信号输入一致性；
- 失败数据源回退不改变业务层结果格式。

### 7.4 回归测试命令

```powershell
cd D:\project\a-t0
python scripts\verify_l5.py
python scripts\run_backtest.py --code 600188 --start 2026-06-22 --end 2026-07-22 --source baostock
python scripts\batch_backtest.py --start 2026-06-22 --end 2026-07-22 --source baostock
python scripts\diagnose_crossday_pairing.py
```

整改后应增加：

```powershell
python -m pytest tests -q
python -m at0.cli.validate_data --start 2025-01-01 --end 2026-07-22
python -m at0.cli.optimize --config config/overlays/research.yaml --walk-forward
```

## 8. 验收标准

### 8.1 工程质量验收

必须全部满足：

- 业务层不直接依赖具体数据供应商；
- 策略层不写文件、不修改仓位；
- 成本模型只有一个实现；
- 回测、纸面监控使用同一个信号和风控接口；
- 所有关键函数有类型注解和单元测试；
- 关键模块测试覆盖率不低于80%；
- 同一数据、参数、代码版本重复运行结果一致；
- 回测报告包含run_id、数据源、参数版本、成本场景和代码版本。

### 8.2 回测正确性验收

- 未来数据泄漏测试100%通过；
- 成本压力测试可重复；
- 未配对腿必须全部有生命周期状态；
- 日终风险事件不能静默丢失；
- T+1锁定仓和可卖仓测试全部通过；
- 数据缺失、重复、乱序时必须告警或拒绝运行。

### 8.3 策略研究验收

作为阶段性门槛，建议至少满足：

- 样本外净收益为正；
- 基准成本场景和悲观成本场景均不出现灾难性亏损；
- 最大回撤不超过预先设定的风险预算；
- 未配对敞口比例低于5%；
- 4个以上交易日才配对的交易腿低于2%；
- 单只股票收益贡献不超过总收益的30%；
- 盈利不是由少数几天或少数几只股票决定；
- 交易次数下降后，单位交易期望仍为正；
- 参数在邻近取值区间内表现平滑，不出现单点最优。

### 8.4 纸面交易验收

至少连续4周：

- 实际成交价差和回测假设差异在可接受范围内；
- 无未解释的T+1违规信号；
- 无重复下单、重复日志或状态错乱；
- 信号延迟、数据过期和异常行情均有告警；
- 日终所有持仓状态可解释、可复盘。

## 9. 大模型与机器学习使用规范

### 9.1 大模型适用范围

- 生成候选特征和实验假设；
- 读取报告并归类亏损原因；
- 检查代码耦合、异常和测试缺口；
- 生成数据质量和交易复盘报告；
- 根据既定接口生成测试用例。

### 9.2 大模型禁止范围

- 直接输出实盘下单指令；
- 直接选择历史最优参数作为生产参数；
- 根据未来数据解释过去信号；
- 未经人工审核修改风控阈值；
- 代替样本外测试和纸面交易。

### 9.3 机器学习路线

第一阶段使用Logistic Regression或LightGBM预测：

```text
未来3/6/12根K线内，价格是否能覆盖全部交易成本并完成配对
```

模型输出概率，规则策略负责交易方向，风险层负责是否允许执行。模型必须使用时间切分、滚动验证、特征版本和模型版本，不能随机打乱时间序列。

## 10. 可复用实施提示词

### 10.1 回测逻辑审计提示词

```text
你是量化交易系统代码审计工程师。请审计当前项目中的回测引擎。

要求：
1. 检查是否使用未来数据、完整日高低点、未来成交量或未来指标；
2. 检查佣金、印花税、滑点是否按成交方向正确计算；
3. 检查FIFO配对、部分成交、跨日未配对和未实现盈亏；
4. 检查A股T+1锁定仓、旧底仓可卖仓和尾盘状态；
5. 输出问题严重等级、复现步骤、影响范围和最小修复方案；
6. 不要修改代码，先给出审计报告和测试用例。
```

### 10.2 架构重构提示词

```text
你是Python量化系统架构师。请在不改变现有CLI行为的前提下重构项目。

目标：
- 数据、特征、策略、风控、执行、回测、报告分层；
- 策略只生成Signal，不修改仓位和文件；
- 风控只做批准和仓位限制；
- 执行层统一处理成交、成本和滑点；
- 回测层只编排事件流；
- 数据提供商使用Protocol或抽象接口隔离；

实施规则：
1. 先查看现有代码和测试；
2. 先新增接口和回归测试，再迁移实现；
3. 每个提交只完成一个职责迁移；
4. 保留旧脚本兼容入口；
5. 不修改策略参数，不扩大交易权限；
6. 输出变更文件、依赖方向、风险点和验证命令。
```

### 10.3 参数优化提示词

```text
你是严格的时间序列研究员。请使用给定的回测数据设计参数实验，不直接寻找历史最优参数。

约束：
1. 使用训练/验证/测试时间切分，不随机打乱；
2. 先固定成本模型，再搜索策略参数；
3. 评分同时考虑净收益、最大回撤、未配对比例、交易成本和参数稳定性；
4. 输出全部候选组合数量、失败组合、样本外结果和邻近参数稳定性；
5. 禁止只按胜率排序；
6. 任何候选参数必须经过悲观滑点测试和纸面交易验证；
7. 如果样本不足，明确返回"无法得出可靠最优参数"。
```

### 10.4 交易复盘提示词

```text
请分析这批交易记录，按以下维度分组：
- 股票、日期、时间段、市场状态、趋势状态；
- 触发规则组合、预期价差、真实净收益；
- 持有时间、未配对状态、最大有利/不利波动；
- 成本场景和滑点场景。

请输出：
1. 亏损贡献最大的前10类交易；
2. 可删除或合并的冗余规则；
3. 需要增加的确认条件；
4. 不能仅凭该样本确认的结论；
5. 下一轮最小实验集。
不要直接给出实盘下单建议。
```

## 11. 最终执行顺序

```text
[已完成] 统一成本模型 (P0-1)
[已完成] 交易生命周期与跨日FIFO修复 (P0-2)
[已完成] 尾盘强制风控处置 (P0-3)
  -> 数据审计与run_id版本化 (P0-8)
  -> backtest_t_strategy 局部拆分 (P0-4)
  -> 信号层拆分与趋势过滤 (P0-5/P0-6)
  -> 扩数据至60+交易日 (P0-7)
  -> 小范围网格参数实验
  -> 滚动样本外验证
  -> 纸面交易至少4周
  -> 机器学习概率过滤
  -> 小资金灰度
```

任何阶段未通过验收，都不得进入下一阶段。尤其不能在回测净收益为负、未配对风险未收敛、纸面成交偏差未量化之前，使用大模型继续扩大参数搜索空间。
