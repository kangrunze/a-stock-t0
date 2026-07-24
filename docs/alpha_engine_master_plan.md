# A-T0 日内 Alpha 平台 — 整体实施方案（Master Plan）

版本：v1.0
日期：2026-07-24
定位：把"基于规则的做 T 策略"演进为"可持续迭代的 A 股日内 Alpha 交易研究平台"
基准：`src/at0/`（domain/data/features/strategy/risk/execution/backtest/reports/cli 九层）、`config/thresholds.yaml`、新 baseline（2026-07-24，78 笔配对 / +13,241 元）、`docs/strategy_optimization_implementation.md` v1.1 纪律
配套：本方案是 `docs/alpha_engine_proposal_review.md` 评审结论的落地版

---

## 0. 设计三原则（贯穿全案，不可违背）

1. **测量先于优化**：任何一层在被"优化 / 自适应"之前，必须先有它的效果度量（IC、平台区、分层胜率）。没有尺子不造武器。
2. **一次只放开一个自由度**：23 天样本上，每引入一个可调参数就多一分过拟合。每阶段只动一个变量，用参数平台验证平台区变宽后再动下一个。
3. **人在回路 + 双跑对账**：所有风控阈值变更走 §9.2 人工审核；所有新引擎与旧实现同数据双跑对比通过后才切换；配对口径统计必须能与"全现金流 PnL"对平。

---

## 1. 目标架构（修订版八层 → 落到九文件）

原方案八引擎的**职责**全部保留，但物理上不新增八个目录，而是嵌进现有九文件分层，避免过早抽象（延续 v1.1"9 文件"决策）。

```
                    ┌─────────────────────────────┐
   数据/特征域       │ Universe Engine (screener)   │  每日可做T股票池 + 评分
                    └──────────────┬──────────────┘
                    ┌──────────────▼──────────────┐
   features.py      │ Regime Engine                │  输出 trend/mean/vol 三连续分值
                    └──────────────┬──────────────┘
                    ┌──────────────▼──────────────┐
   strategy.py      │ Alpha Engine (连续评分)      │  AlphaScore 0~100 = Σ wᵢ·scoreᵢ
                    └──────────────┬──────────────┘
                    ┌──────────────▼──────────────┐
   risk.py          │ Adaptive Threshold + Position│  动态阈值(单点) + Confidence 分档仓位
                    └──────────────┬──────────────┘
                    ┌──────────────▼──────────────┐
   execution.py     │ Execution + Pair (FIFO)      │  动态限价/超时提价 + FIFO 记账
                    └──────────────┬──────────────┘
                    ┌──────────────▼──────────────┐
   backtest.py      │ 测量层 + Offline Suggestion  │  IC/分层/平台 + 周报建议(人工审批)
                    └─────────────────────────────┘
```

### 1.1 层职责与数据契约

| 引擎 | 落地文件 | 输入 | 输出（契约） | 相对现状的改造 |
|---|---|---|---|---|
| **Universe** | `screener.py` | 全市场日频行情、财务标记 | `UniverseScore{code, liq_score, vol_score, total, tradable:bool}` | 现有硬规则(振幅≥3.5%/额≥1亿)保留为硬过滤；新增 `0.4·liq + 0.6·vol` 评分，`total<60` 当日剔除 |
| **Regime** | `features.py::detect_market_regime` | 个股 bar 序列 | `RegimeSnapshot{label, trend_score, mean_score, vol_score}`（三值 0~100） | 现 4 标签保留；**新增三个连续分值**作为下游输入 |
| **Alpha** | `strategy.py::evaluate_*_signal` | 特征快照 + regime | `AlphaSignal{alpha_score:0~100, sub_scores:dict, direction}` | 现 `extreme/confirm/filter` 布尔计分 → **连续 score 映射**；`alpha=Σ wᵢ·scoreᵢ`，wᵢ 等权起步、进 yaml |
| **Adaptive Threshold** | `risk.py` | regime.vol_score、ATR | 动态后的开仓偏离阈值 | **仅动态化 VWAP 开仓阈值一个**：`thr = base × vol_factor(regime)`；其余保持静态 |
| **Position** | `risk.py::pre_trade` | alpha_score、ATR、底仓、可回转额度 | `target_size`（已整手 + 额度封顶） | 固定 25% → `RiskBudget × Confidence × VolAdj`，含 100 股整手 & T+1 可回转约束 |
| **Execution** | `execution.py` | 目标委托、盘口/高低点 | Fill | 现执行层已较完善；增 **动态限价 + 5s 超时提价** 模拟 |
| **Pair** | `execution.py::matcher` | 成交腿序列 | Trade（配对） | **保持 FIFO**（拒绝 Best-Match，见评审红线三）；新增"全现金流 PnL"对账基准 |
| **测量层** | `backtest.py::metrics` | 全部交易与信号 | IC/RankIC/Decay/MAE/MFE/分层/平台 | **本方案新增的核心能力**，见 §3 |
| **Offline Suggestion** | `backtest.py` + `reports.py` | 分 regime 交易统计 | 权重/阈值调整**建议**（周报） | 替代原"Online Learning 自动更新"，**人工审批制** |

### 1.2 关键契约细节

**Alpha score 映射（把布尔改连续的核心）**
每个子信号定义单调映射函数，折点本身是参数、纳入平台分析：

```
score_vwap(vwap_dev)  : 0ATR→100, -0.3ATR→80, -0.6ATR→60, -1.0ATR→20   (卖腿方向对称)
score_rsi(rsi)        : 30→90, 40→70, 50→50, 60→20, 70→0
score_kdj             : f(K到20/80距离, J拐点, KD夹角)  # 拒绝金叉/死叉离散判断
score_adx             : 15→10, 20→30, 25→60, 35→90 (配合 DI+/DI- 方向)
score_volume/moneyflow: 量比、MFI 归一
AlphaScore = clip(Σ wᵢ·scoreᵢ, 0, 100)   # w 等权起步，写 thresholds.yaml
```

**动态阈值（只放开一个）**
```
vwap_open_thr = base_dev(0.8·ATR) × vol_factor
vol_factor    = 0.75(低波) / 1.0(常态) / 1.3(高波)   # 由 regime.vol_score 分档
```
止损/止盈公式（`max(1ATR, 1.5σ, OpenDev×0.8)` 等）属**风控阈值**，走 §9.2 人工审核，不在自动路径内。

**仓位**
```
target = RiskBudget × Confidence(alpha) × VolAdj
Confidence: alpha≥95→1.0 / ≥80→0.57 / ≥60→0.29   (对应 35%/20%/10% 档)
硬约束: floor 到 100 股整手; 且 ≤ min(风险预算, 当日可回转额度)
```

---

## 2. 分阶段路线（带验收标准）

### Phase 0 — 硬前置（本周，未完成前冻结 strategy.py）
| 任务 | 交付 | 验收标准 |
|---|---|---|
| Tier 1 风险整改双跑验证 | 同数据新旧对比报告 | 关键指标落在 §6.4 容差内，确认整改未引入回归 |
| P0-7 数据扩容启动 | 60+ 交易日 × 扩大股票池的真实缓存 | 覆盖多种 regime，非合成数据（避免重蹈 synthetic 幻觉） |

> 依据评审红线二：78 笔样本撑不起任何分层与学习，数据是一切的前提。

### Phase 1 — 测量层（1~2 周，最高优先级）
| 任务 | 交付 | 验收标准 |
|---|---|---|
| IC / RankIC / Alpha Decay | `backtest.py` 新增分析 + `reports.py` 展示 | 对每个现有子信号输出 IC 及 1/3/5/10/20 根衰减曲线 |
| 交易质量指标 | MAE/MFE/Expectancy/Holding Time | 与"全现金流 PnL"对账一致 |
| 分层回测 | 按 4-regime × 时间段（开盘30min/午后/尾盘） | 每格样本量透明标注，<5 笔的格子标记"不可结论" |
| 参数平台扫描 | 先扫 VWAP threshold 单参数 | 输出收益-参数曲线，识别平台区 vs 尖峰 |

**产出决定下游**：哪些子信号 IC 显著 → 决定 Alpha Engine 保留/丢弃哪些 score；哪些 regime 区分度真实 → 决定要不要 4→6。

### Phase 2 — 连续评分改造（2~3 周）
| 任务 | 交付 | 验收标准 |
|---|---|---|
| Alpha 连续评分 | `evaluate_*_signal` 输出 AlphaScore | 双跑：连续评分 vs 现布尔门槛，同数据对比 PnL/配对率 |
| Regime 三连续分值 | `detect_market_regime` 扩展 | 分值与旧标签一致性校验 |
| Universe 评分 | `screener.py` 评分函数 | 现 20 股上跑通，评分排序合理 |

### Phase 3 — 单点动态化 + 仓位（2~4 周）
| 任务 | 交付 | 验收标准 |
|---|---|---|
| VWAP 阈值动态化 | `risk.py` vol_factor | 平台分析证明动态后平台区变宽，否则回滚 |
| Confidence 分档仓位 | `risk.py::pre_trade` | 整手 & 可回转额度约束单测通过 |
| 动态止盈止损 | 方案 → **§9.2 人工审核** → 双跑 → 落地 | 审核记录留痕；止损腿 PnL 计入 cost_reduction |

### Phase 4 — 数据就绪后评估（持续）
| 任务 | 触发条件 | 说明 |
|---|---|---|
| Offline Suggestion Engine | Phase 1-3 稳定 | 周报出权重/阈值建议 → 人工审批 → 双跑落地（替代 Online Learning） |
| Walk-Forward 滚动验证 | 数据≥60日 | 滚动训练/验证防过拟合 |
| regime 4→6 | Phase 1 分层证明细分有区分度 | 否则不细分 |
| L2 订单流 Alpha | 先回答"数据源/成本/延迟" | 未立项前不进主线 |

---

## 3. 测量层规格（本方案的技术核心）

这是唯一"零过拟合风险 + 指导所有其他层"的模块，Phase 1 优先交付。

- **Alpha 质量**：IC = corr(score_t, forward_return_{t+k})；RankIC 用秩相关抗异常值；Alpha Decay 画 k=1/3/5/10/20 的 IC 衰减，判断信号有效持续几根 K 线（直接决定 max_holding_bars）。
- **交易质量**：Win Rate / Profit Factor / Expectancy / MAE（最大不利）/ MFE（最大有利）/ Holding Time。MAE/MFE 用来回答遗留问题——55.2% 配对失败卡在"未回归 VWAP"，是开仓信号差还是持仓窗太短。
- **分层**：regime × 时间段（起步两维，样本够再加行业/流动性/振幅/市值分位），每格标注样本量。
- **参数平台**：扫参数邻域，看是宽平台还是尖峰。尖峰 = 过拟合信号，直接否决该参数上生产。

对账铁律：任何配对口径的 PnL 必须能与"全部成交现金流之和"对平，防止再次出现止损腿漏计的统计幻觉。

---

## 4. 纪律与合规映射

| 纪律来源 | 本方案对应设计 |
|---|---|
| §9.2 未经人工审核不改风控阈值 | 止损/止盈/仓位上限变更全部走人工审核；Online Learning 降级为人工审批周报 |
| §1.2 不在样本不足时选最优参数 | 参数平台分析否决尖峰参数；<5 笔格子不下结论 |
| 双跑验证（§6.3/6.4） | 每个新引擎与旧实现同数据对比通过才切换 |
| 止损腿 PnL 计入 cost_reduction | 测量层"全现金流对账"作为硬校验 |
| 不用合成数据证明可行 | Phase 0 强制扩真实数据，替换 synthetic 缓存 |

---

## 5. 一页纸决策摘要

- **做什么**：把规则策略升级为评分制 Alpha 平台，八引擎职责嵌入现有九文件，不推倒重写。
- **先做什么**：Phase 0 双跑+扩数据 → Phase 1 测量层。**先造尺子，再造武器。**
- **不做什么**：不上 Best-Match 配对（保 FIFO）、不上自动 Online Learning（改人工审批）、不一次性全面动态化（一次一个自由度）。
- **成败关键**：数据量 + 验证纪律，而非引擎数量。

> 本文档为研究与工程规划，不构成投资建议。所有阈值均为起始参考值，需回测校准 + 人工审核后方可上生产。
