#!/usr/bin/env python3
"""
L5 T+0 日内做T监控主入口
=========================
每分钟运行一次，对 positions.json 中的持仓逐只评估 T 信号。

流程:
  1. 读取 positions.json 持仓状态
  2. 对每只 t_eligible=true 的股票:
     a. 获取当日 1 分钟 K 线 + 实时报价
     b. 检查数据时效性（防陈旧价格）
     c. 评估减仓/加仓信号
     d. 风控校验
     e. 输出信号到 stdout + signals.csv
  3. 14:50 执行尾盘平衡检查

边界: research_only — 纯研究/监控，不执行任何交易。

独立性: 不依赖 L1/L2/L3/L4。L1/L2 软联动通过 t_risk_guard 实现，
       文件不存在时按默认值（允许T）处理。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from position_tracker import load_positions, get_position, get_sellable_shares
from minute_bar_fetcher import (
    fetch_realtime_quote, check_bar_freshness,
    is_one_word_board, is_limit_up_locked, is_limit_down_locked,
)
from data_provider import fetch_minute_bars
from t_signal_engine import evaluate_all_signals, SignalParams
from t_risk_guard import (
    check_risk, is_l1_systemic_risk, is_theme_retreated,
    eod_balance_check_all, RiskParams,
)
from t_trade_logger import log_signal, log_trade, log_monitor
from market_layer import compute_market_snapshot, MarketSnapshot
from stock_quote_features import fetch_quote_features
from config_loader import load_signal_params, load_risk_params


# ═══════════════════════════════════════════════════════════════
# 时间窗口判断
# ═══════════════════════════════════════════════════════════════
def is_trading_time() -> bool:
    """A 股交易时段 (9:25-11:35 或 12:55-15:05, 周一至五)。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    morning = (9 * 60 + 25) <= t <= (11 * 60 + 35)
    afternoon = (12 * 60 + 55) <= t <= (15 * 60 + 5)
    return morning or afternoon


def is_eod_check_time() -> bool:
    """是否在尾盘平衡检查时段（14:50-14:55）。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (14 * 60 + 50) <= t <= (14 * 60 + 55)


# ═══════════════════════════════════════════════════════════════
# 市场层快照落盘（方案 v0.2 第三节：market_gate.json 供个股层读取）
# ═══════════════════════════════════════════════════════════════
MARKET_GATE_FILE = Path(__file__).resolve().parent.parent / "data" / "market_gate.json"


def _save_market_gate_json(market: MarketSnapshot) -> None:
    """将市场层快照写入 data/market_gate.json，供个股层/外部读取。"""
    try:
        gate_data = {
            "market_risk_state": market.market_sentiment,
            "is_tradable": market.is_tradable_market,
            "up_limit_count": market.up_limit_count,
            "down_limit_count": market.down_limit_count,
            "up_ratio": market.up_ratio,
            "total_amount": market.total_amount,
            "top_industries": market.top_industries[:5] if market.top_industries else [],
            "top_concepts": market.top_concepts[:5] if market.top_concepts else [],
            "futures_basis": market.futures_basis,
            "timestamp": market.timestamp,
        }
        MARKET_GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MARKET_GATE_FILE, "w", encoding="utf-8") as f:
            json.dump(gate_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_monitor(f"market_gate.json 落盘失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 合成报价（非 westock 数据源兜底）
# ═══════════════════════════════════════════════════════════════
def _build_synthetic_quote(bars: list[dict], prev_close: float) -> dict:
    """
    用 bars + prev_close 构造合成报价 dict（baostock/mootdx 无实时 quote 时兜底）。

    与 westock_client.fetch_realtime_quote 的字段口径一致：
      涨停 = prev_close × 1.1，跌停 = prev_close × 0.9（四舍五入到分）。
    """
    last = bars[-1] if bars else {}
    return {
        "code": "",
        "price": float(last.get("close", 0)),
        "prev_close": float(prev_close),
        "open": float(bars[0]["open"]) if bars else 0,
        "high": max(float(b["high"]) for b in bars) if bars else 0,
        "low": min(float(b["low"]) for b in bars) if bars else 0,
        "volume": sum(int(b["volume"]) for b in bars) if bars else 0,
        "amount": sum(float(b["amount"]) for b in bars) if bars else 0,
        "limit_up": round(float(prev_close) * 1.1, 2) if prev_close else 0,
        "limit_down": round(float(prev_close) * 0.9, 2) if prev_close else 0,
    }


# ═══════════════════════════════════════════════════════════════
# 单只股票监控
# ═══════════════════════════════════════════════════════════════
def monitor_single_stock(
    code: str,
    pos: dict,
    market: MarketSnapshot = None,
    signal_params: SignalParams = None,
    risk_params: RiskParams = None,
    source: str = "auto",
    trading_date: str = None,
) -> dict:
    """
    对单只持仓股票进行 T 信号监控。

    参数:
      market: 市场层快照（可选），COLD 市场禁加仓。由 main() 每轮统一计算后传入。
      signal_params: 信号参数（来自 config_loader.load_signal_params()）
      risk_params: 风控参数（来自 config_loader.load_risk_params()）
      source: 数据源 'auto'|'mootdx'|'westock'|'baostock'（统一走 data_provider）
      trading_date: 'YYYY-MM-DD'，None=实时（今日），指定日期=历史回放

    返回监控结果 dict。
    """
    signal_params = signal_params or SignalParams()
    risk_params = risk_params or RiskParams()
    today = datetime.now().strftime("%Y-%m-%d")
    trading_date = trading_date or today
    is_realtime = trading_date == today
    result = {
        "code": code,
        "theme": pos.get("sector_tag", ""),  # 输出键名保留 theme 便于展示
        "t_eligible": pos.get("t_eligible", True),
        "base_shares": pos.get("base_shares", 0),
        "action": "none",
        "signal": None,
        "risk_check": None,
        "reason": "",
    }

    if not result["t_eligible"]:
        result["reason"] = "t_eligible=false"
        return result

    # 获取数据（统一走 data_provider，支持 mootdx/westock/baostock）
    bars, prev_close, meta = fetch_minute_bars(code, trading_date, source)
    if not bars or len(bars) < 30:
        result["reason"] = f"数据不足 ({len(bars)} bars, source={meta.get('source')})"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 数据时效性检查（仅实时模式；历史回放不检查）
    if is_realtime and not check_bar_freshness(bars):
        result["reason"] = "数据陈旧（>2分钟无更新）"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 报价：实时模式优先 westock 实时报价，失败/历史模式用 prev_close 构造合成报价
    quote = None
    if is_realtime and source in ("auto", "westock"):
        try:
            quote = fetch_realtime_quote(code)
        except Exception:
            quote = None
    if not quote:
        quote = _build_synthetic_quote(bars, prev_close)

    # 一字板过滤
    if is_one_word_board(quote):
        result["reason"] = "一字板，无法做T"
        log_monitor(f"{code}: skip — {result['reason']}")
        return result

    # 涨跌停封板检测
    l1_risk = is_l1_systemic_risk()
    theme_name = pos.get("sector_tag")
    retreated = is_theme_retreated(theme_name)

    is_lu_locked = is_limit_up_locked(quote, bars)
    is_ld_locked = is_limit_down_locked(quote, bars)

    # 盘口特征（westock quote 现成字段，用于订单流代理指标）
    quote_feats = fetch_quote_features(code)

    # 评估信号（接入市场层门控 + 盘口特征 + 配置参数）
    eval_result = evaluate_all_signals(
        bars=bars,
        current_price=quote.get("price"),
        prev_close=quote.get("prev_close"),
        is_limit_up_locked=is_lu_locked,
        is_limit_down_locked=is_ld_locked,
        theme_retreated=retreated,
        params=signal_params,
        market=market,
        quote_feats=quote_feats,
    )

    reduce_sig = eval_result["reduce_signal"]
    add_sig = eval_result["add_signal"]
    recommendation = eval_result["recommendation"]

    # 记录信号评估（无论是否触发）
    log_signal(
        code=code,
        direction=recommendation,
        recommendation=recommendation,
        reduce_score=reduce_sig.rules_score,
        add_score=add_sig.rules_score,
        price=quote.get("price", 0),
        snapshot=eval_result["snapshot"],
        reduce_rules=reduce_sig.rules_fired,
        add_rules=add_sig.rules_fired,
    )

    result["signal"] = {
        "recommendation": recommendation,
        "reduce_score": reduce_sig.rules_score,
        "add_score": add_sig.rules_score,
        "price": quote.get("price", 0),
        "rules": reduce_sig.rules_fired if recommendation == "reduce" else add_sig.rules_fired,
    }

    # 无信号
    if recommendation == "none" or recommendation == "conflict":
        result["reason"] = f"recommendation={recommendation}"
        return result

    # 风控校验
    direction = "sell" if recommendation == "reduce" else "buy"
    ref_price = eval_result["snapshot"].get("vwap") or quote.get("prev_close", 0)
    requested_shares = int(pos.get("base_shares", 0) * risk_params.max_t_size_ratio)
    requested_shares = (requested_shares // 100) * 100

    risk_result = check_risk(
        code=code,
        direction=direction,
        requested_shares=requested_shares,
        signal_price=quote.get("price", 0),
        reference_price=ref_price,
        params=risk_params,
    )
    result["risk_check"] = {
        "approved": risk_result.approved,
        "reason": risk_result.reason,
        "adjusted_shares": risk_result.adjusted_shares,
        "checks": risk_result.checks,
    }

    if not risk_result.approved:
        result["reason"] = f"风控拒绝: {risk_result.reason}"
        return result

    # 信号通过风控 → 输出提醒（research_only，不执行交易）
    t_type = ""
    if recommendation == "reduce":
        t_type = "正T-卖出" if not l1_risk else "正T-卖出（L1风险日仅减仓）"
    else:
        t_type = "反T-买入" if not retreated else "反T-买入（题材退潮仅卖允许）"

    result["action"] = "signal"
    result["reason"] = (
        f"{t_type} {risk_result.adjusted_shares} 股 @ {quote.get('price', 0):.2f} — "
        f"参考价 {ref_price:.2f}"
    )

    # 记录到交易日志（标记为 research_only）
    log_trade(
        code=code,
        t_type=t_type,
        direction=direction,
        shares=risk_result.adjusted_shares,
        price=quote.get("price", 0),
        reference_price=ref_price,
        rules_fired=reduce_sig.rules_fired if recommendation == "reduce" else add_sig.rules_fired,
        rules_score=reduce_sig.rules_score if recommendation == "reduce" else add_sig.rules_score,
        risk_approved=True,
        risk_checks=risk_result.checks,
        bar_time=bars[-1].get("time", ""),
        notes="research_only — 信号提醒，未实际执行",
    )

    return result


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════
def main(source: str = "auto", trading_date: str = None) -> int:
    """
    主入口。返回 0 (无信号) 或 1 (有信号输出)。

    参数:
      source: 数据源 'auto'|'mootdx'|'westock'|'baostock'
      trading_date: None=实时（今日），'YYYY-MM-DD'=历史回放
    """
    today = datetime.now().strftime("%Y-%m-%d")
    trading_date = trading_date or today
    is_realtime = trading_date == today

    # 时间窗口（仅实时模式检查；历史回放不受交易时段限制）
    if is_realtime and not is_trading_time():
        log_monitor("skip non_trading_time")
        return 0

    # 尾盘平衡检查（仅实时模式）
    if is_realtime and is_eod_check_time():
        eod_results = eod_balance_check_all()
        for eod in eod_results:
            if eod.get("status") in {"net_reduce", "net_add"}:
                log_monitor(
                    f"EOD {eod['code']}: {eod['status']} delta={eod['net_position_delta']} — {eod['action']}"
                )

    # 加载持仓
    positions = load_positions()
    if not positions:
        log_monitor("no positions")
        return 0

    # 加载配置参数（P0-2：实盘入口接入 config_loader，不再依赖 dataclass 默认值）
    # thresholds.yaml 缺失时 config_loader 会回退到 DEFAULT_PARAMS/DEFAULT_RISK_PARAMS
    signal_params = load_signal_params()
    risk_params = load_risk_params()

    # 市场层快照（跨股票共享，每轮计算一次，落盘 market_gate.json）
    # westock 为可选外部数据源：未配置 WESTOCK_DIR 时降级为独立模式
    # （市场情绪 NEUTRAL，无涨跌停/板块热度数据），与 L1/L2 软依赖处理一致
    use_westock = bool(os.environ.get("WESTOCK_DIR"))
    if not use_westock:
        print("[WARN] WESTOCK_DIR 未设置，市场层降级为独立模式"
              "（NEUTRAL 情绪，无涨跌停/板块数据）", file=sys.stderr)
    market = compute_market_snapshot(use_westock=use_westock)
    _save_market_gate_json(market)

    mode_label = f"历史回放 {trading_date}" if not is_realtime else "实时"
    log_monitor(f"run source={source} mode={mode_label} positions={len(positions)}")

    # 逐只监控
    results = []
    signal_count = 0
    for code, pos in positions.items():
        r = monitor_single_stock(
            code, pos, market=market,
            signal_params=signal_params, risk_params=risk_params,
            source=source, trading_date=trading_date,
        )
        results.append(r)
        if r["action"] == "signal":
            signal_count += 1

    # 输出
    if signal_count == 0:
        log_monitor(f"no_signal positions={len(positions)}")
        return 0

    # 格式化输出
    ts = datetime.now().strftime("%m-%d %H:%M")
    lines = [f"L5 日内做T信号提醒｜{ts} — research_only"]
    lines.append("说明：这是研究/监控信号，不是自动交易或立即执行指令。")
    lines.append("")

    for r in results:
        if r["action"] != "signal":
            continue
        sig = r["signal"]
        lines.append(
            f"- {r['code']} ({r['theme']})\n"
            f"  动作: {r['reason']}\n"
            f"  信号: {sig['recommendation']} (reduce={sig['reduce_score']}/4, add={sig['add_score']}/4)\n"
            f"  规则: {' | '.join(sig['rules'])}"
        )

    output = "\n".join(lines)
    print(output)
    log_monitor(f"signal positions={len(positions)} signals={signal_count}")
    return 1


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="L5 T+0 日内做T监控（多数据源：eastmoney/mootdx/westock/baostock）"
    )
    parser.add_argument("--source", default="auto",
                        choices=["auto", "mootdx", "westock", "baostock", "eastmoney"],
                        help="数据源：auto=自动回退(默认) | eastmoney(免依赖,实时) | mootdx | westock | baostock(历史)")
    parser.add_argument("--date", default=None,
                        help="交易日 YYYY-MM-DD（不传=实时今日，传=历史回放）")
    parser.add_argument("--demo", action="store_true",
                        help="测试模式：忽略交易时段限制（实时模式用）")
    parser.add_argument("--eod-check", action="store_true",
                        help="仅执行尾盘平衡检查")
    args = parser.parse_args()

    if args.demo:
        # 测试模式：忽略时间窗口
        globals()["is_trading_time"] = lambda: True
        print("[DEMO] 测试模式 — 忽略时间窗口限制", file=sys.stderr)

    if args.eod_check:
        # 仅执行尾盘平衡检查
        results = eod_balance_check_all()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        sys.exit(0)

    sys.exit(main(source=args.source, trading_date=args.date))
