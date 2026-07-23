# A-T0 — A股日内做T（T+0）独立引擎

> 基于 L5 设计的独立日内做T引擎，输出全部为 **research_only**（研究/监控），不自动执行交易。

---

## 一、快速开始

### 环境要求

- Python 3.10+
- 依赖（按需安装）：

```powershell
pip install matplotlib requests baostock mootdx
```

| 依赖 | 用途 | 必需性 |
|------|------|--------|
| `requests` | 东方财富数据源（实时1分钟） | 默认已装 |
| `baostock` | BaoStock 数据源（历史5分钟线） | 历史回测推荐 |
| `mootdx` | 通达信数据源（实时+历史） | 可选 |
| `matplotlib` | 模拟交易图表生成 | 仅 `simulate_real_data.py` 需要 |
| `westock-data` CLI | westock 数据源（实盘环境） | 仅实盘，需配 `WESTOCK_DIR` |

### 验证安装

```powershell
cd D:\project\a-t0
python scripts/verify_l5.py
# 预期: 验证结果: 98/98 通过, 0 失败
```

---

## 二、运行方法

### 1. 实时监控（盘中运行）

`l5_monitor.py` 是实盘监控入口，仅交易时段运行：

```powershell
# 自动回退数据源（默认，推荐）
python scripts/l5_monitor.py --source auto

# 指定数据源
python scripts/l5_monitor.py --source eastmoney      # 东方财富（免依赖，盘中实时1分钟首选）
python scripts/l5_monitor.py --source baostock       # BaoStock（当日收盘后才可用）

# 尾盘平衡检查（14:50 单独执行）
python scripts/l5_monitor.py --eod-check

# 测试模式（忽略交易时段限制）
python scripts/l5_monitor.py --source auto --demo
```

**参数说明**：

| 参数 | 说明 |
|------|------|
| `--source` | 数据源：`auto`(默认) / `eastmoney` / `mootdx` / `westock` / `baostock` |
| `--date` | 指定交易日（不传=实时今日，传=历史回放） |
| `--demo` | 测试模式，忽略交易时段限制 |
| `--eod-check` | 仅执行尾盘平衡检查 |

### 2. 历史回放（任意时间运行）

```powershell
# 回放指定日期的信号评估
python scripts/l5_monitor.py --source baostock --date 2026-07-22
python scripts/l5_monitor.py --source auto --date 2026-07-22      # auto 自动回退到 baostock
```

### 3. 单股票多日回测

`run_backtest.py` 拉取历史数据 → 回测 → 生成报告：

```powershell
# 基本用法（auto 数据源，默认7天）
python scripts/run_backtest.py --code 600000

# 指定日期范围和数据源
python scripts/run_backtest.py --code sh.600000 --start 2026-07-15 --end 2026-07-22 --source baostock

# 自定义底仓
python scripts/run_backtest.py --code 600000 --days 7 --base-shares 5000 --avg-cost 8.95
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--code` | `600000` | 股票代码 |
| `--start` | 无 | 起始日期 YYYY-MM-DD（不传用 `--days`） |
| `--end` | 无 | 结束日期 YYYY-MM-DD（不传=今天） |
| `--days` | `7` | 回溯天数（无 `--start` 时用） |
| `--source` | `auto` | 数据源（同上） |
| `--base-shares` | `3000` | 底仓股数 |
| `--avg-cost` | 首日 prev_close | 底仓成本 |

### 4. 批量多股票回测

`batch_backtest.py` 读取候选池，批量回测并汇总：

```powershell
# 默认：候选池20只股票，近1个月 baostock
python scripts/batch_backtest.py

# 指定日期范围
python scripts/batch_backtest.py --start 2026-06-22 --end 2026-07-22

# 指定股票代码（覆盖候选池）
python scripts/batch_backtest.py --codes sh.600176,sh.600183 --source baostock
```

**参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--start` | `2026-06-22` | 起始日期 |
| `--end` | `2026-07-22` | 结束日期 |
| `--source` | `baostock` | 数据源 |
| `--codes` | 候选池 | 逗号分隔代码（覆盖候选池） |
| `--base-shares` | `3000` | 底仓股数 |

### 5. 参数调优

```powershell
# 合成数据快速调优
python scripts/tune_params.py --quick

# 合成数据完整网格搜索（162组合 × 7形态 × 3天）
python scripts/tune_params.py

# 真实数据滚动样本外验证（前2/3训练，后1/3验证）
python scripts/tune_params.py --data-source real --max-codes 5
```

**参数说明**：

| 参数 | 说明 |
|------|------|
| `--data-source` | `synthetic`(默认，合成数据) / `real`(真实多股票数据) |
| `--quick` | 快速模式（缩小参数空间） |
| `--start` / `--end` | real 模式日期范围（默认 2026-06-22 ~ 2026-07-22） |
| `--source` | real 模式数据源（默认 baostock） |
| `--max-codes` | real 模式最多用多少只股票（默认5） |

### 6. 真实数据模拟交易

使用 CSV 5分钟K线数据生成交易记录和图表：

```powershell
python scripts/simulate_real_data.py
```

数据要求：CSV 格式 `date,time,open,high,low,close,volume,amount`，示例数据 `data/sh510300_5min_2026-07-15_2026-07-22.csv`。输出至 `outputs/simulate_trades/`。

---

## 三、数据源对照表

`auto` 模式回退顺序：`eastmoney` → `mootdx` → `westock` → `baostock`（未安装的自动跳过）。

| source | 频率 | 实时 | 历史 | 依赖 | 说明 |
|--------|------|------|------|------|------|
| `auto` | 自动 | ✅ | ✅ | — | 按顺序回退（默认） |
| `eastmoney` | 1分钟 | ✅ | ❌ | `requests` | 东方财富公开API，**盘中实时首选**，仅支持当日 |
| `mootdx` | 1分钟 | ✅ | ✅ | `pip install mootdx` | 通达信协议，需服务器连通 |
| `westock` | 1分钟 | ✅ | ❌ | westock-data CLI | 仅实盘环境，需配 `WESTOCK_DIR` |
| `baostock` | 5分钟 | ❌ | ✅ | `pip install baostock` | **历史回测首选**，当日收盘后才可用 |

> **eastmoney 不能查历史**：klt=1 的 beg/end 参数无效，API 始终返回最近交易日数据。历史日期会自动回退到 baostock（5分钟线）。

---

## 四、参数说明

所有阈值集中在 `config/thresholds.yaml`，实盘入口通过 `config_loader.load_signal_params()` 和 `load_risk_params()` 加载。dataclass 默认值仅作为 fallback。

> ⚠️ **所有参数均为起始参考值，未经真实分钟数据充分验证，禁止直接用于实盘下单。**

### SignalParams（信号参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `vwap_dev_atr_multiplier` | 0.8 | VWAP 偏离度阈值 = ±0.8 × ATR_intraday |
| `rsi_period` | 14 | RSI 周期 |
| `rsi_overbought` | 70.0 | RSI 超买（辅助） |
| `rsi_oversold` | 30.0 | RSI 超卖（辅助） |
| `kdj_n` / `kdj_m1` / `kdj_m2` | 9 / 3 / 3 | KDJ 周期参数 |
| `kdj_overbought` | 80.0 | KDJ K 超买（主触发） |
| `kdj_oversold` | 20.0 | KDJ K 超卖（主触发） |
| `bb_period` / `bb_std` | 20 / 2.0 | 布林带周期 / 标准差倍数 |
| `ema_period` | 20 | EMA 周期 |
| `trend_filter_enabled` | true | 启用 MACD/DMI 趋势过滤 |
| `adx_trend_threshold` | 25.0 | ADX > 此值视为趋势盘 |
| `vol_ratio_lookback` / `vol_ratio_baseline` | 5 / 20 | 量比当前窗口 / 基准窗口 |
| `shrink_threshold` | 0.8 | 缩量阈值：当前5min量 < 过去20min均量 × 0.8 |
| `mfi_overbought` / `mfi_oversold` | 80.0 / 20.0 | MFI 超买/超卖（资金面） |
| `active_sell_pressure` / `active_buy_pressure` | 0.55 / 0.55 | 主动卖/买占比阈值 |
| `min_rules_to_trigger` | 3 | 4 项规则中至少 3 项满足 |

### RiskParams（风控参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_t_size_ratio` | 0.5 | 单次T仓位比例上限（底仓的 50%） |
| `max_t_trades_per_day` | 4 | 每日最大T次数 |
| `min_capture_spread` | 0.006 | 最小预期捕获空间（0.6%） |
| `round_trip_cost` | 0.001 | 来回成本（0.1%） |
| `eod_check_time` | "14:50" | 尾盘平衡检查时间 |

### BacktestParams（回测参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `commission_rate` | 0.00025 | 佣金万2.5（单边） |
| `stamp_tax_rate` | 0.0005 | 印花税0.05%（卖单） |
| `slippage` | 0.001 | 滑点0.1%（买入上浮/卖出下浮） |
| `base_shares` | 3000 | 底仓股数 |
| `warmup_bars` | 30 | 预热K线数（前30根不产生信号） |
| `cooldown_bars` | 3 | 信号触发后N根K线内不再触发同方向信号 |
| `require_opposite_direction` | true | 有未配对腿时只允许反方向信号（强制T闭环） |

### ScreenerParams（候选筛选参数）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_20d_amplitude` | 0.035 | 20日平均振幅 ≥ 3.5% |
| `min_20d_amount` | 1.0e8 | 20日日均成交额 ≥ 1亿元 |
| `min_capture_spread` | 0.006 | 单笔预期捕获空间 ≥ 0.6% |

---

## 五、核心概念

### T+1 约束

A 股 T+1 结算：今日买入的股份当日不可卖。通过 `positions.json` 的 `locked_shares` 字段硬性实现，`get_sellable_shares(code) = base_shares - locked_shares`。

### 正T / 反T

| 类型 | 操作 | 适用场景 |
|------|------|----------|
| **正T** | 先卖后买 | 冲高回落 |
| **反T** | 先买后卖 | 下探回升 |

### 三层共振信号

4 项规则中 ≥3 项同时满足才触发：

**减仓信号**：VWAP偏离 ≥ +ATR × mult / RSI超买或KDJ.K超买 / 缩量 / 未涨停
**加仓信号**：VWAP偏离 ≤ -ATR × mult / RSI超卖或KDJ.K超卖 / 地量企稳 / 题材未退潮

### FIFO 配对结算

回测采用跨日连续 FIFO 配对：买卖腿按时间顺序匹配，**不按日重置**。回测结束时仍未配对的敞口按收盘价计算浮盈浮亏，计入 `unrealized_pnl`。

---

## 六、输出文件

| 路径 | 说明 |
|------|------|
| `outputs/backtest/*_report.json` | 单股票回测报告 |
| `outputs/backtest/*_trades.json` | 单股票交易明细 |
| `outputs/backtest/batch_summary.json` | 批量回测汇总（含胜率/净盈亏/未配对浮盈） |
| `outputs/backtest/candidate_pool.json` | 候选股票池 |
| `outputs/backtest/param_tuning_report.json` | 合成数据调优报告 |
| `outputs/backtest/param_tuning_real_report.json` | 真实数据滚动样本外验证报告 |
| `outputs/simulate_trades/trades.csv` | 模拟交易明细 |
| `outputs/simulate_trades/trades_overview.png` | 交易总览图 |
| `state/signals.csv` | 信号评估记录 |
| `state/monitor.log` | 运行日志 |

---

## 七、典型使用流程

### 实时监控（持仓 600000.SH 做日内T）

```powershell
# 1. 初始化持仓（首次，编辑 data/positions.json 填入真实代码/成本）
python scripts/position_tracker.py --init-sample

# 2. 交易日开盘前重置 T 状态
python scripts/position_tracker.py --reset-today

# 3. 交易时段运行监控（每分钟）
python scripts/l5_monitor.py --source auto

# 4. 14:50 尾盘平衡检查
python scripts/l5_monitor.py --eod-check
```

### 历史回测验证策略

```powershell
# 单股票多日回测
python scripts/run_backtest.py --code 600000 --start 2026-07-15 --end 2026-07-22 --source baostock

# 批量回测（20只股票）
python scripts/batch_backtest.py --start 2026-06-22 --end 2026-07-22

# 真实数据参数调优
python scripts/tune_params.py --data-source real --max-codes 5
```

---

## 八、风险提示

1. **research_only**：所有输出均为研究/监控信号，**不自动执行交易**
2. **T+1 硬约束**：`locked_shares` 字段确保今日买入当日不可卖
3. **参数不可信**：`thresholds.yaml` 所有默认参数未经真实分钟数据充分验证，禁止直接用于实盘
4. **涨跌停过滤**：信号层 + 回测层双重过滤一字板/封死状态
5. **数据时效性**：实盘监控检查分钟数据新鲜度（>2分钟无更新视为陈旧）
