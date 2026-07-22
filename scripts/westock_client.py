#!/usr/bin/env python3
"""
westock-data CLI 统一客户端
============================
P2-1/P2-2: 收敛 minute_bar_fetcher / market_layer / stock_quote_features /
candidate_screener 四处重复的 westock CLI 调用逻辑。

环境变量:
  WESTOCK_NODE  — node 可执行文件路径（默认 ~/.workbuddy/...，可移植）
  WESTOCK_DIR   — westock-data 安装目录（**必须配置**，无硬编码默认值）
                  未设置时 run_westock() 会抛 RuntimeError 给出清晰提示。

独立性: 仅被需要实盘 westock 数据的模块使用；回测/测试不依赖此模块。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
# node 可执行文件：Path.home() 默认值可移植，保留
WESTOCK_NODE = os.environ.get(
    "WESTOCK_NODE",
    str(Path.home() / ".workbuddy/binaries/node/versions/22.22.2/node.exe"),
)

# westock-data 目录：P2-2 — 不再硬编码个人机器路径，必须通过环境变量配置
WESTOCK_DIR = os.environ.get("WESTOCK_DIR", "")


def _check_configured() -> None:
    """检查 WESTOCK_DIR 是否已配置。未配置时抛出 RuntimeError（懒检查，不影响 import）。"""
    if not WESTOCK_DIR:
        raise RuntimeError(
            "环境变量 WESTOCK_DIR 未设置。请在 .env 或系统环境变量中配置 WESTOCK_DIR，"
            "指向 westock-data 安装目录（例如 "
            "/path/to/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/westock-data）。"
            "如需指定 node 路径，另设 WESTOCK_NODE。"
        )


# ═══════════════════════════════════════════════════════════════
# 代码格式转换
# ═══════════════════════════════════════════════════════════════
def to_westock_symbol(code: str) -> str:
    """将 6 位 A 股代码转换为 westock 所需的 sh/sz/bj 前缀格式。"""
    if code.startswith(("sh", "sz", "bj", "pt")):
        return code
    if len(code) == 6 and code[0] == "6":
        return f"sh{code}"
    if len(code) == 6 and code[0] in {"0", "2", "3"}:
        return f"sz{code}"
    if len(code) == 6 and code[0] in {"4", "8"}:
        return f"bj{code}"
    return code


# ═══════════════════════════════════════════════════════════════
# CLI 调用
# ═══════════════════════════════════════════════════════════════
def run_westock(cmd: str, timeout: int = 45) -> Optional[object]:
    """
    调用 westock-data CLI，返回解析后的原始 JSON（dict / list / None）。

    参数:
      cmd: westock 子命令字符串（如 "changedist"、"sector ranking"、"quote sh600000"）
      timeout: 超时秒数

    返回: 解析后的 JSON 对象；空输出或异常时返回 None。
    """
    _check_configured()
    westock_script = os.path.join(WESTOCK_DIR, "scripts", "index.js")
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(WESTOCK_DIR, "node_modules")
    env["PYTHONIOENCODING"] = "utf-8"
    full_cmd = [WESTOCK_NODE, westock_script] + cmd.split() + ["--raw"]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None
        return json.loads(stdout)
    except Exception as e:
        print(f"[WARN] westock call failed (cmd={cmd}): {e}", file=sys.stderr)
        return None
