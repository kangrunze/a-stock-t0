"""
A-T0 特征计算编排（pipeline 层）
=================================
对齐 strategy_optimization_implementation.md v1.1 §3.1：
pipeline 负责特征计算编排与版本号管理，不包含业务逻辑。

当前项目特征计算由 features.compute_reference_snapshot() 统一产出，
本模块提供版本号管理和编排入口，供 backtest 和 cli 调用。
"""
from __future__ import annotations

from typing import Optional

from .features import compute_reference_snapshot, merge_with_reference_snapshot


# 特征版本号（每次 features 层指标算法变更时递增）
FEATURES_VERSION = "1.1.0"

# 特征快照 schema 版本（影响序列化兼容性）
SNAPSHOT_SCHEMA_VERSION = 1


def compute_snapshot(
    bars: list[dict],
    current_price: Optional[float] = None,
    prev_close: Optional[float] = None,
    quote_feats: Optional[dict] = None,
) -> dict:
    """
    特征计算编排入口。

    调用 features.compute_reference_snapshot 计算基础指标，
    再合并盘口特征（如有），返回带版本号的完整快照。

    :param bars: K 线列表
    :param current_price: 当前价格（None 时取最后一根 close）
    :param prev_close: 昨收价
    :param quote_feats: 盘口特征 dict（可选）
    :return: 包含 _features_version 和 _schema_version 的快照 dict
    """
    snap = compute_reference_snapshot(bars, current_price, prev_close)
    if not snap:
        return {}

    if quote_feats:
        snap = merge_with_reference_snapshot(snap, quote_feats)

    # 标注版本号
    snap["_features_version"] = FEATURES_VERSION
    snap["_schema_version"] = SNAPSHOT_SCHEMA_VERSION
    return snap
