"""
A-T0 日内T+0策略研究系统
=========================
分层架构（对齐 strategy_optimization_implementation.md v1.1 §3.1）:

    data -> features -> strategy -> risk -> execution -> backtest -> reports
                         ^          ^
                      domain     config

模块职责:
  - domain:    领域对象（Bar/Signal/Order/Fill/Position/Trade/BacktestResult + enums + errors）
  - data:      数据层（BarProvider Protocol + 4个数据源 + normalizer + validator + l2_theme_reader）
  - features:  特征层（reference + quote + market + regime）
  - pipeline:  特征计算编排与版本号
  - strategy:  策略层（极值/确认/环境三层信号引擎，regime 由 features 产出）
  - risk:      风控层（cost_model + pre_trade + exposure + exit_policy 合并）
  - execution: 执行层（simulator + matcher(FIFO) + portfolio(分仓) 合并）
  - backtest:  回测层（engine + walk_forward + metrics + artifacts 合并）
  - reports:   报告层（json_report + html_report 合并）
  - cli:       入口层（backtest/optimize/validate_data/paper_monitor）

依赖规则（硬约束）:
  - features 不得读取仓位或执行状态
  - features 产出 MarketRegime 标签，strategy 和 risk 都可读，避免 risk 反向依赖 strategy
  - strategy 只生成信号，不修改仓位，不计算手续费
  - risk 只决定信号是否允许执行和执行上限
  - execution 只负责成交、滑点和配对，不判断信号好坏
  - backtest 只编排事件流，不重复实现业务规则
  - reports 不反向影响回测
  - 数据提供商只能实现统一接口，业务层不得直接调用 BaoStock/东方财富/mootox

FIFO 跨日配对不变量（最高优先级守卫）:
  matcher 的 FIFO 队列生命周期 = 整个回测区间，禁止按日 reset()。
  跨日未配对腿通过 initial_open_legs 在交易日之间传递。
  历史教训：按日重置导致 84% 跨日交易被排除统计，产生虚假高胜率。
"""
__version__ = "1.1.0"
