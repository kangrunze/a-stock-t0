#!/usr/bin/env python3
"""
L5 持仓状态追踪器
==================
positions.json 的唯一读写入口。所有写操作加文件锁，避免手动更新
与脚本自动更新并发覆盖。

positions.json 结构 (Single Source of Truth):
{
  "600xxx.SH": {
    "base_shares": 3000,           # 底仓股数（T+1已解锁，可卖）
    "avg_cost": 12.35,             # 底仓成本价
    "entry_date": "2026-07-15",
    "sector_tag": "机器人概念",       # 关联 L2 题材（可选）
    "t_eligible": true,            # 是否允许做T
    "today_t_state": {
      "locked_shares": 0,          # 今日新买入、当天不可卖的股份数
      "t_trades_today": 0,         # 今日已做T次数
      "net_position_delta": 0      # 相对底仓的净增减
    }
  }
}

独立性：本模块不依赖 L1/L2/L3/L4。positions.json 是 L5 唯一硬依赖。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Iterator, Optional

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
POSITIONS_FILE = PROJECT_ROOT / "data" / "positions.json"
LOCK_FILE = POSITIONS_FILE.with_suffix(".json.lock")


# ═══════════════════════════════════════════════════════════════
# 文件锁（跨平台）
# ═══════════════════════════════════════════════════════════════
@contextmanager
def _file_lock(lock_path: Path = LOCK_FILE, timeout: float = 5.0) -> Iterator[None]:
    """
    跨平台文件锁。Windows 用 msvcrt.locking，Linux/Mac 用 fcntl.flock。
    超时未获取锁则抛出 TimeoutError。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.time() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, IOError):
                if time.time() > deadline:
                    raise TimeoutError(f"file_lock timeout: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


# ═══════════════════════════════════════════════════════════════
# 读写 API
# ═══════════════════════════════════════════════════════════════
def load_positions(path: Path = POSITIONS_FILE) -> dict:
    """加载所有持仓状态。文件不存在或损坏返回 {}。"""
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] load_positions failed: {e}", file=sys.stderr)
    return {}


def save_positions(positions: dict, path: Path = POSITIONS_FILE) -> None:
    """原子写入持仓状态（加文件锁）。仅用于一次性覆盖写，不涉及读-改-写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock():
        # 原子写：先写临时文件，再 rename
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def _atomic_update(mutate_fn, path: Path = POSITIONS_FILE) -> None:
    """
    原子读-改-写：在同一个文件锁内完成 load → mutate → save。

    P0-3 修复：apply_t_trade / reset_today_state / set_t_eligible 之前
    是 load_positions()（无锁）→ 改 dict → save_positions()（有锁），
    锁只包住了写，读-改-写窗口期内并发调用会互相覆盖丢失更新。

    本函数把整个读-改-写包在同一个 _file_lock() 块里，mutate_fn 在内存中
    修改 positions dict，修改完成后在同一锁内写入文件。

    参数:
      mutate_fn(positions: dict) -> None: 在内存中修改 positions dict
    """
    with _file_lock():
        positions = load_positions(path)
        mutate_fn(positions)
        # 原子写（不再调用 save_positions，因为已经在锁内，避免重入死锁）
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)


def get_position(code: str, path: Path = POSITIONS_FILE) -> Optional[dict]:
    """读取单只股票的持仓状态。"""
    return load_positions(path).get(code)


def get_sellable_shares(code: str, path: Path = POSITIONS_FILE) -> int:
    """
    计算当前可卖股份数 = base_shares - today_t_state.locked_shares。
    T+1 约束的硬性体现：今日新买的股份当天不可卖。
    """
    pos = get_position(code, path)
    if not pos:
        return 0
    base = int(pos.get("base_shares", 0))
    locked = int(pos.get("today_t_state", {}).get("locked_shares", 0))
    return max(0, base - locked)


def get_t_trades_today(code: str, path: Path = POSITIONS_FILE) -> int:
    """读取今日已做T次数。"""
    pos = get_position(code, path)
    if not pos:
        return 0
    return int(pos.get("today_t_state", {}).get("t_trades_today", 0))


def get_net_position_delta(code: str, path: Path = POSITIONS_FILE) -> int:
    """读取相对底仓的净增减（用于尾盘平衡检查）。"""
    pos = get_position(code, path)
    if not pos:
        return 0
    return int(pos.get("today_t_state", {}).get("net_position_delta", 0))


# ═══════════════════════════════════════════════════════════════
# T 操作后状态更新
# ═══════════════════════════════════════════════════════════════
def apply_t_trade(
    code: str,
    direction: str,
    shares: int,
    price: float,
    path: Path = POSITIONS_FILE,
) -> None:
    """
    在一笔 T 交易完成后更新持仓状态。

    direction:
      - "sell"        正T 卖出底仓 / 反T 卖出老仓
                      → locked_shares 不变（卖的是老仓）
                      → net_position_delta -= shares
                      → t_trades_today += 1
      - "buy"         反T 买入 / 正T 买回
                      → locked_shares += shares（T+1 锁定）
                      → net_position_delta += shares
                      → t_trades_today += 1（反T 算一次完整 T；正T 买回也算一次）

    注意：调用方必须先通过 t_risk_guard 校验，本函数不做风控。
    P0-3: 使用 _atomic_update 保证读-改-写原子性，防止并发覆盖。
    """
    if direction not in {"buy", "sell"}:
        raise ValueError(f"direction must be 'buy' or 'sell', got {direction}")
    if shares <= 0:
        raise ValueError(f"shares must be positive, got {shares}")

    def _mutate(positions: dict) -> None:
        if code not in positions:
            raise KeyError(f"position not found: {code}")
        pos = positions[code]
        today = pos.setdefault("today_t_state", {})
        if direction == "buy":
            today["locked_shares"] = int(today.get("locked_shares", 0)) + shares
            today["net_position_delta"] = int(today.get("net_position_delta", 0)) + shares
        else:  # sell
            today["net_position_delta"] = int(today.get("net_position_delta", 0)) - shares
        today["t_trades_today"] = int(today.get("t_trades_today", 0)) + 1
        today["last_trade_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today["last_trade_direction"] = direction
        today["last_trade_shares"] = shares
        today["last_trade_price"] = price

    _atomic_update(_mutate, path)


def reset_today_state(path: Path = POSITIONS_FILE) -> int:
    """
    每个交易日开盘前调用：清零所有持仓的 today_t_state，
    并把昨日的 locked_shares 转入 base_shares（T+1 已解锁）。

    返回重置的持仓数。
    P0-3: 使用 _atomic_update 保证读-改-写原子性。
    """
    today_str = date.today().isoformat()
    count_holder = {"count": 0}

    def _mutate(positions: dict) -> None:
        if not positions:
            return
        for code, pos in positions.items():
            today = pos.get("today_t_state", {})
            # 昨日买入的股份今日解锁，并入 base_shares
            yesterday_locked = int(today.get("locked_shares", 0))
            if yesterday_locked > 0:
                pos["base_shares"] = int(pos.get("base_shares", 0)) + yesterday_locked
            pos["today_t_state"] = {
                "locked_shares": 0,
                "t_trades_today": 0,
                "net_position_delta": 0,
                "reset_date": today_str,
            }
            count_holder["count"] += 1

    _atomic_update(_mutate, path)
    return count_holder["count"]


def set_t_eligible(code: str, eligible: bool, path: Path = POSITIONS_FILE) -> None:
    """手动/外部系统设置 t_eligible 状态（例如 L1/L2 熔断联动）。
    P0-3: 使用 _atomic_update 保证读-改-写原子性。
    """
    def _mutate(positions: dict) -> None:
        if code not in positions:
            return
        positions[code]["t_eligible"] = eligible

    _atomic_update(_mutate, path)


# ═══════════════════════════════════════════════════════════════
# 示例 / 初始化
# ═══════════════════════════════════════════════════════════════
def init_sample_positions(path: Path = POSITIONS_FILE) -> None:
    """初始化示例持仓（用于测试 / 回测样例）。"""
    sample = {
        "600xxx.SH": {
            "base_shares": 3000,
            "avg_cost": 12.35,
            "entry_date": "2026-07-15",
            "sector_tag": "机器人概念",
            "t_eligible": True,
            "today_t_state": {
                "locked_shares": 0,
                "t_trades_today": 0,
                "net_position_delta": 0,
            },
        },
    }
    save_positions(sample, path)
    print(f"[OK] sample positions written to {path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="L5 position tracker CLI")
    parser.add_argument("--init-sample", action="store_true", help="写入示例持仓")
    parser.add_argument("--show", action="store_true", help="打印当前持仓")
    parser.add_argument("--reset-today", action="store_true", help="清零今日 T 状态")
    args = parser.parse_args()

    if args.init_sample:
        init_sample_positions()
    elif args.show:
        positions = load_positions()
        print(json.dumps(positions, ensure_ascii=False, indent=2))
    elif args.reset_today:
        n = reset_today_state()
        print(f"[OK] reset today_t_state for {n} positions")
    else:
        parser.print_help()
