# A-T0 — A股日内做T（T+0）独立引擎

> 基于 L5 设计的独立日内做T（T+0 滚动）引擎，不依赖 L1/L2/L3/L4 任何层级。
> 输出全部为 **research_only**（研究/监控），不自动执行交易。

---

## 一、项目简介

本项目实现了 A 股日内做T的完整闭环：

- **T+1 约束硬性实现**：今日买入的股份当日不可卖
- **正T / 反T 双向支持**：先卖后买（正T）/ 先买后卖（反T）
- **三层共振信号框架**：位置层 / 动量层 / 量能层
- **严格因果指标**：所有指标（VWAP、布林带、RSI、KDJ、ATR）只用 [0, t] 区间数据，无未来函数
- **风控守卫**：仓位比例、每日次数、最小价差、L1/L2 软联动熔断、可用股份校验
- **分钟级回测引擎**：含滑点、佣金、印花税模拟
- **参数网格搜索调优**

### 独立性

本项目从 `ashare-sop-engine` 项目的 L5 模块迁移而来，**已完全独立**：

- 不引用 `ashare-sop-engine` 任何代码
- L1（宏观环境）文件不存在时按默认值 `RANGE_BOUND, research_allowed=True` 处理
- L2（题材状态）文件不存在时按 `unknown`（非退潮）处理
- 仅保留 westock-data CLI 作为可选外部数据源（实盘用，回测不需要）

---

## 二、目录结构

```
a-t0/
├── scripts/                       # 所有 Python 脚本
│   ├── position_tracker.py        # 持仓状态读写（T+1 约束核心）
│   ├── intraday_reference.py      # 滚动指标计算（VWAP/布林/RSI/KDJ/ATR）
│   ├── t_signal_engine.py         # 三层共振信号判定
│   ├── t_risk_guard.py            # 风控守卫（含 L1/L2 软联动）
│   ├── t_trade_logger.py          # 交易日志记录器
│   ├── minute_bar_fetcher.py      # 分钟K线获取（westock/CSV）
│   ├── backtest_t_strategy.py     # 分钟级回测引擎
│   ├── sample_data_generator.py   # 合成数据生成器（7种日内形态）
│   ├── tune_params.py             # 参数网格搜索
│   ├── l5_monitor.py              # 主监控入口
│   └── verify_l5.py               # 端到端验证脚本
├── data/                          # 运行时数据
│   ├── positions.json             # 持仓状态（唯一真相源）
│   └── minute_bars/               # 分钟K线CSV缓存
├── outputs/
│   └── backtest/                  # 回测报告
│       └── param_tuning_report.json
└── state/                         # 运行日志
    ├── signals.csv                # 信号评估记录
    ├── trades.csv                 # 成交记录
    └── monitor.log                # 运行日志
```

---

## 三、核心概念

### 1. T+1 约束（硬约束）

A 股实行 T+1 结算：**今日买入的股份当日不可卖**。

本项目通过 `positions.json` 中的 `locked_shares` 字段硬性实现：

```json
{
  "600xxx.SH": {
    "base_shares": 3000,              // 底仓股数（T+1已解锁，可卖）
    "avg_cost": 12.35,                // 底仓成本价
    "today_t_state": {
      "locked_shares": 1000,          // 今日新买入、当天不可卖的股份数
      "t_trades_today": 2,            // 今日已做T次数
      "net_position_delta": 500       // 相对底仓的净增减
    }
  }
}
```

- `get_sellable_shares(code) = base_shares - locked_shares`
- 每个交易日开盘前调用 `reset_today_state()` 把昨日 `locked_shares` 转入 `base_shares`

### 2. 正T / 反T

| 类型 | 操作 | 适用场景 |
|------|------|----------|
| **正T** | 先卖后买 | 冲高回落（卖出高位，买回低位） |
| **反T** | 先买后卖 | 下探回升（买入低位，卖出高位） |

### 3. 三层共振信号

信号触发要求 **4 项规则中 ≥3 项同时满足**：

**减仓信号（正T-卖出 / 反T-买回）**：
1. VWAP 偏离度 ≥ +1.0 × ATR_intraday（相对值）
2. 分钟 RSI(14) > 65 或 KDJ.K > 80
3. 当前5分钟均量 < 过去20分钟均量 × 0.8（缩量）
4. 未处于涨停封板状态

**加仓信号（反T-买入 / 正T-买回）**：
1. VWAP 偏离度 ≤ -1.0 × ATR_intraday 或跌破开盘区间下轨 或跌破布林带下轨
2. 分钟 RSI(14) < 25 或 KDJ.K < 20
3. 连续2-3根1分钟K线缩量且不再创新低（地量企稳）
4. 关联题材未被判定为"退潮"（默认非退潮）

---

## 四、快速开始

### 环境要求

- Python 3.10+
- 无第三方依赖（仅标准库）

### 1. 验证功能

```powershell
cd D:\project\a-t0
python scripts/verify_l5.py
```

预期输出：`验证结果: 40/40 通过, 0 失败`

### 2. 运行回测调优（快速模式）

```powershell
python scripts/tune_params.py --quick
```

报告输出至 `outputs/backtest/param_tuning_report.json`。

### 3. 运行完整网格搜索

```powershell
python scripts/tune_params.py
```

162 参数组合 × 7 形态 × 3 天 = 3402 次回测，耗时约 1-2 分钟。

### 4. 实盘监控（需 westock-data）

```powershell
python scripts/l5_monitor.py
```

仅在 A 股交易时段（9:25-11:35, 12:55-15:05）运行。`--demo` 可忽略时间窗口测试。

---

## 五、模块详解

### position_tracker.py — 持仓状态追踪器

`positions.json` 的唯一读写入口，所有写操作加文件锁。

| 函数 | 作用 |
|------|------|
| `load_positions()` | 加载所有持仓 |
| `save_positions(positions)` | 原子写入（加文件锁） |
| `get_sellable_shares(code)` | 计算可卖股数（T+1 约束） |
| `apply_t_trade(code, direction, shares, price)` | T 交易后更新状态 |
| `reset_today_state()` | 次日开盘前重置（T+1 解锁） |
| `init_sample_positions(path)` | 初始化示例持仓 |

### intraday_reference.py — 滚动指标计算器

纯计算模块，**严格因果**（无未来函数）。

| 函数 | 指标 |
|------|------|
| `cumulative_vwap(bars)` | 累计成交量加权均价 |
| `vwap_deviation(price, vwap)` | VWAP 偏离度 |
| `intraday_bollinger(bars, period, num_std)` | 日内布林带 |
| `opening_range(bars)` | 开盘区间（9:31-10:00 高低点） |
| `intraday_atr(bars, period)` | 日内 ATR |
| `rsi(bars, period)` | 分钟级 RSI |
| `kdj(bars, n, m1, m2)` | 分钟级 KDJ |
| `volume_ratio(bars, lookback, baseline)` | 量比 |
| `compute_reference_snapshot(bars)` | 一次性计算所有指标 |

### t_signal_engine.py — 信号引擎

| 函数 | 作用 |
|------|------|
| `evaluate_reduce_signal(bars, ...)` | 评估减仓信号 |
| `evaluate_add_signal(bars, ...)` | 评估加仓信号 |
| `evaluate_all_signals(bars, ...)` | 综合评估，返回 `recommendation` |

`recommendation` 取值：`reduce` / `add` / `none` / `conflict`

### t_risk_guard.py — 风控守卫

下单前最后一道闸门。

| 函数 | 作用 |
|------|------|
| `check_risk(code, direction, shares, ...)` | 风控主检查 |
| `read_l1_gate()` | 读取 L1 状态（不存在返回默认允许） |
| `is_l1_systemic_risk()` | L1 是否系统性风险日 |
| `read_theme_state(theme_name)` | 读取 L2 题材状态 |
| `is_theme_retreated(theme_name)` | 题材是否退潮 |
| `eod_balance_check(code)` | 尾盘平衡检查 |

**风控规则**：
- 单次T仓位 ≤ 50% 底仓
- 每日最大T次数 ≤ 4 次
- 最小预期价差 ≥ 0.8%
- L1 系统性风险日：禁止加仓，仅允许减仓
- L2 题材退潮：禁止加仓，仅允许减仓
- 可用底仓不足：自动调整或拒绝

### backtest_t_strategy.py — 回测引擎

| 函数 | 作用 |
|------|------|
| `backtest_single_day(code, date, bars, prev_close, params)` | 单日回测 |
| `backtest_multi_day(code, daily_bars, daily_prev_closes, params)` | 多日回测 |
| `calc_trade_cost(direction, shares, price, params)` | 计算交易成本 |
| `apply_slippage(direction, price, params)` | 应用滑点 |

**回测成本模型**：
- 佣金：万2.5（单边）
- 印花税：0.05%（卖单）
- 滑点：0.1%（买入上浮，卖出下浮）

### sample_data_generator.py — 合成数据生成器

7 种日内形态用于回测：

| 形态 | 描述 | T 策略适用性 |
|------|------|--------------|
| `spike_pullback` | 冲高回落 | 正T 理想 |
| `dip_rally` | 下探回升 | 反T 理想 |
| `range_bound` | 横盘震荡 | T 难有作为 |
| `trend_up` | 单边上涨 | 正T 卖后难买回 |
| `trend_down` | 单边下跌 | 反T 买后难卖出 |
| `v_shape` | V型反转 | 先反T后正T |
| `inverted_v` | 倒V型 | 先正T后反T |

---

## 六、参数说明

### SignalParams（信号参数）

当前值为合成数据网格搜索调优结果。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `vwap_dev_atr_multiplier` | 1.0 | VWAP 偏离度阈值 = ±1.0 × ATR_intraday |
| `rsi_overbought` | 65.0 | RSI 超买阈值 |
| `rsi_oversold` | 25.0 | RSI 超卖阈值 |
| `bb_period` | 20 | 布林带周期 |
| `bb_std` | 2.0 | 布林带标准差倍数 |
| `kdj_overbought` | 80.0 | KDJ K 超买 |
| `kdj_oversold` | 20.0 | KDJ K 超卖 |
| `shrink_threshold` | 0.8 | 缩量阈值 |
| `min_rules_to_trigger` | 3 | 4 项中至少 3 项满足 |

### RiskParams（风控参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_t_size_ratio` | 0.5 | 单次T仓位比例上限（底仓的 50%） |
| `max_t_trades_per_day` | 4 | 每日最大T次数 |
| `min_capture_spread` | 0.008 | 最小预期捕获空间（0.8%） |
| `round_trip_cost` | 0.001 | 来回成本（0.1%） |

### BacktestParams（回测参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `commission_rate` | 0.00025 | 佣金万2.5（单边） |
| `stamp_tax_rate` | 0.0005 | 印花税0.05%（卖单） |
| `slippage` | 0.001 | 滑点0.1% |
| `base_shares` | 3000 | 底仓股数 |
| `warmup_bars` | 30 | 预热K线数（前30根不产生信号） |

---

## 七、CLI 使用示例

### 初始化示例持仓

```powershell
python scripts/position_tracker.py --init-sample
```

### 查看当前持仓

```powershell
python scripts/position_tracker.py --show
```

### 次日开盘前重置

```powershell
python scripts/position_tracker.py --reset-today
```

### 单日回测

```powershell
python scripts/backtest_t_strategy.py --code 600xxx.SH --date 2026-07-22 --prev-close 10.00
```

### 验证脚本

```powershell
python scripts/verify_l5.py
```

### 参数调优（快速模式）

```powershell
python scripts/tune_params.py --quick
```

### 实盘监控

```powershell
python scripts/l5_monitor.py            # 交易时段自动运行
python scripts/l5_monitor.py --demo     # 忽略时间窗口测试
python scripts/l5_monitor.py --eod-check # 仅尾盘平衡检查
```

---

## 八、验证与回测

### 端到端验证

`verify_l5.py` 覆盖 6 大模块共 40 项测试：

1. **position_tracker**（13项）：T+1 约束、持仓读写、状态重置
2. **intraday_reference**（6项）：VWAP 因果性、数据不足处理
3. **t_signal_engine**（5项）：减仓/加仓/无信号场景
4. **t_risk_guard**（8项）：仓位/价差/熔断/独立性
5. **backtest_t_strategy**（3项）：端到端回测
6. **独立性**（5项）：L1/L2 文件不存在时默认允许

### 调优结果

完整网格搜索：162 参数组合 × 7 形态 × 3 天 = 3402 次回测。

最优参数组合（合成数据）：
- `vwap_dev_atr_multiplier=1.0, rsi_overbought=65.0, rsi_oversold=25.0`
- `min_capture_spread=0.008, max_t_size_ratio=0.5`
- 总盈亏 10460.93，84 笔交易 100% 胜率

> 🚫 **严禁直接用于实盘**：当前 `config/thresholds.yaml` 的所有默认值（含 `min_rules_to_trigger=3`、`vwap_dev_atr_multiplier=0.8`、`min_capture_spread=0.008` 等全部参数）均**未经真实分钟数据验证**。上述"100% 胜率"完全建立在 `sample_data_generator.py` 生成的 7 种理想化合成形态上，不反映真实市场结构。**在完成真实分钟数据回测（覆盖足够多股票与交易日）并重新调优之前，禁止将本仓库任何默认参数用于实盘下单。**

---

## 九、独立性设计

### L1 软联动

```python
def read_l1_gate() -> dict:
    try:
        if L1_GATE_FILE.exists():
            with open(L1_GATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {"regime": "RANGE_BOUND", "research_allowed": True}
```

L1 文件不存在时返回默认允许值，L5 可独立运行。

### L2 软联动

```python
def read_theme_state(theme_name) -> str:
    if not theme_name:
        return "unknown"
    # 尝试读取 L2 题材文件，失败返回 "unknown"
    return "unknown"
```

L2 文件不存在或无关联题材时返回 `unknown`（视为非退潮），L5 可独立运行。

### 文件级解耦

| 文件 | 依赖 | 缺失时行为 |
|------|------|------------|
| `positions.json` | **硬依赖** | 报错（必须存在） |
| `l1_gate.json` | 软依赖 | 默认 `RANGE_BOUND, allowed=True` |
| L2 题材文件 | 软依赖 | 默认 `unknown`（非退潮） |
| westock CLI | 可选 | 仅实盘需要，回测不需要 |

---

## 十、风险提示与边界

1. **research_only**：所有输出均为研究/监控信号，**不自动执行交易**
2. **T+1 硬约束**：`locked_shares` 字段确保今日买入当日不可卖
3. **参数不可信**：当前 `thresholds.yaml` 所有默认参数**仅基于合成数据调优，未经真实分钟数据验证，禁止直接用于实盘**（详见第八节调优结果免责声明）
4. **涨跌停过滤**：信号层 + 回测层双重过滤一字板/封死状态
5. **数据时效性**：实盘监控会检查分钟数据新鲜度（>2分钟无更新视为陈旧）
6. **尾盘平衡**：14:50 强制检查 `net_position_delta`，标记主动减/加仓状态

---

## 十一、典型使用流程

### 场景：对持仓 600xxx.SH 做日内T

```powershell
# 1. 初始化持仓（首次）
python scripts/position_tracker.py --init-sample

# 2. 交易日开盘前重置 T 状态
python scripts/position_tracker.py --reset-today

# 3. 交易时段运行监控（每分钟）
python scripts/l5_monitor.py

# 4. 14:50 尾盘平衡检查
python scripts/l5_monitor.py --eod-check

# 5. 收盘后复盘
python scripts/t_trade_logger.py  # 查看日志统计
```

### 场景：回测验证策略

```powershell
# 1. 生成合成数据自检
python scripts/sample_data_generator.py

# 2. 运行网格搜索调优
python scripts/tune_params.py

# 3. 查看报告
type outputs\backtest\param_tuning_report.json
```

---

## 十二、从源项目迁移说明

本项目从 `d:\project\ashare-sop-engine\skills\ashare-sop-l5-intraday-t\` 迁移而来。

**路径常量变更**：

| 旧值 | 新值 |
|------|------|
| `HERMES_ROOT = ...parent.parent.parent.parent / "hermes"` | `PROJECT_ROOT = Path(__file__).resolve().parent.parent` |
| `hermes/data/positions.json` | `data/positions.json` |
| `hermes/data/l1_gate.json` | `data/l1_gate.json` |
| `hermes/data/l5_minute_bars/` | `data/minute_bars/` |
| `hermes/outputs/l5_backtest/` | `outputs/backtest/` |
| `hermes/state/l5_intraday_t/` | `state/` |

迁移后已通过 40/40 端到端验证，可作为独立项目运行。
