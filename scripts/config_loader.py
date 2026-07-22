#!/usr/bin/env python3
"""
阈值集中配置加载器
==================
方案 v0.2 第八节："所有阈值集中配置，禁止散落在各脚本里硬编码"

本模块提供从 config/thresholds.yaml 加载阈值的能力。
各模块的 dataclass 默认值保留作为 fallback（yaml 不存在时使用），
实盘入口优先用 load_*_params() 从 yaml 加载。

用法:
  from config_loader import load_signal_params
  params = load_signal_params()  # yaml 不存在时返回 SignalParams 默认值
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from t_signal_engine import SignalParams, DEFAULT_PARAMS
from t_risk_guard import RiskParams, DEFAULT_RISK_PARAMS
from backtest_t_strategy import BacktestParams
from candidate_screener import ScreenerParams

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"


def _load_yaml() -> Optional[dict]:
    """加载 thresholds.yaml，失败返回 None。"""
    try:
        import yaml
    except ImportError:
        return None
    if not CONFIG_FILE.exists():
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[WARN] config_loader: 加载 {CONFIG_FILE} 失败: {e}", file=sys.stderr)
        return None


def load_signal_params():
    """从 yaml 加载 SignalParams，yaml 不存在时返回默认值。"""
    data = _load_yaml()
    if not data or "signal" not in data:
        return DEFAULT_PARAMS
    sig = data["signal"]
    kwargs = {}
    for k, v in sig.items():
        if hasattr(DEFAULT_PARAMS, k):
            kwargs[k] = v
    return SignalParams(**kwargs) if kwargs else DEFAULT_PARAMS


def load_risk_params():
    """从 yaml 加载 RiskParams，yaml 不存在时返回默认值。"""
    data = _load_yaml()
    if not data or "risk" not in data:
        return DEFAULT_RISK_PARAMS
    risk = data["risk"]
    kwargs = {}
    for k, v in risk.items():
        if hasattr(DEFAULT_RISK_PARAMS, k):
            kwargs[k] = v
    return RiskParams(**kwargs) if kwargs else DEFAULT_RISK_PARAMS


def load_backtest_params():
    """从 yaml 加载 BacktestParams，yaml 不存在时返回默认值。"""
    data = _load_yaml()
    if not data or "backtest" not in data:
        return BacktestParams()
    bt = data["backtest"]
    defaults = BacktestParams()
    kwargs = {}
    for k, v in bt.items():
        if hasattr(defaults, k):
            kwargs[k] = v
    return BacktestParams(**kwargs) if kwargs else defaults


def load_screener_params():
    """从 yaml 加载 ScreenerParams，yaml 不存在时返回默认值。"""
    data = _load_yaml()
    if not data or "screener" not in data:
        return ScreenerParams()
    sc = data["screener"]
    defaults = ScreenerParams()
    kwargs = {}
    for k, v in sc.items():
        if hasattr(defaults, k):
            kwargs[k] = v
    return ScreenerParams(**kwargs) if kwargs else defaults


if __name__ == "__main__":
    print(f"config file: {CONFIG_FILE}")
    print(f"exists: {CONFIG_FILE.exists()}")

    # 测试 yaml 是否可加载（需 PyYAML）
    data = _load_yaml()
    if data is None:
        print("[WARN] yaml 不可用（未安装 PyYAML 或文件不存在），各模块将使用 dataclass 默认值")
    else:
        print(f"sections: {list(data.keys())}")
        sig = load_signal_params()
        print(f"signal params: ATR×={sig.vwap_dev_atr_multiplier}, RSI={sig.rsi_overbought}/{sig.rsi_oversold}")
        risk = load_risk_params()
        print(f"risk params: spread={risk.min_capture_spread}")
