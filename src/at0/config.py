"""
A-T0 配置加载器（config 层）
=============================
方案 v0.2 第八节："所有阈值集中配置，禁止散落在各脚本里硬编码"

从 config/thresholds.yaml 加载阈值，支持 overlay 覆盖（research/paper）。
各模块的 dataclass 默认值保留作为 fallback（yaml 不存在时使用）。

加载优先级：
  base(thresholds.yaml) → overlay(config/overlays/*.yaml) → dataclass 默认值

用法:
  from at0.config import load_signal_params
  params = load_signal_params()
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from .strategy import SignalParams, DEFAULT_PARAMS
from .risk import RiskParams, DEFAULT_RISK_PARAMS, CostModel, ExposurePolicy
from .backtest import BacktestParams
from .screener import ScreenerParams

# 项目根目录（src/at0/config.py → src/at0/ → src/ → 项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "thresholds.yaml"
OVERLAYS_DIR = CONFIG_DIR / "overlays"


def _load_yaml_file(path: Path) -> Optional[dict]:
    """加载单个 yaml 文件，失败返回 None。"""
    try:
        import yaml
    except ImportError:
        return None
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[WARN] config: 加载 {path} 失败: {e}", file=sys.stderr)
        return None


def _deep_merge(base: dict, overlay: dict) -> dict:
    """深度合并 overlay 到 base（overlay 覆盖 base）。"""
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_yaml() -> Optional[dict]:
    """加载 thresholds.yaml + overlay（如环境变量 AT0_OVERLAY 指定）。"""
    import os

    data = _load_yaml_file(CONFIG_FILE)
    if data is None:
        return None

    # 加载 overlay（默认无，通过环境变量 AT0_OVERLAY=research/paper 指定）
    overlay_name = os.environ.get("AT0_OVERLAY", "")
    if overlay_name:
        overlay_path = OVERLAYS_DIR / f"{overlay_name}.yaml"
        overlay_data = _load_yaml_file(overlay_path)
        if overlay_data:
            data = _deep_merge(data, overlay_data)

    return data


def load_signal_params() -> SignalParams:
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


def load_risk_params() -> RiskParams:
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


def load_backtest_params() -> BacktestParams:
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


def load_screener_params() -> ScreenerParams:
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


def load_cost_model() -> CostModel:
    """
    P0-1: 从 yaml 加载 CostModel，yaml 不存在时返回默认 base 场景。
    支持 scenario 字段选择 optimistic/base/pessimistic。
    """
    data = _load_yaml()
    if not data or "cost" not in data:
        return CostModel.base()
    cost = data["cost"]
    scenario = cost.get("scenario", "base")
    model = CostModel.from_scenario(scenario)
    kwargs = {}
    for k, v in cost.items():
        if k == "scenario":
            continue
        if hasattr(model, k):
            kwargs[k] = v
    if kwargs:
        kwargs["scenario"] = scenario
        return CostModel(**kwargs)
    return model


def load_exposure_policy() -> ExposurePolicy:
    """P0-3: 从 yaml 加载 ExposurePolicy，yaml 不存在时返回默认值。"""
    data = _load_yaml()
    if not data or "exposure_policy" not in data:
        return ExposurePolicy()
    ep = data["exposure_policy"]
    defaults = ExposurePolicy()
    kwargs = {}
    for k, v in ep.items():
        if hasattr(defaults, k):
            kwargs[k] = v
    return ExposurePolicy(**kwargs) if kwargs else defaults


if __name__ == "__main__":
    print(f"config file: {CONFIG_FILE}")
    print(f"exists: {CONFIG_FILE.exists()}")

    data = _load_yaml()
    if data is None:
        print("[WARN] yaml 不可用（未安装 PyYAML 或文件不存在），各模块将使用 dataclass 默认值")
    else:
        print(f"sections: {list(data.keys())}")
        sig = load_signal_params()
        print(f"signal params: ATR×={sig.vwap_dev_atr_multiplier}, RSI={sig.rsi_overbought}/{sig.rsi_oversold}")
        risk = load_risk_params()
        print(f"risk params: spread={risk.min_capture_spread}, max_t_size={risk.max_t_size_ratio}")
        cm = load_cost_model()
        print(f"cost model: scenario={cm.scenario}, round_trip_rate={cm.round_trip_cost_rate()}")
        ep = load_exposure_policy()
        print(f"exposure policy: max_holding_bars={ep.max_holding_bars}, require_opposite={ep.require_opposite_direction}")
