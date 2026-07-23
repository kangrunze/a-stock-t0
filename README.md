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
│   ├── l5_monitor.py              # 主监控入口（实时+历史回放，多数据源）
│   ├── simulate_real_data.py      # 真实数据模拟交易 + 图表生成
│   ├── data_provider.py           # 统一数据源适配器（mootdx/westock/baostock）
│   ├── run_backtest.py            # 多日回测运行器（多数据源 → 回测 → 报告）
│   ├── westock_client.py          # westock CLI 统一客户端
│   ├── l2_theme_reader.py         # L2 题材状态统一读取
│   ├── market_layer.py            # 市场层（情绪分级+门控）
│   ├── stock_quote_features.py    # 个股实时报价特征
│   ├── candidate_screener.py      # T-eligible 候选筛选
│   ├── config_loader.py           # thresholds.yaml 加载器
│   └── verify_l5.py               # 端到端验证脚本（98项）
├── config/
│   └── thresholds.yaml            # 阈值集中配置
├── data/                          # 运行时数据
│   ├── positions.json             # 持仓状态（唯一真相源）
│   ├── minute_bars/               # 分钟K线CSV缓存
│   └── sh510300_5min_*.csv        # 真实5分钟K线数据
├── outputs/
│   ├── backtest/                  # 回测报告
│   │   └── param_tuning_report.json
│   └── simulate_trades/           # 模拟交易输出
│       ├── trades.csv             # 交易记录明细
│       ├── trades.json            # 结构化交易记录+汇总
│       ├── trades_overview.png    # 交易总览图
│       └── trades_daily.png       # 逐日交易明细图
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
- 依赖：
  - `matplotlib` — 仅 `simulate_real_data.py` 图表生成需要
  - `requests` — 东方财富数据源（`pip install requests`，**默认已装**）
  - `baostock` — BaoStock 数据源（`pip install baostock`，历史5分钟线）
  - `mootdx` — 通达信数据源（`pip install mootdx`，可选）
  - `westock-data` — westock CLI（可选，仅实盘实时行情，需配置 `WESTOCK_DIR` 环境变量）

```powershell
pip install matplotlib requests baostock mootdx
```

> **数据源回退顺序**（`auto` 模式）：`eastmoney`（免依赖，盘中实时1分钟）→ `mootdx` → `westock` → `baostock`（历史5分钟兜底）。未安装的数据源自动跳过。

### 1. 验证功能

```powershell
cd D:\project\a-t0
python scripts/verify_l5.py
```

预期输出：`验证结果: 98/98 通过, 0 失败`

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

### 4. 实时监控 / 历史回放（多数据源）

`l5_monitor.py` 支持四种数据源，实时和历史回放统一入口：

```powershell
# 实时监控（今日数据，仅交易时段运行）
python scripts/l5_monitor.py --source auto          # 自动回退（默认，eastmoney优先）
python scripts/l5_monitor.py --source eastmoney     # 东方财富（免依赖，盘中实时1分钟）
python scripts/l5_monitor.py --source baostock      # BaoStock（当日数据收盘后才可用）
python scripts/l5_monitor.py --source auto --demo   # 忽略时间窗口测试

# 历史回放（指定日期，不受交易时段限制）
python scripts/l5_monitor.py --source auto --date 2026-07-22       # auto 自动回退到 baostock
python scripts/l5_monitor.py --source baostock --date 2026-07-22   # 指定 BaoStock
```

**数据源说明**：

| source | 频率 | 实时 | 历史 | 依赖 | 说明 |
|--------|------|------|------|------|------|
| `auto` | 自动 | 是 | 是 | — | 按顺序回退：eastmoney→mootdx→westock→baostock |
| `eastmoney` | 1分钟 | ✅ | ❌ | `requests`（默认已装） | 东方财富公开API，**盘中实时首选**，仅支持当日 |
| `mootdx` | 1分钟 | 是 | 是 | `pip install mootdx` | 通达信协议，需服务器连通 |
| `westock` | 1分钟 | 是 | 否（仅当日） | westock-data CLI + `WESTOCK_DIR` | 仅实盘环境 |
| `baostock` | 5分钟 | ❌ | ✅ | `pip install baostock` | **历史回放首选**，当日数据收盘后才可用 |

> **为什么 eastmoney 不能查历史？** 东方财富 klt=1（1分钟线）的 beg/end 参数无效，API 始终返回最近交易日数据。历史日期会自动回退到 baostock（5分钟线）。
>
> 历史回放模式下，数据时效性检查（`check_bar_freshness`）自动跳过。无实时报价时用 `prev_close` 构造合成报价（涨跌停价 = prev_close × ±10%）。

### 5. 真实数据模拟交易

使用真实5分钟K线数据（沪深300ETF）进行模拟交易，生成交易记录和图表。

```powershell
python scripts/simulate_real_data.py
```

**数据要求**：CSV 格式 `date,time,open,high,low,close,volume,amount`，时间字段为 `YYYYMMDDHHMMSSmmm`（如 `20260715093500000`）。示例数据：`data/sh510300_5min_2026-07-15_2026-07-22.csv`。

**运行示例输出**：

```
========================================================================
  真实数据模拟交易 — sh510300（沪深300ETF）5分钟K线
========================================================================

[数据] 6 个交易日, 288 根K线
[参数] cooldown=3, require_opposite=True

========================================================================
  逐日模拟交易
========================================================================

────────────────────────────────────────────────────────────
  2026-07-16  | prev_close=4.8380
  时间         方向         股数        成交价      PnL   配对
  10:45:00   sell     5000     4.8162     0.00    否
  14:20:00   buy      5000     4.7558   302.14    是
  14:35:00   buy      5000     4.7417     0.00    否
  → T3笔, 净+272.21元, net_add

========================================================================
  汇总统计
========================================================================

  交易日:   6
  总交易:   7笔（配对1, 未配对6）
  盈利笔数: 1
  配对收益: 302.14元
  交易成本: 65.07元
  净盈亏:   +237.07元
  胜率:     14.3%
  收益率:   0.491%

[保存] CSV: outputs/simulate_trades/trades.csv
[保存] JSON: outputs/simulate_trades/trades.json
[图表] 总览图: outputs/simulate_trades/trades_overview.png
[图表] 按日详细图: outputs/simulate_trades/trades_daily.png
```

**输出文件**：

| 文件 | 说明 |
|------|------|
| `trades.csv` | 交易明细（日期/时间/方向/股数/信号价/成交价/成本/配对PnL/触发规则） |
| `trades.json` | 结构化记录（含每日汇总统计） |
| `trades_overview.png` | 6天总览图：蓝色折线=5min收盘价，红色▼=卖出，绿色▲=买入，橙色虚线=配对连线+盈亏标注 |
| `trades_daily.png` | 逐日明细图：每天一个子图，横轴为交易时段（午休间隙已去除） |

**图表说明**：横轴使用连续K线索引，午休时段（11:30-13:00）不在图上显示，上午最后一根与下午第一根K线直接相邻。

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

当前值为 `config/thresholds.yaml` 的起始参考值（方案 v0.2 起始值，dataclass 同步作为 fallback）。实盘入口通过 `config_loader.load_signal_params()` 加载，需用真实分钟数据回测校准后才能上生产。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `vwap_dev_atr_multiplier` | 0.8 | VWAP 偏离度阈值 = ±0.8 × ATR_intraday（相对值） |
| `bb_period` | 20 | 布林带周期 |
| `bb_std` | 2.0 | 布林带标准差倍数 |
| `ema_period` | 20 | EMA 周期（位置基准补充） |
| `rsi_period` | 14 | RSI 周期 |
| `rsi_overbought` | 70.0 | RSI 超买（辅助） |
| `rsi_oversold` | 30.0 | RSI 超卖（辅助） |
| `kdj_n` / `kdj_m1` / `kdj_m2` | 9 / 3 / 3 | KDJ 周期参数 |
| `kdj_overbought` | 80.0 | KDJ K 超买（主触发） |
| `kdj_oversold` | 20.0 | KDJ K 超卖（主触发） |
| `trend_filter_enabled` | true | 是否启用 MACD/DMI 趋势过滤 |
| `adx_trend_threshold` | 25.0 | ADX > 此值视为趋势盘 |
| `vol_ratio_lookback` | 5 | 量比当前窗口 |
| `vol_ratio_baseline` | 20 | 量比基准窗口 |
| `shrink_threshold` | 0.8 | 缩量阈值：当前5min量 < 过去20min均量 × 0.8 |
| `mfi_overbought` | 80.0 | MFI 超买（资金超买） |
| `mfi_oversold` | 20.0 | MFI 超卖（资金超卖） |
| `active_sell_pressure` | 0.55 | 主动卖占比 > 此值视为卖压 |
| `active_buy_pressure` | 0.55 | 主动买占比 > 此值视为买盘 |
| `min_rules_to_trigger` | 3 | 4 项中至少 3 项满足（3 内容项 + 1 过滤项） |

### RiskParams（风控参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_t_size_ratio` | 0.5 | 单次T仓位比例上限（底仓的 50%） |
| `max_t_trades_per_day` | 4 | 每日最大T次数 |
| `min_capture_spread` | 0.006 | 最小预期捕获空间（0.6%，方案 v0.2 起始值） |
| `round_trip_cost` | 0.001 | 来回成本（0.1%） |
| `eod_check_time` | "14:50" | 尾盘平衡检查时间 |

### ScreenerParams（候选筛选参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_20d_amplitude` | 0.035 | 20日平均振幅 ≥ 3.5% |
| `min_20d_amount` | 1.0e8 | 20日日均成交额 ≥ 1亿元 |
| `min_capture_spread` | 0.006 | 单笔预期捕获空间 ≥ 0.6% |

### BacktestParams（回测参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `commission_rate` | 0.00025 | 佣金万2.5（单边） |
| `stamp_tax_rate` | 0.0005 | 印花税0.05%（卖单） |
| `slippage` | 0.001 | 滑点0.1% |
| `base_shares` | 3000 | 底仓股数 |
| `avg_cost` | 10.00 | 底仓成本 |
| `warmup_bars` | 30 | 预热K线数（前30根不产生信号） |
| `cooldown_bars` | 3 | 信号触发后N根K线内不再触发同方向信号 |
| `require_opposite_direction` | true | 有未配对腿时只允许反方向信号（强制T闭环） |

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

### 多日回测（多数据源）

```powershell
# 自动回退数据源
python scripts/run_backtest.py --code 600000 --start 2026-07-15 --end 2026-07-22

# 指定 BaoStock
python scripts/run_backtest.py --code sh.600000 --start 2026-07-15 --end 2026-07-22 --source baostock

# 自定义底仓
python scripts/run_backtest.py --code 600000 --days 7 --base-shares 5000 --avg-cost 8.95
```

### 数据源自检

```powershell
python scripts/data_provider.py --code 600000 --date 2026-07-22 --source baostock
python scripts/data_provider.py --code 600000 --start 2026-07-15 --end 2026-07-22 --source auto
```

### 验证脚本

```powershell
python scripts/verify_l5.py
```

### 参数调优（快速模式）

```powershell
python scripts/tune_params.py --quick
```

### 实时监控 / 历史回放（多数据源）

```powershell
# 实时模式（今日数据，仅交易时段）
python scripts/l5_monitor.py --source auto              # 自动回退（默认）
python scripts/l5_monitor.py --source baostock          # BaoStock 数据源
python scripts/l5_monitor.py --source auto --demo       # 忽略时间窗口测试
python scripts/l5_monitor.py --eod-check                # 仅尾盘平衡检查

# 历史回放（指定日期，不受交易时段限制）
python scripts/l5_monitor.py --source baostock --date 2026-07-22
```

### 真实数据模拟交易

```powershell
python scripts/simulate_real_data.py    # 生成交易记录+图表
```

---

## 八、验证与回测

### 端到端验证

`verify_l5.py` 覆盖 8 大模块共 98 项测试：

1. **position_tracker**（15项）：T+1 约束、持仓读写、状态重置、并发写保护
2. **intraday_reference**（10项）：VWAP 因果性、KDJ 连续递推、数据不足处理
3. **t_signal_engine**（10项）：减仓/加仓/无信号场景、市场情绪加权
4. **t_risk_guard**（10项）：仓位/价差/熔断/独立性
5. **backtest_t_strategy**（5项）：端到端回测、FIFO 配对结算
6. **config_loader**（10项）：YAML 加载、dataclass fallback
7. **market_layer**（10项）：情绪分级、门控、权重调整
8. **独立性**（5项）：L1/L2 文件不存在时默认允许

### 调优结果

完整网格搜索：162 参数组合 × 7 形态 × 3 天 = 3402 次回测。

最优参数组合（合成数据）：
- `vwap_dev_atr_multiplier=1.0, rsi_overbought=65.0, rsi_oversold=25.0`
- `min_capture_spread=0.008, max_t_size_ratio=0.5`
- 总盈亏 10460.93，84 笔交易 100% 胜率

> 🚫 **严禁直接用于实盘**：当前 `config/thresholds.yaml` 的所有默认值（含 `min_rules_to_trigger=3`、`vwap_dev_atr_multiplier=0.8`、`min_capture_spread=0.006` 等全部参数）均**未经真实分钟数据验证**。上述"100% 胜率"完全建立在 `sample_data_generator.py` 生成的 7 种理想化合成形态上，不反映真实市场结构。**在完成真实分钟数据回测（覆盖足够多股票与交易日）并重新调优之前，禁止将本仓库任何默认参数用于实盘下单。**

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
| westock CLI | 可选 | 实盘实时行情可选数据源之一（`--source auto` 自动回退到 baostock） |
| baostock/mootdx | 可选 | 实时+历史数据源（`pip install baostock mootdx`） |

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

### 场景：对持仓 600000.SH 做日内T（实时监控）

```powershell
# 1. 初始化持仓（首次，编辑 data/positions.json 填入真实代码/成本）
python scripts/position_tracker.py --init-sample

# 2. 交易日开盘前重置 T 状态
python scripts/position_tracker.py --reset-today

# 3. 交易时段运行监控（每分钟，多数据源可选）
python scripts/l5_monitor.py --source auto          # 自动回退（推荐）
python scripts/l5_monitor.py --source baostock      # 指定 BaoStock

# 4. 14:50 尾盘平衡检查
python scripts/l5_monitor.py --eod-check

# 5. 收盘后复盘
python scripts/t_trade_logger.py  # 查看日志统计
```

### 场景：历史回放验证策略（不受交易时段限制）

```powershell
# 用 BaoStock 拉取历史数据，回放某天的信号评估
python scripts/l5_monitor.py --source baostock --date 2026-07-22

# 多日回测（生成买卖记录 + 报告）
python scripts/run_backtest.py --code 600000 --start 2026-07-15 --end 2026-07-22 --source baostock

# 查看报告
type outputs\backtest\600000_2026-07-15_2026-07-22_report.json
```

### 场景：合成数据回测调优

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

迁移后已通过 98/98 端到端验证，可作为独立项目运行。
