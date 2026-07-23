"""
data 层合并模块
================
本模块由 scripts/ 下三个独立脚本合并而成，作为 data 层的统一出口：

  - westock_client        — westock-data CLI 封装（run_westock / to_westock_symbol）
  - minute_bar_fetcher    — 分钟线获取 + CSV 读写（fetch_realtime_minute_bars /
                            load_minute_bars_from_csv / get_minute_bars 等）
  - data_provider         — auto 回退多数据源适配器（eastmoney→mootdx→westock→baostock）

模块组成（对应文档 4.2 data 层规约）：
  - BarProvider Protocol          — 统一数据提供者接口（load 方法）
  - 4 个 _fetch_xxx provider 方法 — _fetch_eastmoney / _fetch_mootdx /
                                    _fetch_westock / _fetch_baostock
  - normalizer                    — normalize_code 代码归一化
  - validator                     — check_bar_freshness / is_one_word_board /
                                    is_limit_up_locked / is_limit_down_locked

合并说明：
  - westock_client 无本地依赖，原 import 保留
  - minute_bar_fetcher 原 `from westock_client import ...` 已删除（同文件直接调用）
  - data_provider 原 `from minute_bar_fetcher import ...` 及 sys.path 注入已删除
    （同文件直接调用）
  - 所有原始函数 / 类 / 常量的实现保持不变，未做任何逻辑修改
"""

from __future__ import annotations

import atexit
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol


# ═══════════════════════════════════════════════════════════════
# BarProvider Protocol（文档 4.2 data 层统一接口）
# ═══════════════════════════════════════════════════════════════
class BarProvider(Protocol):
    def load(self, request) -> list: ...


# ═══ data: westock_client（westock CLI 封装） ═══

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

# 未配置警告只打印一次，避免多只股票刷屏
_warned_not_configured = False


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
          WESTOCK_DIR 未配置时返回 None（打印警告，不抛异常，让调用方降级）。
    """
    try:
        _check_configured()
    except RuntimeError as e:
        global _warned_not_configured
        if not _warned_not_configured:
            print(f"[WARN] {e}", file=sys.stderr)
            _warned_not_configured = True
        return None
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


# ═══ data: minute_bar_fetcher（分钟线获取 + CSV 读写） ═══

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # src/at0/ -> src/ -> 项目根
MINUTE_DATA_DIR = PROJECT_ROOT / "data" / "minute_bars"


# ═══════════════════════════════════════════════════════════════
# 代码格式转换
# ═══════════════════════════════════════════════════════════════
def strip_market_prefix(code: str) -> str:
    """将 sh/sz/bj 前缀代码映射回 6 位代码。"""
    if code.startswith(("sh", "sz", "bj")) and len(code) == 8:
        return code[2:]
    return code


# ═══════════════════════════════════════════════════════════════
# westock-data CLI 调用（list 展平封装）
# ═══════════════════════════════════════════════════════════════
def _run_westock(cmd: str) -> list[dict]:
    """调用 westock CLI 并将返回展平为 list[dict]（适配 kline 的嵌套结构）。"""
    data = run_westock(cmd)
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


# ═══ data: data_provider（auto 回退：eastmoney→mootdx→westock→baostock） ═══

# ═══════════════════════════════════════════════════════════════
# 数据源标识
# ═══════════════════════════════════════════════════════════════
SRC_MOOTDX = "mootdx"
SRC_WESTOCK = "westock"
SRC_BAOSTOCK = "baostock"
SRC_EASTMONEY = "eastmoney"

# auto 模式回退顺序：eastmoney 优先（免依赖、盘中实时），baostock 兜底（历史多日）
AUTO_FALLBACK = [SRC_EASTMONEY, SRC_MOOTDX, SRC_WESTOCK, SRC_BAOSTOCK]


# ═══════════════════════════════════════════════════════════════
# 代码归一化
# ═══════════════════════════════════════════════════════════════
def normalize_code(code: str) -> dict:
    """
    将任意格式代码归一化为各数据源所需格式。

    支持输入: '600000' / 'sh.600000' / 'sh600000' / '600000.SH' / '000001'

    返回:
      {
        "pure": "600000",           # 6位纯代码
        "baostock": "sh.600000",    # BaoStock 格式
        "westock": "sh600000",      # westock-data 格式
        "mootdx": "600000",         # mootdx 格式（纯代码）
        "eastmoney": "1.600000",    # 东方财富 secid（沪=1. 深=0.）
        "market": 0,                # mootdx market: 0=沪 1=深
      }
    """
    s = code.strip().lower().replace(".sh", "").replace(".sz", "")
    s = s.replace("sh", "").replace("sz", "").replace(".", "")
    if not (len(s) == 6 and s.isdigit()):
        raise ValueError(f"无法解析股票代码: {code}")

    head = s[0]
    if head == "6":
        market, prefix = 0, "sh"
        em_market = 1  # 东方财富：沪市 1
    elif head in ("0", "3", "2"):
        market, prefix = 1, "sz"
        em_market = 0  # 东方财富：深市 0
    elif head in ("4", "8"):
        market, prefix = 1  # 北交所 mootdx 兼容深市通道，BaoStock 用 bj
        prefix = "bj"
        em_market = 0
    else:
        raise ValueError(f"未知代码前缀: {code}")

    return {
        "pure": s,
        "baostock": f"{prefix}.{s}",
        "westock": f"{prefix}{s}",
        "mootdx": s,
        "eastmoney": f"{em_market}.{s}",
        "market": market,
    }


# ═══════════════════════════════════════════════════════════════
# mootdx 适配器（真实 1 分钟线）
# ═══════════════════════════════════════════════════════════════
_mootdx_client = None


def _get_mootdx_client():
    """懒加载 mootdx 客户端（单例）。连不上返回 None。"""
    global _mootdx_client
    if _mootdx_client is not None:
        return _mootdx_client
    try:
        from mootdx.quotes import Quotes
        _mootdx_client = Quotes.factory(market="std", bestip=True)
    except Exception as e:
        print(f"[data_provider] mootdx 初始化失败: {e}")
        _mootdx_client = None
    return _mootdx_client


def _fetch_mootdx(code: str, trading_date: str) -> tuple[list[dict], float]:
    """
    mootdx 拉取指定交易日 1 分钟线 + 昨收。

    mootdx 的 client.bars 返回最近 N 根 K 线，需拉足够多后按日期过滤。
    """
    nc = normalize_code(code)
    client = _get_mootdx_client()
    if client is None:
        return [], 0.0

    # 拉 1 分钟线：offset 取 7 个交易日 * 240 根 + 缓冲
    try:
        df = client.bars(symbol=nc["mootdx"], frequency=8, offset=2000)
        if df is None or len(df) == 0:
            return [], 0.0
        # mootdx 列名: datetime/open/high/low/close/vol/amount/...
        # datetime 可能是字符串或 Timestamp
        bars: list[dict] = []
        for _, row in df.iterrows():
            dt = row.get("datetime")
            if dt is None:
                continue
            # 解析 datetime，按交易日过滤
            try:
                ts = pd_timestamp(dt)
            except Exception:
                continue
            if ts.strftime("%Y-%m-%d") != trading_date:
                continue
            bars.append({
                "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(float(row["vol"])),
                "amount": float(row["amount"]),
            })
        bars.sort(key=lambda b: b["time"])

        # prev_close：从日K取 trading_date 前一交易日 close
        prev_close = _mootdx_prev_close(client, nc["mootdx"], trading_date)
        return bars, prev_close
    except Exception as e:
        print(f"[data_provider] mootdx 拉取失败: {e}")
        return [], 0.0


def _mootdx_prev_close(client, symbol: str, trading_date: str) -> float:
    """从 mootdx 日K找 trading_date 前一交易日收盘价。"""
    try:
        df = client.bars(symbol=symbol, frequency=9, offset=30)
        if df is None or len(df) == 0:
            return 0.0
        # 找 trading_date 前一交易日
        dates_closes = []
        for _, row in df.iterrows():
            dt = row.get("datetime")
            if dt is None:
                continue
            try:
                ts = pd_timestamp(dt)
                dates_closes.append((ts.strftime("%Y-%m-%d"), float(row["close"])))
            except Exception:
                continue
        dates_closes.sort()
        prev_close = 0.0
        for d, c in dates_closes:
            if d < trading_date:
                prev_close = c
            else:
                break
        return prev_close
    except Exception:
        return 0.0


def pd_timestamp(dt):
    """把 mootdx 的 datetime 字段转成 datetime。"""
    import pandas as pd
    if isinstance(dt, (pd.Timestamp, datetime)):
        return dt.to_pydatetime() if isinstance(dt, pd.Timestamp) else dt
    # 字符串：尝试常见格式
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(str(dt), fmt)
        except ValueError:
            continue
    # 最后交给 pandas
    return pd.to_datetime(dt).to_pydatetime()


# ═══════════════════════════════════════════════════════════════
# westock-data CLI 适配器（仅当日实时 1 分钟线）
# ═══════════════════════════════════════════════════════════════
def _fetch_westock(code: str, trading_date: str) -> tuple[list[dict], float]:
    """
    westock-data CLI：只能拉当日实时，历史日期返回空。
    prev_close 从实时报价取。
    """
    nc = normalize_code(code)
    today = datetime.now().strftime("%Y-%m-%d")
    if trading_date != today:
        return [], 0.0  # westock 不支持历史

    try:
        bars = fetch_realtime_minute_bars(nc["westock"], limit=240)
        quote = fetch_realtime_quote(nc["westock"])
        prev_close = quote.get("prev_close", 0.0) if quote else 0.0
        # westock 的 time 可能只有 HH:MM:SS，补日期
        fixed = []
        for b in bars:
            t = b.get("time", "")
            if len(t) <= 8:  # HH:MM:SS
                t = f"{trading_date} {t}"
            fixed.append({**b, "time": t})
        return fixed, prev_close
    except Exception as e:
        print(f"[data_provider] westock 拉取失败: {e}")
        return [], 0.0


# ═══════════════════════════════════════════════════════════════
# BaoStock 适配器（5 分钟线 + 日线 preclose，兜底）
# ═══════════════════════════════════════════════════════════════
_bs_logged_in = False


def _ensure_bs_login():
    """BaoStock 懒登录（进程级单例，atexit 退出）。"""
    global _bs_logged_in
    if _bs_logged_in:
        return
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    _bs_logged_in = True
    atexit.register(_bs_logout)


def _bs_logout():
    global _bs_logged_in
    if _bs_logged_in:
        try:
            import baostock as bs
            bs.logout()
        except Exception:
            pass
        _bs_logged_in = False


def _parse_bs_time(t: str) -> str:
    """'20260715093500000' -> '2026-07-15 09:35:00'。"""
    if not t or len(t) < 14:
        return t
    return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"


def _fetch_baostock(code: str, trading_date: str) -> tuple[list[dict], float]:
    """
    BaoStock 拉取指定交易日 5 分钟线 + 昨收（前复权）。

    注意：BaoStock 最细 5 分钟，返回 meta.frequency='5min'。
    """
    nc = normalize_code(code)
    import baostock as bs
    _ensure_bs_login()

    # 5 分钟线
    fields = "date,time,open,high,low,close,volume,amount"
    rs = bs.query_history_k_data_plus(
        nc["baostock"], fields,
        start_date=trading_date, end_date=trading_date,
        frequency="5", adjustflag="2",  # 前复权，保证日内价格口径一致
    )
    if rs.error_code != "0":
        raise RuntimeError(f"BaoStock 查询失败: {rs.error_msg}")

    bars: list[dict] = []
    bs_fields = rs.fields
    while rs.next():
        row = dict(zip(bs_fields, rs.get_row_data()))
        close = float(row.get("close", 0))
        if close <= 0:
            continue
        bars.append({
            "time": _parse_bs_time(row.get("time", "")),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": close,
            "volume": int(float(row["volume"])),
            "amount": float(row["amount"]),
        })
    bars.sort(key=lambda b: b["time"])

    # prev_close：日线 preclose 字段
    prev_close = _baostock_prev_close(nc["baostock"], trading_date)
    return bars, prev_close


def _baostock_prev_close(baostock_code: str, trading_date: str) -> float:
    """BaoStock 日线 preclose 字段直接给前一交易日收盘（复权后）。"""
    import baostock as bs
    _ensure_bs_login()
    rs = bs.query_history_k_data_plus(
        baostock_code, "date,close,preclose",
        start_date=trading_date, end_date=trading_date,
        frequency="d", adjustflag="2",
    )
    if rs.error_code != "0":
        return 0.0
    while rs.next():
        row = rs.get_row_data()
        # fields = [date, close, preclose]
        try:
            return float(row[2])
        except (IndexError, ValueError):
            return 0.0
    return 0.0


# ═══════════════════════════════════════════════════════════════
# 东方财富适配器（实时1分钟线 + 日线 prev_close，免第三方依赖）
# ═══════════════════════════════════════════════════════════════
_EM_KLINE_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"


def _fetch_eastmoney(code: str, trading_date: str) -> tuple[list[dict], float]:
    """
    东方财富拉取指定交易日 1 分钟线 + 昨收。

    注意：东方财富 klt=1 的 beg/end 参数无效，API 始终返回最近交易日数据。
    因此历史日期需拉取足够多 K 线（lmt=N×240）再按日期过滤。

    优势：免第三方依赖（仅 requests），支持当日盘中实时，也支持历史多日。
    返回格式与 baostock/mootdx 一致。
    """
    import requests
    from datetime import datetime, timedelta

    nc = normalize_code(code)
    secid = nc["eastmoney"]
    target_date = trading_date  # YYYY-MM-DD
    today = datetime.now().strftime("%Y-%m-%d")

    # 估算需要拉取的 K 线数：每交易日最多 240 根，按天数差计算
    days_diff = (datetime.now() - datetime.strptime(target_date, "%Y-%m-%d")).days
    n_days = max(1, days_diff + 1)  # 至少 1 天
    lmt = min(n_days * 240, 5000)  # 上限 5000 根避免响应过大

    # 1. 拉 1 分钟线（klt=1），lmt 控制根数
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "1",       # 1=1分钟
        "fqt": "1",       # 1=前复权
        "beg": "19900101",  # 东方财富忽略此参数，但必须传
        "end": "20500101",
        "lmt": str(lmt),
    }
    try:
        r = requests.get(_EM_KLINE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("data") or {}
        klines = data.get("klines") or []
    except Exception as e:
        print(f"[data_provider] eastmoney 拉取失败: {e}")
        return [], 0.0

    # 2. 按目标日期过滤
    bars: list[dict] = []
    for line in klines:
        # 格式: "2026-07-23 10:52,9.01,9.00,9.01,8.99,3161,2845908.00,0.22"
        parts = line.split(",")
        if len(parts) < 7:
            continue
        if not parts[0].startswith(target_date):
            continue
        try:
            bars.append({
                "time": f"{parts[0]}:00",  # "2026-07-23 10:52" → "2026-07-23 10:52:00"
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": int(float(parts[5]) * 100),  # 东方财富单位"手"→股
                "amount": float(parts[6]),
            })
        except (ValueError, IndexError):
            continue

    if not bars:
        return [], 0.0

    # 3. prev_close：拉日线，取 target_date 前一交易日收盘
    prev_close = _eastmoney_prev_close(secid, target_date)
    return bars, prev_close


def _eastmoney_prev_close(secid: str, trading_date: str) -> float:
    """东方财富日线取 trading_date 前一交易日收盘（前复权）。"""
    import requests
    from datetime import datetime, timedelta

    # 往前推 10 天确保覆盖周末/假日
    end = datetime.strptime(trading_date, "%Y-%m-%d")
    beg = end - timedelta(days=10)
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101",     # 101=日线
        "fqt": "1",
        "beg": beg.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "lmt": "10",
    }
    try:
        r = requests.get(_EM_KLINE_URL, params=params, timeout=8)
        r.raise_for_status()
        klines = (r.json().get("data") or {}).get("klines") or []
    except Exception:
        return 0.0

    # 找 trading_date 之前最近一条的 close
    prev_close = 0.0
    for line in klines:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        d = parts[0]
        if d < trading_date:
            try:
                prev_close = float(parts[2])  # 日线 close
            except ValueError:
                continue
    return prev_close


# ═══════════════════════════════════════════════════════════════
# 统一入口（auto 回退）
# ═══════════════════════════════════════════════════════════════
def fetch_minute_bars(
    code: str,
    trading_date: str,
    source: str = "auto",
) -> tuple[list[dict], float, dict]:
    """
    统一获取个股某交易日分钟K线 + 昨收。

    :param code: 任意格式代码
    :param trading_date: 'YYYY-MM-DD'
    :param source: 'auto' | 'mootdx' | 'westock' | 'baostock' | 'eastmoney'
    :return: (bars, prev_close, meta)
        bars: 升序，格式 {time, open, high, low, close, volume, amount}
        meta: {"source": 实际数据源, "frequency": "1min"|"5min",
               "trading_date": str, "bars_count": int}
    """
    sources = AUTO_FALLBACK if source == "auto" else [source]
    errors: list[str] = []

    for src in sources:
        try:
            if src == SRC_MOOTDX:
                bars, pc = _fetch_mootdx(code, trading_date)
                freq = "1min"
            elif src == SRC_WESTOCK:
                bars, pc = _fetch_westock(code, trading_date)
                freq = "1min"
            elif src == SRC_BAOSTOCK:
                bars, pc = _fetch_baostock(code, trading_date)
                freq = "5min"
            elif src == SRC_EASTMONEY:
                bars, pc = _fetch_eastmoney(code, trading_date)
                freq = "1min"
            else:
                continue

            if bars and pc > 0:
                meta = {
                    "source": src, "frequency": freq,
                    "trading_date": trading_date, "bars_count": len(bars),
                }
                return bars, pc, meta
            else:
                errors.append(f"{src}: bars={len(bars)} prev_close={pc}")
        except Exception as e:
            errors.append(f"{src}: {e}")
            continue

    # 全部失败
    return [], 0.0, {
        "source": None, "frequency": None,
        "trading_date": trading_date, "bars_count": 0,
        "errors": errors,
    }


def fetch_multi_day(
    code: str,
    start_date: str,
    end_date: str,
    source: str = "auto",
) -> tuple[dict, dict, dict]:
    """
    拉取一段日期区间内每个交易日的分钟数据。

    交易日由数据源决定（拉不到的日期自动跳过）。

    :return: (daily_bars, daily_prev_closes, daily_meta)
        daily_bars: {date: [bars]}
        daily_prev_closes: {date: prev_close}
        daily_meta: {date: meta}
    """
    # 枚举自然日，逐日拉取（数据源会对非交易日返回空，自动跳过）
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    daily_bars: dict[str, list[dict]] = {}
    daily_prev_closes: dict[str, float] = {}
    daily_meta: dict[str, dict] = {}

    cur = start
    while cur <= end:
        d = cur.strftime("%Y-%m-%d")
        bars, pc, meta = fetch_minute_bars(code, d, source)
        if bars and pc > 0:
            daily_bars[d] = bars
            daily_prev_closes[d] = pc
            daily_meta[d] = meta
            print(f"[data_provider] {d} ← {meta['source']}({meta['frequency']}) "
                  f"{meta['bars_count']} bars, prev_close={pc:.4f}")
        cur = _next_day(cur)

    return daily_bars, daily_prev_closes, daily_meta


def _next_day(dt: datetime) -> datetime:
    from datetime import timedelta
    return dt + timedelta(days=1)


# ═══════════════════════════════════════════════════════════════
# CLI 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="统一分钟数据适配器")
    parser.add_argument("--code", default="600000", help="股票代码")
    parser.add_argument("--date", default=None, help="单日 YYYY-MM-DD")
    parser.add_argument("--start", default=None, help="区间起 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="区间止 YYYY-MM-DD")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"])
    args = parser.parse_args()

    if args.date:
        bars, pc, meta = fetch_minute_bars(args.code, args.date, args.source)
        print(f"\n=== {args.code} {args.date} ===")
        print(f"meta: {meta}")
        print(f"prev_close: {pc}")
        print(f"bars: {len(bars)}")
        if bars:
            print(f"  first: {bars[0]}")
            print(f"  last:  {bars[-1]}")
    elif args.start and args.end:
        db, dpc, dm = fetch_multi_day(args.code, args.start, args.end, args.source)
        print(f"\n=== {args.code} {args.start}~{args.end} ===")
        print(f"交易日数: {len(db)}")
        for d in sorted(db.keys()):
            m = dm[d]
            print(f"  {d}: {m['source']}({m['frequency']}) {m['bars_count']} bars")
    else:
        parser.print_help()


# ═══════════════════════════════════════════════════════════════
# data: l2_theme_reader（L2 题材数据统一读取器）
# ═══════════════════════════════════════════════════════════════
# P1-2 修复：统一 market_layer 和 t_risk_guard 两处各自猜测
# ashare-sop-engine L2 输出文件名的重复实现。
# 独立性：L2 文件不存在时返回 None / "unknown"，不影响 L5 运行。

# 候选文件路径（按优先级尝试）：
# 1. data/themes_v17.json — market_layer 原来猜测的路径
# 2. outputs/theme_hypothesis_{今日}.json — t_risk_guard 原来猜测的路径
# 3. outputs/theme_hypothesis_latest.json — t_risk_guard 的 fallback
# 4. 环境变量 L2_THEMES_FILE 指定的路径（允许外部覆盖）
_L2_CANDIDATE_FILES: list[Path] = []

_env_override = os.environ.get("L2_THEMES_FILE")
if _env_override:
    _L2_CANDIDATE_FILES.append(Path(_env_override))

_L2_CANDIDATE_FILES.extend([
    PROJECT_ROOT / "data" / "themes_v17.json",
    PROJECT_ROOT / "outputs" / f"theme_hypothesis_{datetime.now().strftime('%Y-%m-%d')}.json",
    PROJECT_ROOT / "outputs" / "theme_hypothesis_latest.json",
])


def _l2_load_first_available() -> Optional[dict]:
    """尝试所有候选文件，返回第一个能成功加载的 JSON dict。"""
    for cand in _L2_CANDIDATE_FILES:
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
    return _l2_load_first_available()


def get_theme_state(theme_name: Optional[str]) -> str:
    """
    查找特定题材的状态。
    返回值 ∈ {"启动", "发酵", "高潮", "分歧", "退潮", "unknown"}。
    "unknown" 视为非退潮（不影响 T 操作）。
    """
    if not theme_name:
        return "unknown"

    data = _l2_load_first_available()
    if data is None:
        return "unknown"

    theme_states = data.get("theme_states", {})
    if isinstance(theme_states, dict):
        state = theme_states.get(theme_name, {}).get("state")
        if state:
            return state

    themes = data.get("themes", [])
    if isinstance(themes, list):
        for t in themes:
            if t.get("name") == theme_name:
                return t.get("state", "unknown")

    return "unknown"
