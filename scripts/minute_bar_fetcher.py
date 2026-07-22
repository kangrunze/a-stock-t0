#!/usr/bin/env python3
"""
L5 分钟K线获取器
==================
从 westock-data CLI 或本地 CSV 获取/合成 1 分钟 K 线数据。
支持两种数据源：
  1. westock-data CLI（实盘）
  2. 本地 CSV 文件（回测/离线）

数据结构（1 分钟 K 线）:
[
  {"time": "2026-07-22 09:31:00", "open": 10.00, "high": 10.05,
   "low": 9.98, "close": 10.03, "volume": 12000, "amount": 120360.0},
  ...
]

独立性：不依赖 L1/L2/L3/L4。仅依赖 westock-data CLI（实盘）或 CSV 文件（回测）。
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MINUTE_DATA_DIR = PROJECT_ROOT / "data" / "minute_bars"

# westock-data CLI（与 L3 一致）
WESTOCK_NODE = os.environ.get(
    "WESTOCK_NODE",
    str(Path.home() / ".workbuddy/binaries/node/versions/22.22.2/node.exe"),
)
WESTOCK_DIR = os.environ.get(
    "WESTOCK_DIR",
    "D:/Users/kangrunze/AppData/Local/Programs/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/westock-data",
)
WESTOCK_SCRIPT = os.path.join(WESTOCK_DIR, "scripts", "index.js")


# ═══════════════════════════════════════════════════════════════
# 代码格式转换
# ═══════════════════════════════════════════════════════════════
def to_westock_symbol(code: str) -> str:
    """将 6 位 A 股代码转换为 westock 所需的 sh/sz 前缀格式。"""
    if code.startswith(("sh", "sz", "bj", "pt")):
        return code
    if len(code) == 6 and code[0] == "6":
        return f"sh{code}"
    if len(code) == 6 and code[0] in {"0", "2", "3"}:
        return f"sz{code}"
    if len(code) == 6 and code[0] in {"4", "8"}:
        return f"bj{code}"
    return code


def strip_market_prefix(code: str) -> str:
    """将 sh/sz/bj 前缀代码映射回 6 位代码。"""
    if code.startswith(("sh", "sz", "bj")) and len(code) == 8:
        return code[2:]
    return code


# ═══════════════════════════════════════════════════════════════
# westock-data CLI 调用
# ═══════════════════════════════════════════════════════════════
def _run_westock(cmd: str) -> list[dict]:
    """调用 westock-data CLI 并以 JSON 解析返回。"""
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(WESTOCK_DIR, "node_modules")
    env["PYTHONIOENCODING"] = "utf-8"
    full_cmd = [WESTOCK_NODE, WESTOCK_SCRIPT] + cmd.split() + ["--raw"]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            env=env,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return []
        data = json.loads(stdout)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            items = data["data"]
            if isinstance(items, list):
                flat = []
                for item in items:
                    if isinstance(item, dict) and "data" in item:
                        flat.append(item["data"])
                    else:
                        flat.append(item)
                return flat
        return []
    except Exception as e:
        print(f"[WARN] westock-data call failed: {e}", file=sys.stderr)
        return []


def fetch_realtime_minute_bars(code: str, limit: int = 240) -> list[dict]:
    """
    从 westock-data 获取当日 1 分钟 K 线。

    返回格式统一为:
      [{"time": "HH:MM:SS" or "YYYY-MM-DD HH:MM:SS",
        "open": float, "high": float, "low": float, "close": float,
        "volume": int, "amount": float}, ...]

    按时间升序返回（最旧在前，最新在后）。
    """
    symbol = to_westock_symbol(code)
    data = _run_westock(f"kline {symbol} --period 1m --limit {limit}")
    bars = []
    for item in data:
        # 兼容不同字段名
        bar = {
            "time": item.get("time") or item.get("datetime") or item.get("date", ""),
            "open": float(item.get("open", 0)),
            "high": float(item.get("high", 0)),
            "low": float(item.get("low", 0)),
            "close": float(item.get("close") or item.get("last", 0)),
            "volume": int(float(item.get("volume", 0))),
            "amount": float(item.get("amount", 0)),
        }
        if bar["close"] > 0:
            bars.append(bar)
    return bars


def fetch_realtime_quote(code: str) -> Optional[dict]:
    """获取实时报价（含涨停价、跌停价、昨收等）。"""
    symbol = to_westock_symbol(code)
    data = _run_westock(f"quote {symbol}")
    if not data:
        return None
    item = data[0] if isinstance(data, list) else data
    return {
        "code": code,
        "price": float(item.get("price", 0)),
        "prev_close": float(item.get("prev_close", 0)),
        "change_percent": float(item.get("change_percent", 0)),
        "volume": int(float(item.get("volume", 0))),
        "amount": float(item.get("amount", 0)),
        "high": float(item.get("high", 0)),
        "low": float(item.get("low", 0)),
        "open": float(item.get("open", 0)),
        # 涨停价/跌停价：westock 通常不直接返回，需根据 prev_close 计算
        "limit_up": round(float(item.get("prev_close", 0)) * 1.1, 2),
        "limit_down": round(float(item.get("prev_close", 0)) * 0.9, 2),
    }


# ═══════════════════════════════════════════════════════════════
# 本地 CSV 数据源（回测用）
# ═══════════════════════════════════════════════════════════════
def load_minute_bars_from_csv(
    code: str,
    trading_date: str,
    data_dir: Path = MINUTE_DATA_DIR,
) -> list[dict]:
    """
    从本地 CSV 加载某只股票某日的 1 分钟 K 线。

    CSV 文件路径约定: {data_dir}/{code}_{trading_date}.csv
    CSV 字段: time,open,high,low,close,volume,amount
              time 格式: HH:MM:SS 或 YYYY-MM-DD HH:MM:SS
    """
    csv_path = data_dir / f"{code}_{trading_date}.csv"
    if not csv_path.exists():
        return []
    bars = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bar = {
                "time": row.get("time", ""),
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": int(float(row.get("volume", 0))),
                "amount": float(row.get("amount", 0)),
            }
            if bar["close"] > 0:
                bars.append(bar)
    return bars


def save_minute_bars_to_csv(
    code: str,
    trading_date: str,
    bars: list[dict],
    data_dir: Path = MINUTE_DATA_DIR,
) -> None:
    """将 1 分钟 K 线保存到 CSV（供回测复用）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / f"{code}_{trading_date}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "open", "high", "low", "close", "volume", "amount"])
        for bar in bars:
            writer.writerow([
                bar["time"], bar["open"], bar["high"], bar["low"],
                bar["close"], bar["volume"], bar["amount"],
            ])


# ═══════════════════════════════════════════════════════════════
# 数据时效性检查（防陈旧价格）
# ═══════════════════════════════════════════════════════════════
def check_bar_freshness(
    bars: list[dict],
    now: Optional[datetime] = None,
    max_delay_seconds: int = 120,
) -> bool:
    """
    检查分钟数据是否新鲜。最新一根 K 线的时间距 now 超过 max_delay_seconds
    则视为陈旧，返回 False。

    用于实盘：拿不到最新分钟数据时绝不能用上一次的陈旧价格硬算 VWAP。
    """
    if not bars:
        return False
    if now is None:
        now = datetime.now()

    last_bar_time_str = bars[-1].get("time", "")
    # 尝试多种格式解析
    last_bar_time = None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%H:%M:%S", "%Y-%m-%dT%H:%M:%S"]:
        try:
            last_bar_time = datetime.strptime(last_bar_time_str, fmt)
            if fmt == "%H:%M:%S":
                # 只有时间，补今日日期
                last_bar_time = last_bar_time.replace(
                    year=now.year, month=now.month, day=now.day
                )
            break
        except ValueError:
            continue

    if last_bar_time is None:
        return False

    delay = (now - last_bar_time).total_seconds()
    return delay <= max_delay_seconds


# ═══════════════════════════════════════════════════════════════
# 一字板 / 涨跌停封死检测
# ═══════════════════════════════════════════════════════════════
def is_one_word_board(quote: dict) -> bool:
    """检测当日是否一字板（开盘=最高=最低=收盘且接近涨/跌停）。"""
    if not quote:
        return False
    o = quote.get("open", 0)
    h = quote.get("high", 0)
    l = quote.get("low", 0)
    c = quote.get("price", 0)
    if o <= 0:
        return False
    # 一字板：四价相同（允许极小误差）
    if abs(h - l) / o < 0.001 and abs(o - c) / o < 0.001:
        return True
    return False


def is_limit_up_locked(quote: dict, bars: list[dict], lookback: int = 5) -> bool:
    """
    检测是否涨停封死：当前价 == 涨停价 且最近 N 分钟几乎无成交量。
    """
    if not quote:
        return False
    price = quote.get("price", 0)
    limit_up = quote.get("limit_up", 0)
    if limit_up <= 0 or abs(price - limit_up) / limit_up > 0.001:
        return False
    # 检查最近 N 分钟成交量
    recent_bars = bars[-lookback:] if len(bars) >= lookback else bars
    if not recent_bars:
        return True  # 没有量能数据，谨慎起见视为封死
    avg_vol = sum(b["volume"] for b in recent_bars) / len(recent_bars)
    # 阈值：每分钟成交量 < 100 股视为封死（可调）
    return avg_vol < 100


def is_limit_down_locked(quote: dict, bars: list[dict], lookback: int = 5) -> bool:
    """检测是否跌停封死。"""
    if not quote:
        return False
    price = quote.get("price", 0)
    limit_down = quote.get("limit_down", 0)
    if limit_down <= 0 or abs(price - limit_down) / limit_down > 0.001:
        return False
    recent_bars = bars[-lookback:] if len(bars) >= lookback else bars
    if not recent_bars:
        return True
    avg_vol = sum(b["volume"] for b in recent_bars) / len(recent_bars)
    return avg_vol < 100


# ═══════════════════════════════════════════════════════════════
# 统一获取接口
# ═══════════════════════════════════════════════════════════════
def get_minute_bars(
    code: str,
    trading_date: Optional[str] = None,
    use_cache: bool = True,
) -> tuple[list[dict], Optional[dict]]:
    """
    统一获取 1 分钟 K 线 + 实时报价。

    实盘模式（trading_date=None）：
      - 从 westock-data 实时拉取
      - 自动缓存到 CSV（便于复盘）
    回测模式（trading_date="YYYY-MM-DD"）：
      - 优先从本地 CSV 加载
      - CSV 不存在则从 westock-data 拉取并缓存

    返回: (bars, quote) — bars 按时间升序；实盘模式 quote 不为 None
    """
    if trading_date is None:
        # 实盘
        bars = fetch_realtime_minute_bars(code, limit=240)
        quote = fetch_realtime_quote(code)
        if use_cache and bars:
            today_str = datetime.now().strftime("%Y-%m-%d")
            save_minute_bars_to_csv(code, today_str, bars)
        return bars, quote
    else:
        # 回测：优先本地 CSV
        bars = load_minute_bars_from_csv(code, trading_date)
        if bars:
            return bars, None
        # CSV 不存在则拉取并缓存
        bars = fetch_realtime_minute_bars(code, limit=240)
        if bars:
            save_minute_bars_to_csv(code, trading_date, bars)
        return bars, None


if __name__ == "__main__":
    # 简单自检
    import argparse

    parser = argparse.ArgumentParser(description="L5 minute bar fetcher")
    parser.add_argument("--code", default="600xxx.SH", help="股票代码")
    parser.add_argument("--date", default=None, help="交易日（回测模式）")
    args = parser.parse_args()

    bars, quote = get_minute_bars(args.code, args.date)
    print(f"[INFO] {args.code}: {len(bars)} bars")
    if bars:
        print(f"  first: {bars[0]}")
        print(f"  last:  {bars[-1]}")
    if quote:
        print(f"  quote: {quote}")
