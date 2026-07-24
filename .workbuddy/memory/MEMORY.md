# 项目长期记忆 (AT0 日内做T策略)

## 策略与风控约定
- 策略：A 股日内做 T（底仓 + VWAP 均值回归回转），代码在 `src/at0/`。
- 风险整改严守 `docs/strategy_optimization_implementation.md` 纪律：双跑验证 + §9.2"未经人工审核不修改风控阈值"。任何风控阈值调整需人工确认后再落地。
- 回测 PnL 口径：止损腿(STOPPED)盈亏必须计入 `state.cost_reduction`（曾因漏计产生"统计幻觉"）。

## 关键状态 (2026-07-24)
- Tier 1 风险整改已代码落地：open_max_vwap_dev 2.0%→1.2%；动态止损 floor 1.5% 已接入回测+实盘；max_holding_bars 按频率缩放。
- 整改 PnL 效果**未重跑验证**——下一步是同一数据双跑对比。
