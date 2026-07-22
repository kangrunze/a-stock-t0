#!/usr/bin/env python3
"""
市场层（Market Layer）
======================
跨股票共享的日内市场状态，刷新频率低于个股层（建议 1-3 分钟一次）。
作为个股层（Layer A/B/C）的门控与权重调整依据。

数据源：
  - 涨跌停数量 / 上涨占比：westock changedist（实时）
  - 板块热度 / 行业排名：westock sector ranking（实时）
  - 期指升贴水：westock 暂不支持国内 IF/IH/IC/IM，接口保留返回 None（二期接券商源）
  - 题材状态（可选 fallback）：软依赖 ashare-sop-engine 的 themes_v17.json

独立性：不依赖 L1/L2/L3/L4。themes_v17.json 不存在时按默认值处理。
仅依赖 westock-data CLI（实盘）或调用方传入的缓存数据（回测）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ═══════════════════════════════════════════════════════════════
# 路径配置
# ═══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ashare-sop-engine 题材快照（软依赖，不存在则忽略）
# 同机运行时可指向 d:/project/ashare-sop-engine/hermes/data/themes_v17.json
THEMES_V17_FILE = Path(os.environ.get(
    "A_T0_THEMES_V17",
    str(PROJECT_ROOT / "data" / "themes_v17.json"),
))

# westock CLI（与 minute_bar_fetcher 一致）
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
# westock 调用（通用版，返回原始 JSON）
# ═══════════════════════════════════════════════════════════════
def _run_westock_raw(cmd: str, timeout: int = 45) -> Optional[object]:
    """
    调用 westock-data CLI，返回原始解析后的 JSON（dict / list / None）。

    与 minute_bar_fetcher._run_westock 不同：后者针对 kline/quote 的 list 结构
    做了扁平化；本函数保留原始结构，以适配 changedist（单 dict）和
    sector ranking（嵌套 dict）的返回。
    """
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
            timeout=timeout,
            env=env,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            return None
        return json.loads(stdout)
    except Exception as e:
        print(f"[WARN] market_layer westock call failed: {e}", file=sys.stderr)
        return None


# ═══════════════════════════════════════════════════════════════
# 市场快照数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass
class MarketSnapshot:
    """市场层快照。所有字段在数据不可用时为 None / 空列表，不阻塞个股层。"""
    # ── 涨跌停 / 情绪（来自 changedist）──
    up_limit_count: Optional[int] = None       # 涨停家数
    down_limit_count: Optional[int] = None     # 跌停家数
    up_count: Optional[int] = None             # 上涨家数
    down_count: Optional[int] = None           # 下跌家数
    up_ratio: Optional[float] = None           # 上涨占比（0-100）
    up_ratio_comment: Optional[str] = None     # 情绪文案
    total_amount: Optional[float] = None       # 两市成交额（元）

    # ── 板块热度（来自 sector ranking）──
    top_industries: list[dict] = field(default_factory=list)   # 行业涨幅榜
    top_concepts: list[dict] = field(default_factory=list)     # 概念涨幅榜
    top_inflow_sectors: list[dict] = field(default_factory=list)  # 主力资金流入榜

    # ── 期指升贴水（二期，暂未接入）──
    futures_basis: Optional[dict] = None
    # 结构: {"if": {"spread": -0.5, "spread_pct": -0.08}, "ih": ..., "ic": ..., "im": ...}

    # ── 题材状态（软依赖 themes_v17.json，可选）──
    themes_snapshot: Optional[dict] = None

    # ── 元信息 ──
    timestamp: Optional[str] = None            # 快照时间 ISO
    source: str = "westock"                    # 数据源标记

    @property
    def market_sentiment(self) -> str:
        """
        市场情绪分级（供个股层门控）。

        判定逻辑（优先级从高到低）:
          1. 涨停 ≥ 80 且 跌停 ≤ 10 → HOT（赚钱效应强，可正常做T）
          2. 涨停 ≤ 20 或 跌停 ≥ 50 → COLD（赚钱效应弱，减仓信号宽松/加仓信号严格）
          3. 上涨占比 ≤ 30% → COOL（偏冷，谨慎）
          4. 其余 → NEUTRAL
        数据缺失时返回 NEUTRAL（不阻塞）。
        """
        if self.up_limit_count is not None and self.down_limit_count is not None:
            if self.up_limit_count >= 80 and self.down_limit_count <= 10:
                return "HOT"
            if self.up_limit_count <= 20 or self.down_limit_count >= 50:
                return "COLD"
        if self.up_ratio is not None and self.up_ratio <= 30:
            return "COOL"
        return "NEUTRAL"

    @property
    def is_tradable_market(self) -> bool:
        """市场是否适合做T（非 COLD 即可）。COLD 时建议只减不加。"""
        return self.market_sentiment != "COLD"


# ═══════════════════════════════════════════════════════════════
# 数据采集函数
# ═══════════════════════════════════════════════════════════════
def fetch_limit_board_snapshot() -> dict:
    """
    从 westock changedist 拉取涨跌停家数 + 上涨占比。

    返回 dict（字段与 MarketSnapshot 涨跌停部分对应）。失败返回空 dict。
    """
    data = _run_westock_raw("changedist")
    if not isinstance(data, dict):
        return {}
    return {
        "up_limit_count": data.get("upLimitCount"),
        "down_limit_count": data.get("downLimitCount"),
        "up_count": data.get("upCount"),
        "down_count": data.get("downCount"),
        "up_ratio": data.get("upRatio"),
        "up_ratio_comment": data.get("upRatioComment"),
        "total_amount": data.get("totalAmount"),
    }


def fetch_sector_ranking_snapshot() -> dict:
    """
    从 westock sector ranking 拉取板块热度。

    返回 dict，包含:
      - top_industries: 行业涨幅榜（list[dict]）
      - top_concepts: 概念涨幅榜（list[dict]）
      - top_inflow_sectors: 主力资金流入榜（list[dict]）
    失败返回空 dict。
    """
    data = _run_westock_raw("sector ranking")
    if not isinstance(data, dict):
        return {}
    sections = data.get("sections", [])
    result = {
        "top_industries": [],
        "top_concepts": [],
        "top_inflow_sectors": [],
    }
    # sections 结构: [行业榜, 概念榜, 资金流入榜]（按 westock 当前返回顺序）
    if len(sections) >= 1 and isinstance(sections[0], list):
        result["top_industries"] = sections[0]
    if len(sections) >= 2 and isinstance(sections[1], list):
        result["top_concepts"] = sections[1]
    if len(sections) >= 3 and isinstance(sections[2], list):
        result["top_inflow_sectors"] = sections[2]
    return result


def fetch_futures_basis() -> Optional[dict]:
    """
    期指升贴水（IF/IH/IC/IM 基差）。

    ⚠️ 一期未接入：westock futures 仅支持外盘商品/金融期货 + 港股股指，
    不支持国内 IF/IH/IC/IM。需二期接券商期货行情源（CTP/Choice/Wind）。

    返回 None 表示数据源未接入，不阻塞个股层。
    """
    return None


def read_themes_v17() -> Optional[dict]:
    """
    软依赖读取 ashare-sop-engine 的 L2 题材数据。

    P1-2: 改为委托 l2_theme_reader.get_themes_snapshot()，
    统一文件发现逻辑（不再各自猜测文件名）。

    文件不存在时返回 None（a-t0 独立运行，不报错）。
    """
    from l2_theme_reader import get_themes_snapshot
    return get_themes_snapshot()


# ═══════════════════════════════════════════════════════════════
# 市场层主入口
# ═══════════════════════════════════════════════════════════════
def compute_market_snapshot(
    use_westock: bool = True,
    cached_limit_board: Optional[dict] = None,
    cached_sector_ranking: Optional[dict] = None,
) -> MarketSnapshot:
    """
    汇总市场层快照。

    参数:
      use_westock: 是否实时调用 westock（实盘 True / 回测 False）
      cached_limit_board: 预先拉取的 changedist 数据（回测注入，避免重复调用）
      cached_sector_ranking: 预先拉取的 sector ranking 数据

    返回: MarketSnapshot。任何数据源失败时对应字段为 None/空，不抛异常。
    """
    snap = MarketSnapshot(timestamp=datetime.now().isoformat(timespec="seconds"))

    # 涨跌停 / 情绪
    limit_data = cached_limit_board if cached_limit_board is not None else (
        fetch_limit_board_snapshot() if use_westock else {}
    )
    for k, v in limit_data.items():
        if hasattr(snap, k):
            setattr(snap, k, v)

    # 板块热度
    sector_data = cached_sector_ranking if cached_sector_ranking is not None else (
        fetch_sector_ranking_snapshot() if use_westock else {}
    )
    for k, v in sector_data.items():
        if hasattr(snap, k):
            setattr(snap, k, v)

    # 期指升贴水（二期）
    snap.futures_basis = fetch_futures_basis() if use_westock else None

    # 题材状态（软依赖）
    snap.themes_snapshot = read_themes_v17()

    return snap


# ═══════════════════════════════════════════════════════════════
# 个股层门控接口
# ═══════════════════════════════════════════════════════════════
def market_gate_for_add(market: MarketSnapshot) -> tuple[bool, str]:
    """
    加仓门控：市场层是否允许加仓信号触发。

    返回 (allowed, reason)。
    COLD 市场禁止加仓（只减不加）；其余允许。
    """
    sentiment = market.market_sentiment
    if sentiment == "COLD":
        return False, f"市场情绪 COLD（涨停{market.up_limit_count}/跌停{market.down_limit_count}），禁止加仓"
    return True, f"市场情绪 {sentiment}，加仓放行"


def market_gate_for_reduce(market: MarketSnapshot) -> tuple[bool, str]:
    """
    减仓门控：市场层是否允许减仓信号触发。

    减仓在任何市场情绪下都允许（COLD 时反而更应减仓）。
    """
    return True, f"市场情绪 {market.market_sentiment}，减仓放行"


def adjust_signal_weight(market: MarketSnapshot, direction: str) -> float:
    """
    根据市场情绪调整信号权重（供决策层加权使用）。

    参数:
      direction: "reduce" 或 "add"

    返回: 权重乘数（0.5 ~ 1.2）
      - HOT 市场：加仓权重 ↑（回调买入更安全），减仓权重 ↓（趋势可能延续）
      - COLD 市场：加仓权重 ↓（避免接飞刀），减仓权重 ↑（及时止盈更重要）
      - NEUTRAL/COOL：1.0
    """
    sentiment = market.market_sentiment
    if direction == "add":
        return {"HOT": 1.2, "NEUTRAL": 1.0, "COOL": 0.8, "COLD": 0.5}.get(sentiment, 1.0)
    if direction == "reduce":
        return {"HOT": 0.8, "NEUTRAL": 1.0, "COOL": 1.1, "COLD": 1.2}.get(sentiment, 1.0)
    return 1.0


# ═══════════════════════════════════════════════════════════════
# 自检
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== 市场层自检（实盘拉取）===")
    snap = compute_market_snapshot(use_westock=True)
    print(f"  时间: {snap.timestamp}")
    print(f"  情绪: {snap.market_sentiment} (tradable={snap.is_tradable_market})")
    print(f"  涨停/跌停: {snap.up_limit_count}/{snap.down_limit_count}")
    print(f"  上涨/下跌: {snap.up_count}/{snap.down_count} ({snap.up_ratio}%)")
    print(f"  情绪文案: {snap.up_ratio_comment}")
    print(f"  两市成交额: {snap.total_amount}")
    print(f"  行业榜前3: {[s.get('name') for s in snap.top_industries[:3]]}")
    print(f"  概念榜前3: {[s.get('name') for s in snap.top_concepts[:3]]}")
    print(f"  资金流入榜前3: {[s.get('name') for s in snap.top_inflow_sectors[:3]]}")
    print(f"  期指升贴水: {snap.futures_basis}")
    print(f"  themes_v17: {'已加载' if snap.themes_snapshot else '未加载（独立模式）'}")

    print("\n=== 门控测试 ===")
    for d in ["add", "reduce"]:
        allowed, reason = (market_gate_for_add if d == "add" else market_gate_for_reduce)(snap)
        weight = adjust_signal_weight(snap, d)
        print(f"  {d}: allowed={allowed}, weight={weight}, reason={reason}")

    print("\n=== 回测模式（注入缓存数据，不调 westock）===")
    cached_snap = compute_market_snapshot(
        use_westock=False,
        cached_limit_board={
            "up_limit_count": 120, "down_limit_count": 5,
            "up_count": 3000, "down_count": 2000, "up_ratio": 60,
            "up_ratio_comment": "市场情绪高涨", "total_amount": 1.5e12,
        },
        cached_sector_ranking={
            "top_industries": [{"name": "半导体", "changePct": "3.5"}],
            "top_concepts": [{"name": "AI芯片", "changePct": "5.2"}],
            "top_inflow_sectors": [{"name": "半导体", "mainNetInflow": "100000"}],
        },
    )
    print(f"  情绪: {cached_snap.market_sentiment} (应为 HOT)")
    print(f"  涨停: {cached_snap.up_limit_count} (应为 120)")
    print(f"  加仓门控: {market_gate_for_add(cached_snap)}")
    print(f"  加仓权重: {adjust_signal_weight(cached_snap, 'add')} (应为 1.2)")

    # COLD 场景
    cold_snap = compute_market_snapshot(
        use_westock=False,
        cached_limit_board={"up_limit_count": 10, "down_limit_count": 80, "up_ratio": 20},
    )
    print(f"\n  COLD 情绪: {cold_snap.market_sentiment} (应为 COLD)")
    print(f"  COLD 加仓门控: {market_gate_for_add(cold_snap)}")
    print(f"  COLD 加仓权重: {adjust_signal_weight(cold_snap, 'add')} (应为 0.5)")
