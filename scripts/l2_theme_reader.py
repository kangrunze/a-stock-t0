#!/usr/bin/env python3
"""
L2 题材数据统一读取器
=====================
P1-2 修复：统一 market_layer.read_themes_v17() 和 t_risk_guard.read_theme_state()
两处各自猜测 ashare-sop-engine L2 输出文件名的重复实现。

⚠️ 待核实：当前候选文件名（themes_v17.json / theme_hypothesis_*.json）均基于
早期对 ashare-sop-engine 输出结构的猜测，尚未对照真实仓库结构验证。
如果实际文件名或字段结构不同，只需修改本模块，调用方无需改动。

提供两个接口:
  - get_themes_snapshot(): 返回完整题材快照 dict（供 market_layer 使用）
  - get_theme_state(name): 查找特定题材的状态（供 t_risk_guard 使用）

独立性：L2 文件不存在时返回 None / "unknown"，不影响 L5 运行。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 候选文件路径（按优先级尝试）：
# 1. data/themes_v17.json — market_layer 原来猜测的路径
# 2. outputs/theme_hypothesis_{今日}.json — t_risk_guard 原来猜测的路径
# 3. outputs/theme_hypothesis_latest.json — t_risk_guard 的 fallback
# 4. 环境变量 L2_THEMES_FILE 指定的路径（允许外部覆盖）
_CANDIDATE_FILES: list[Path] = []

# 环境变量覆盖（最高优先级）
_env_override = os.environ.get("L2_THEMES_FILE")
if _env_override:
    _CANDIDATE_FILES.append(Path(_env_override))

# 固定候选
_CANDIDATE_FILES.extend([
    PROJECT_ROOT / "data" / "themes_v17.json",
    PROJECT_ROOT / "outputs" / f"theme_hypothesis_{datetime.now().strftime('%Y-%m-%d')}.json",
    PROJECT_ROOT / "outputs" / "theme_hypothesis_latest.json",
])


def _load_first_available() -> Optional[dict]:
    """尝试所有候选文件，返回第一个能成功加载的 JSON dict。"""
    for cand in _CANDIDATE_FILES:
        try:
            if cand.exists():
                with open(cand, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
    return None


def get_themes_snapshot() -> Optional[dict]:
    """
    返回完整题材快照 dict（供 market_layer 计算板块热度/概念排名）。

    文件不存在时返回 None（a-t0 独立运行模式）。
    """
    return _load_first_available()


def get_theme_state(theme_name: Optional[str]) -> str:
    """
    查找特定题材的状态。

    返回值 ∈ {"启动", "发酵", "高潮", "分歧", "退潮", "unknown"}。
    "unknown" 视为非退潮（不影响 T 操作）。

    查找逻辑（兼容两种可能的字段结构）:
      1. data["theme_states"][name]["state"]
      2. data["themes"] 列表中 name 匹配项的 "state" 字段
    """
    if not theme_name:
        return "unknown"

    data = _load_first_available()
    if data is None:
        return "unknown"

    # 结构1: theme_states dict
    theme_states = data.get("theme_states", {})
    if isinstance(theme_states, dict):
        state = theme_states.get(theme_name, {}).get("state")
        if state:
            return state

    # 结构2: themes 列表
    themes = data.get("themes", [])
    if isinstance(themes, list):
        for t in themes:
            if t.get("name") == theme_name:
                return t.get("state", "unknown")

    return "unknown"


if __name__ == "__main__":
    print(f"候选文件路径（按优先级）:")
    for i, cand in enumerate(_CANDIDATE_FILES):
        exists = "✓" if cand.exists() else "✗"
        print(f"  [{i}] {exists} {cand}")

    snap = get_themes_snapshot()
    if snap:
        print(f"\n已加载题材快照，顶层键: {list(snap.keys())}")
        # 尝试查找一个测试题材
        test_state = get_theme_state("测试题材")
        print(f"测试题材状态: {test_state}")
    else:
        print("\n未找到 L2 题材文件（独立运行模式）")
