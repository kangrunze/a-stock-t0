#!/usr/bin/env python3
"""
计算 expired 腿的真实平仓盈亏（模拟强制平仓）
================================================
当前 backtest 中 expired 腿不产生真实平仓成交，paired_pnl=0。
本脚本用 expired 腿 expire_bar_idx 时刻的收盘价模拟强制平仓，
计算真实平仓盈亏（含滑点和成本），重新汇总 batch 数字。

输出: outputs/backtest/diagnose_expired_real_pnl.json + 控制台打印
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import BacktestParams
from at0.strategy import SignalParams
from at0.risk import RiskParams, CostModel

# 复用 diagnose_pairing_failure.py 的 instrumented backtest + leg 收集
from diagnose_pairing_failure import (
    instrumented_backtest_multi_day,
    collect_legs_from_daily_results,
    adapt_params,
    POOL_PATH, START, END,
)


def _find_bar_price(bar_log: list[dict], date: str, bar_idx: int) -> float | None:
    """从 bar_log 中找到指定 date+bar_idx 的 price。"""
    for b in bar_log:
        if b.get("date") == date and b.get("bar_idx") == bar_idx:
            return b.get("price")
    return None


def main():
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = json.load(f)
    codes = [c["code"] for c in pool["candidates"]]
    print(f"[诊断] 候选池 {len(codes)} 只股票，{START}~{END}")
    print(f"[诊断] 目标：计算 expired 腿真实平仓盈亏（模拟强制平仓）\n")

    cost_model = CostModel.base()  # 与回测一致

    all_expired_legs = []
    batch_realized_pnl = 0.0  # 原 baseline 的 net_pnl（paired 已实现 - 成本，expired 记为 0）
    batch_expired_real_pnl = 0.0
    batch_expired_cost = 0.0

    for i, code in enumerate(codes):
        daily_bars, daily_prev, daily_meta = fetch_multi_day(code, START, END, "baostock")
        if not daily_bars:
            print(f"[data {i+1}/{len(codes)}] {code}: 无数据，跳过")
            continue

        pure_code = normalize_code(code)["pure"]
        first_meta = next(iter(daily_meta.values()))
        freq = first_meta.get("frequency", "5min")
        bpd = first_meta.get("bars_count", 48)
        first_date = min(daily_prev.keys())
        avg_cost = daily_prev[first_date]

        params = BacktestParams(
            base_shares=3000, avg_cost=avg_cost,
            signal_params=SignalParams(), risk_params=RiskParams(),
        )
        params = adapt_params(params, freq, bpd)

        result, bar_log = instrumented_backtest_multi_day(
            code=pure_code, daily_bars=daily_bars, daily_prev_closes=daily_prev, params=params,
        )

        legs = collect_legs_from_daily_results(result["daily_results"])

        # 合并 bar_log 到 date->bar_idx->price 索引
        bar_price_map: dict[tuple[str, int], float] = {}
        for b in bar_log:
            key = (b.get("date"), b.get("bar_idx"))
            bar_price_map[key] = b.get("price")

        stock_realized_pnl = result["net_pnl"]  # 原 baseline 的 net_pnl（expired 记为 0）
        stock_expired_real_pnl = 0.0
        stock_expired_cost = 0.0

        for leg in legs:
            if leg["close_type"] == "expired":
                # 找到 expire 时刻的 price
                close_date = leg.get("close_date", "")
                close_bar_idx = leg.get("close_bar_idx", -1)
                expire_price = bar_price_map.get((close_date, close_bar_idx))

                fill_price = leg["fill_price"]
                shares = leg["shares"]
                direction = leg["direction"]

                if expire_price is None:
                    # 找不到价格，用 NaN 标记
                    real_pnl = float("nan")
                    close_cost = 0.0
                    note = "price_not_found"
                else:
                    # 模拟强制平仓：用 expire 时刻价格 + 滑点
                    # 平仓方向：buy leg 用 sell 平仓，sell leg 用 buy 平仓
                    close_dir = "sell" if direction == "buy" else "buy"
                    close_fill_price = cost_model.fill_price(close_dir, expire_price)
                    close_cost = cost_model.calc_cost(close_dir, shares, close_fill_price)

                    # 盈亏计算（与 FIFO 配对一致：(卖价-买价)*shares）
                    if direction == "buy":
                        # buy leg 平仓：卖价=close_fill_price，买价=fill_price
                        gross_pnl = (close_fill_price - fill_price) * shares
                    else:
                        # sell leg 平仓：卖价=fill_price，买价=close_fill_price
                        gross_pnl = (fill_price - close_fill_price) * shares
                    real_pnl = gross_pnl - close_cost
                    note = ""

                all_expired_legs.append({
                    "code": pure_code,
                    "direction": direction,
                    "shares": shares,
                    "fill_price": round(fill_price, 4),
                    "expire_price": round(expire_price, 4) if expire_price else None,
                    "expire_pct_from_fill": (
                        round((expire_price - fill_price) / fill_price * 100, 3)
                        if expire_price and fill_price > 0 else None
                    ),
                    "real_pnl": round(real_pnl, 2) if real_pnl == real_pnl else None,  # NaN 检查
                    "close_cost": round(close_cost, 2),
                    "open_date": leg["open_date"],
                    "close_date": close_date,
                    "holding_bars": leg.get("holding_bars"),
                    "note": note,
                })
                if real_pnl == real_pnl:  # 非 NaN
                    stock_expired_real_pnl += real_pnl
                    stock_expired_cost += close_cost

        batch_realized_pnl += stock_realized_pnl
        batch_expired_real_pnl += stock_expired_real_pnl
        batch_expired_cost += stock_expired_cost

        print(f"[{i+1}/{len(codes)}] {pure_code}: "
              f"realized={stock_realized_pnl:+.2f} "
              f"expired_real={stock_expired_real_pnl:+.2f} "
              f"expired_count={sum(1 for l in legs if l['close_type']=='expired')}")

    # 统计
    print("\n" + "=" * 80)
    print("Expired 腿真实平仓盈亏汇总（模拟强制平仓）")
    print("=" * 80)

    valid_expired = [l for l in all_expired_legs if l["real_pnl"] is not None]
    expired_wins = [l for l in valid_expired if l["real_pnl"] > 0]
    expired_losses = [l for l in valid_expired if l["real_pnl"] < 0]
    expired_total = sum(l["real_pnl"] for l in valid_expired)
    expired_total_cost = sum(l["close_cost"] for l in valid_expired)

    print(f"\nexpired 腿总数: {len(all_expired_legs)} (有效 {len(valid_expired)})")
    print(f"  盈利腿数: {len(expired_wins)} ({len(expired_wins)/max(1,len(valid_expired))*100:.1f}%)")
    print(f"  亏损腿数: {len(expired_losses)} ({len(expired_losses)/max(1,len(valid_expired))*100:.1f}%)")
    print(f"  总真实盈亏: {expired_total:+.2f}")
    print(f"  总平仓成本: {expired_total_cost:.2f}")
    print(f"  均盈: {statistics.mean([l['real_pnl'] for l in expired_wins]):+.2f}" if expired_wins else "  均盈: N/A")
    print(f"  均亏: {statistics.mean([l['real_pnl'] for l in expired_losses]):+.2f}" if expired_losses else "  均亏: N/A")

    print(f"\nexpire 时刻价格相对开仓价的偏移分布（%）:")
    pcts = [l["expire_pct_from_fill"] for l in all_expired_legs if l["expire_pct_from_fill"] is not None]
    if pcts:
        sp = sorted(pcts)
        print(f"  均值: {statistics.mean(pcts):+.3f}%")
        print(f"  中位数: {statistics.median(pcts):+.3f}%")
        print(f"  P10: {sp[int(len(sp)*0.10)]:+.3f}%")
        print(f"  P90: {sp[int(len(sp)*0.90)]:+.3f}%")
        print(f"  范围: [{sp[0]:+.3f}%, {sp[-1]:+.3f}%]")

    print(f"\n{'='*80}")
    print(f"批次汇总对比（20只股票）")
    print(f"{'='*80}")
    print(f"  原 baseline net_pnl（paired已实现-expired记0）: {batch_realized_pnl:+.2f}")
    print(f"  expired 腿真实平仓盈亏（模拟）:                {batch_expired_real_pnl:+.2f}")
    print(f"  expired 腿平仓成本:                             {batch_expired_cost:.2f}")
    print(f"  ---")
    print(f"  新总净盈亏（realized + expired真实）:           {batch_realized_pnl + batch_expired_real_pnl:+.2f}")

    # 对比原 baseline
    batch_summary_path = Path(__file__).resolve().parent.parent / "outputs" / "backtest" / "batch_summary.json"
    if batch_summary_path.exists():
        with open(batch_summary_path, "r", encoding="utf-8") as f:
            old = json.load(f)
        old_net = old["overall"]["net_pnl"]
        old_unreal = old["overall"]["unrealized_pnl"]
        print(f"\n对比当前 baseline（expired 腿记为 0）:")
        print(f"  原 baseline net_pnl:              {old_net:+.2f}")
        print(f"  原 baseline unrealized_pnl:       {old_unreal:+.2f}")
        print(f"  原 baseline net_with_unrealized:   {old_net + old_unreal:+.2f}")
        new_net = batch_realized_pnl + batch_expired_real_pnl
        print(f"  新 net（expired 真实平仓）:        {new_net:+.2f}")
        delta = new_net - (old_net + old_unreal)
        print(f"  差异（新 - 原含浮盈）:             {delta:+.2f}")

    # 保存
    output = {
        "summary": {
            "expired_legs_count": len(all_expired_legs),
            "valid_expired_count": len(valid_expired),
            "expired_wins": len(expired_wins),
            "expired_losses": len(expired_losses),
            "expired_total_real_pnl": round(expired_total, 2),
            "expired_total_close_cost": round(expired_total_cost, 2),
            "batch_realized_pnl": round(batch_realized_pnl, 2),
            "new_batch_net_pnl": round(batch_realized_pnl + batch_expired_real_pnl, 2),
        },
        "all_expired_legs": all_expired_legs,
    }
    output_path = Path(__file__).resolve().parent.parent / "outputs" / "backtest" / "diagnose_expired_real_pnl.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[诊断] 完整结果 -> {output_path}")


if __name__ == "__main__":
    main()
