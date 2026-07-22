#!/usr/bin/env python3
"""
个股盘口特征层（Stock Quote Features）
======================================
从 westock quote 拉取现成的盘口/资金字段（无需自己算），与 intraday_reference
的特征计算层快照合并，供决策层（t_signal_engine）使用。

westock quote 已返回的字段（实测 sh600000）:
  - avg_price         = VWAP（现成，可交叉校验自算累计 VWAP）
  - volume_ratio      = 量比（现成）
  - turnover_rate     = 换手率（现成）
  - range_pct         = 振幅（现成）
  - wb_ratio          = 委比（订单失衡代理）
  - inner_volume      = 内盘（主动卖，Lee-Ready 近似）
  - outer_volume      = 外盘（主动买，Lee-Ready 近似）
  - price_ceiling     = 涨停价（现成，覆盖 prev_close×1.1 估算）
  - price_floor       = 跌停价（现成）
  - price / prev_close / open / high / low / volume / amount

本模块不重复计算 intraday_reference 已有的指标，只补充 westock 现成字段 +
派生的订单流代理指标。

独立性：仅依赖 westock-data CLI（实盘）或调用方注入的 quote dict（回测）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# westock CLI（与 minute_bar_fetcher / market_layer 一致）
# ═══════════════════════════════════════════════════════════════
WESTOCK_NODE = os.environ.get(
    "WESTOCK_NODE",
    str(Path.home() / ".workbuddy/binaries/node/versions/22.22.2/node.exe"),
)
WESTOCK_DIR = os.environ.get(
    "WESTOCK_DIR",
    "D:/Users/kangrunze/AppData/Local/Programs/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/westock-data",
)
WESTOCK_SCRIPT = os.path.join(WESTOCK_DIR, "scripts", "index.js")


def _to_westock_symbol(code: str) -> str:
    """6 位代码 → sh/sz/bj 前缀。"""
    if code.startswith(("sh", "sz", "bj", "pt")):
        return code
    if len(code) == 6 and code[0] == "6":
        return f"sh{code}"
    if len(code) == 6 and code[0] in {"0", "2", "3"}:
        return f"sz{code}"
    if len(code) == 6 and code[0] in {"4", "8"}:
        return f"bj{code}"
    return code


def _run_westock_quote(symbol: str) -> Optional[dict]:
    """调用 westock quote，返回原始 item dict。"""
    import json
    env = os.environ.copy()
    env["NODE_PATH"] = os.path.join(WESTOCK_DIR, "node_modules")
    env["PYTHONIOENCODING"] = "utf-8"
    full_cmd = [WESTOCK_NODE, WESTOCK_SCRIPT, "quote", symbol, "--raw"]
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
            return None
        data = json.loads(stdout)
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None
    except Exception as e:
        print(f"[WARN] stock_quote_features westock quote failed: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════
# 盘口特征提取
# ═══════════════════════════════════════════════════════════════
def fetch_quote_features(code: str) -> dict:
    """
    从 westock quote 拉取盘口/资金字段，返回标准化 dict。

    所有数值字段失败时为 None，不抛异常。
    """
    symbol = _to_westock_symbol(code)
    item = _run_westock_quote(symbol)
    if not item:
        return {}

    def _f(key: str) -> Optional[float]:
        v = item.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _i(key: str) -> Optional[int]:
        v = item.get(key)
        try:
            return int(float(v)) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "code": code,
        "name": item.get("name"),
        # ── 价格（westock 现成，比 prev_close×1.1 估算更准）──
        "current_price": _f("price"),
        "prev_close": _f("prev_close"),
        "open": _f("open"),
        "high": _f("high"),
        "low": _f("low"),
        "limit_up": _f("price_ceiling"),    # 现成涨停价
        "limit_down": _f("price_floor"),    # 现成跌停价
        # ── 量能/资金（westock 现成）──
        "quote_vwap": _f("avg_price"),       # westock 算好的 VWAP
        "quote_volume_ratio": _f("volume_ratio"),
        "turnover_rate": _f("turnover_rate"),
        "amplitude_pct": _f("range_pct"),
        "volume": _i("volume"),
        "amount": _f("amount"),
        # ── 订单流代理（westock 现成）──
        "wb_ratio": _f("wb_ratio"),          # 委比 = 订单失衡代理
        "inner_volume": _i("inner_volume"),  # 内盘 = 主动卖
        "outer_volume": _i("outer_volume"),  # 外盘 = 主动买
    }


def compute_order_flow_proxy(quote_feats: dict) -> dict:
    """
    基于盘口字段派生订单流代理指标。

    返回:
      - active_buy_ratio: 主动买占比 = outer / (inner + outer)，0-1
      - active_sell_ratio: 主动卖占比 = inner / (inner + outer)，0-1
      - order_imbalance_pct: 委比（直接用 wb_ratio，正数偏买、负数偏卖）
      - vwap_dev_from_quote: (price - quote_vwap) / quote_vwap，与自算 vwap_dev 交叉校验
    """
    result = {
        "active_buy_ratio": None,
        "active_sell_ratio": None,
        "order_imbalance_pct": None,
        "vwap_dev_from_quote": None,
    }
    inner = quote_feats.get("inner_volume")
    outer = quote_feats.get("outer_volume")
    if inner is not None and outer is not None and (inner + outer) > 0:
        total = inner + outer
        result["active_buy_ratio"] = outer / total
        result["active_sell_ratio"] = inner / total
    result["order_imbalance_pct"] = quote_feats.get("wb_ratio")
    price = quote_feats.get("current_price")
    qvwap = quote_feats.get("quote_vwap")
    if price is not None and qvwap is not None and qvwap > 0:
        result["vwap_dev_from_quote"] = (price - qvwap) / qvwap
    return result


# ═══════════════════════════════════════════════════════════════
# 与特征计算层快照合并
# ═══════════════════════════════════════════════════════════════
def merge_with_reference_snapshot(
    ref_snap: dict,
    quote_feats: Optional[dict] = None,
) -> dict:
    """
    把盘口特征合并进 intraday_reference 的快照。

    参数:
      ref_snap: compute_reference_snapshot() 返回的 dict
      quote_feats: fetch_quote_features() 返回的 dict。None 时跳过（回测无 quote）

    返回: 合并后的 dict（原 ref_snap 的拷贝 + quote 字段 + 派生指标）。
    保留原 ref_snap 的所有键，quote 字段以 quote_ 前缀或新键名加入，不覆盖。
    """
    merged = dict(ref_snap)
    if not quote_feats:
        merged["_quote_available"] = False
        return merged

    merged["_quote_available"] = True

    # 合并 westock 现成字段（用独立键名，不覆盖 ref_snap 的自算值）
    for k in [
        "quote_vwap", "quote_volume_ratio", "turnover_rate",
        "amplitude_pct", "wb_ratio", "inner_volume", "outer_volume",
    ]:
        if k in quote_feats:
            merged[k] = quote_feats[k]

    # 涨跌停价：优先用 westock 的 price_ceiling/floor（比 prev_close×1.1 准）
    if quote_feats.get("limit_up") is not None:
        merged["limit_up"] = quote_feats["limit_up"]
    if quote_feats.get("limit_down") is not None:
        merged["limit_down"] = quote_feats["limit_down"]

    # 如果 ref_snap 没有 current_price（理论上不会），用 quote 的
    if merged.get("current_price") is None and quote_feats.get("current_price") is not None:
        merged["current_price"] = quote_feats["current_price"]

    # 派生订单流代理
    merged.update(compute_order_flow_proxy(quote_feats))

    return merged


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== 盘口特征层自检（实盘 sh600000）===")
    feats = fetch_quote_features("sh600000")
    if not feats:
        print("  [WARN] westock quote 无返回（非交易时段或数据源不可用）")
    else:
        for k, v in feats.items():
            print(f"  {k}: {v}")

        print("\n=== 订单流代理派生 ===")
        proxy = compute_order_flow_proxy(feats)
        for k, v in proxy.items():
            print(f"  {k}: {v}")

    print("\n=== 合并快照测试（注入模拟 ref_snap）===")
    fake_ref = {
        "current_price": 9.01,
        "vwap": 8.89,
        "vwap_dev": 0.0135,
        "rsi": 55.0,
        "kdj_k": 60.0,
    }
    merged = merge_with_reference_snapshot(fake_ref, feats if feats else None)
    print(f"  _quote_available: {merged.get('_quote_available')}")
    print(f"  ref_snap 键保留: vwap={merged.get('vwap')}, rsi={merged.get('rsi')}")
    if feats:
        print(f"  quote 字段合并: quote_vwap={merged.get('quote_vwap')}, wb_ratio={merged.get('wb_ratio')}")
        print(f"  派生: active_buy_ratio={merged.get('active_buy_ratio')}, "
              f"order_imbalance_pct={merged.get('order_imbalance_pct')}")
        print(f"  涨跌停价: limit_up={merged.get('limit_up')}, limit_down={merged.get('limit_down')}")

    # 回测模式（无 quote）
    print("\n=== 回测模式（无 quote）===")
    merged_no_q = merge_with_reference_snapshot(fake_ref, None)
    print(f"  _quote_available: {merged_no_q.get('_quote_available')}")
    print(f"  ref_snap 键保留: vwap={merged_no_q.get('vwap')}")
