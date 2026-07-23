#!/usr/bin/env python3
"""
统一分钟数据适配器（多数据源 → 统一出口）
========================================
数据源（按 auto 回退顺序）:
  1. mootdx   — 通达信协议，真实 1 分钟线，免登录（当前环境若连不上则回退）
  2. westock  — westock-data CLI，1 分钟线（仅当日实时，历史日期返回空）
  3. baostock — BaoStock，5 分钟线 + 日线 preclose（兜底，覆盖历史多日）

统一出口 bars 格式（与 minute_bar_fetcher / backtest_t_strategy 完全一致）:
  {"time": "YYYY-MM-DD HH:MM:SS",
   "open": float, "high": float, "low": float, "close": float,
   "volume": int, "amount": float}

核心接口:
  fetch_minute_bars(code, trading_date, source='auto')
      -> (bars, prev_close, meta)
  fetch_multi_day(code, start_date, end_date, source='auto')
      -> (daily_bars, daily_prev_closes, daily_meta)

回测引擎只依赖本模块的统一出口，不感知具体数据源。
"""

from __future__ import annotations

import atexit
import re
from datetime import datetime
from typing import Optional

# 复用项目已有的 westock-data 封装
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from minute_bar_fetcher import fetch_realtime_minute_bars, fetch_realtime_quote


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
