#!/usr/bin/env python3
"""
单笔盈亏分布对比分析
====================
对 600184（盈利股）和 603061（outlier 亏损股）跑趋势跟随 T+0 回测，
提取所有单笔 trade，统计盈亏分布，验证是否存在"赢小亏大"结构性缺陷。

用法:
  python scripts/analyze_pnl_distribution.py

数据已缓存（fetch_multi_day use_cache=True），二次运行直接读缓存。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from at0.data import fetch_multi_day, normalize_code
from at0.backtest import (
    BacktestParams,
    backtest_multi_day,
    summarize_one_stock,
    extract_trades,
)
from at0.strategy import SignalParams
from at0.risk import RiskParams


# ═══════════════════════════════════════════════════════════════
# 参数适配（内联自 batch_backtest.adapt_params / cli.adapt_params_by_frequency）
# ═══════════════════════════════════════════════════════════════
def adapt_params(params: BacktestParams, frequency: str, bars_per_day: int) -> BacktestParams:
    """按频率自适应 warmup/eod_check 参数。"""
    if frequency == "5min":
        params.warmup_bars = min(6, max(3, bars_per_day // 8))
        params.eod_check_bar_idx = min(33, bars_per_day - 2)
    else:
        params.warmup_bars = 30
        params.eod_check_bar_idx = 200
    return params


# ═══════════════════════════════════════════════════════════════
# 回测一只股票
# ═══════════════════════════════════════════════════════════════
def run_backtest(code: str, start: str = "2025-07-24", end: str = "2026-07-22"):
    """跑单只股票回测，返回 (summary, result, trades)。"""
    daily_bars, daily_prev, daily_meta = fetch_multi_day(
        code, start, end, "baostock", use_cache=True
    )
    if not daily_bars:
        raise RuntimeError(f"{code} 无数据")

    pure_code = normalize_code(code)["pure"]
    first_meta = next(iter(daily_meta.values()))
    freq = first_meta.get("frequency", "5min")
    bpd = first_meta.get("bars_count", 48)
    first_date = min(daily_prev.keys())
    avg_cost = daily_prev[first_date]

    params = BacktestParams(
        base_shares=3000,
        avg_cost=avg_cost,
        signal_params=SignalParams(),
        risk_params=RiskParams(),
    )
    adapt_params(params, freq, bpd)

    result = backtest_multi_day(pure_code, daily_bars, daily_prev, params)
    summary = summarize_one_stock(pure_code, result)
    trades = extract_trades(result)
    return summary, result, trades


# ═══════════════════════════════════════════════════════════════
# 统计分析
# ═══════════════════════════════════════════════════════════════
def analyze(code: str, summary: dict, result: dict, trades: list[dict]) -> dict:
    """对单只股票的 trades 做盈亏分布统计。"""
    # 仅看已配对/止损 trade（paired=True），open 腿是未平仓的，pnl=0
    paired_trades = [t for t in trades if t.get("paired")]
    wins = [t for t in paired_trades if t.get("pnl", 0) > 0]
    losses = [t for t in paired_trades if t.get("pnl", 0) < 0]
    flats = [t for t in paired_trades if t.get("pnl", 0) == 0]

    n_win = len(wins)
    n_loss = len(losses)
    n_total = len(paired_trades)

    sum_win = sum(t["pnl"] for t in wins)
    sum_loss = sum(t["pnl"] for t in losses)
    avg_win = sum_win / n_win if n_win else 0.0
    avg_loss = sum_loss / n_loss if n_loss else 0.0
    win_rate = n_win / n_total if n_total else 0.0
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    # 按 status 分组（trades 列表里的 status: paired / stopped / open）
    by_status = {}
    for t in paired_trades:
        st = t.get("status", "unknown")
        by_status.setdefault(st, []).append(t)
    status_stats = {}
    for st, lst in by_status.items():
        pnls = [t.get("pnl", 0) for t in lst]
        status_stats[st] = {
            "count": len(lst),
            "ratio": len(lst) / n_total if n_total else 0.0,
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "total_pnl": sum(pnls),
        }

    # expired 腿不在 trades 列表里，从 result 顶层字段取
    expired_count = result.get("expired_legs_count", 0)
    expired_pnl = result.get("expired_legs_real_pnl", 0.0)
    status_stats["expired"] = {
        "count": expired_count,
        "ratio": expired_count / n_total if n_total else 0.0,
        "avg_pnl": expired_pnl / expired_count if expired_count else 0.0,
        "total_pnl": expired_pnl,
    }

    # 持仓时长分桶（holding_bars）
    def bucket(hb: int) -> str:
        if hb <= 0:
            return "0-开仓当根"
        if hb <= 3:
            return "1-3 (短)"
        if hb <= 6:
            return "4-6 (中)"
        if hb <= 12:
            return "7-12 (长)"
        return "超时 (>12)"
    holding_buckets = {}
    for t in paired_trades:
        b = bucket(t.get("holding_bars", 0))
        holding_buckets.setdefault(b, []).append(t)
    holding_stats = {}
    for b, lst in holding_buckets.items():
        pnls = [t.get("pnl", 0) for t in lst]
        wins_b = [p for p in pnls if p > 0]
        holding_stats[b] = {
            "count": len(lst),
            "avg_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
            "total_pnl": sum(pnls),
            "win_rate": len(wins_b) / len(pnls) if pnls else 0.0,
        }

    # 极值
    max_win = max((t["pnl"] for t in wins), default=0.0)
    max_loss = min((t["pnl"] for t in losses), default=0.0)
    max_abs_loss = abs(max_loss)
    extreme_ratio = abs(max_win / max_loss) if max_loss != 0 else float("inf")

    return {
        "code": code,
        "summary": summary,
        "n_total_trades": len(trades),
        "n_paired": n_total,
        "n_open": len(trades) - n_total,
        "n_win": n_win,
        "n_loss": n_loss,
        "n_flat": len(flats),
        "win_rate": win_rate,
        "sum_win": sum_win,
        "sum_loss": sum_loss,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_loss_ratio": profit_loss_ratio,
        "gross_pnl": summary.get("gross_pnl", 0.0),
        "total_cost": summary.get("total_cost", 0.0),
        "net_pnl": summary.get("net_pnl", 0.0),
        "net_pnl_with_unrealized": summary.get("net_pnl_with_unrealized", 0.0),
        "unrealized_pnl": summary.get("unrealized_pnl", 0.0),
        "status_stats": status_stats,
        "holding_stats": holding_stats,
        "max_win": max_win,
        "max_loss": max_loss,
        "max_abs_loss": max_abs_loss,
        "extreme_ratio": extreme_ratio,
        # 赢小亏大核心指标
        "win_loss_size_ratio": profit_loss_ratio,  # avg_win / |avg_loss|
    }


# ═══════════════════════════════════════════════════════════════
# 打印辅助
# ═══════════════════════════════════════════════════════════════
def fmt(v, sign=False, w=12, prec=2):
    if isinstance(v, float) and abs(v) == float("inf"):
        return "inf".rjust(w)
    if sign:
        return f"{v:+,.{prec}f}".rjust(w)
    return f"{v:,.{prec}f}".rjust(w)


def print_row(label, a, b, w=12, prec=2, sign=False):
    print(f"  {label:<28} {fmt(a, sign, w, prec)} {fmt(b, sign, w, prec)}")


def print_report(stats_list: list[dict]):
    """并排对比两只股票。"""
    s1, s2 = stats_list
    c1, c2 = s1["code"], s2["code"]
    header = f"  {'指标':<28} {c1:>12} {c2:>12}"
    sep = "  " + "-" * (28 + 1 + 12 + 1 + 12)

    print("\n" + "=" * 70)
    print("单笔盈亏分布对比报告 — 趋势跟随 T+0 策略结构性缺陷验证")
    print("=" * 70)

    print("\n【1. 总览（来自 summary）】")
    print(header)
    print(sep)
    print_row("总 trade 数", s1["n_total_trades"], s2["n_total_trades"], w=12, prec=0)
    print_row("已配对 trade 数", s1["n_paired"], s2["n_paired"], w=12, prec=0)
    print_row("未配对(跨日) trade 数", s1["n_open"], s2["n_open"], w=12, prec=0)
    print_row("毛盈亏(gross_pnl)", s1["gross_pnl"], s2["gross_pnl"], sign=True)
    print_row("总成本(total_cost)", s1["total_cost"], s2["total_cost"], sign=False)
    print_row("净盈亏(net_pnl)", s1["net_pnl"], s2["net_pnl"], sign=True)
    print_row("未配对浮盈(unrealized)", s1["unrealized_pnl"], s2["unrealized_pnl"], sign=True)
    print_row("净盈亏含浮盈(net_w_u)", s1["net_pnl_with_unrealized"], s2["net_pnl_with_unrealized"], sign=True)

    print("\n【2. 盈亏比（基于已配对 trade）】")
    print(header)
    print(sep)
    print_row("盈利单数", s1["n_win"], s2["n_win"], w=12, prec=0)
    print_row("亏损单数", s1["n_loss"], s2["n_loss"], w=12, prec=0)
    print_row("胜率", s1["win_rate"] * 100, s2["win_rate"] * 100, w=12, prec=1)
    print_row("平均盈利 avg_win", s1["avg_win"], s2["avg_win"], sign=True)
    print_row("平均亏损 avg_loss", s1["avg_loss"], s2["avg_loss"], sign=True)
    print_row("总盈利 sum_win", s1["sum_win"], s2["sum_win"], sign=True)
    print_row("总亏损 sum_loss", s1["sum_loss"], s2["sum_loss"], sign=True)
    print_row("盈亏比 win/|loss|", s1["profit_loss_ratio"], s2["profit_loss_ratio"], w=12, prec=2)

    print("\n【3. 按 status 分组】")
    print(f"  {'status':<12} | {'count':>6} {'ratio':>7} {'avg_pnl':>12} {'total_pnl':>12} || "
          f"{'count':>6} {'ratio':>7} {'avg_pnl':>12} {'total_pnl':>12}")
    print("  " + "-" * 110)
    all_statuses = ["paired", "stopped", "expired", "open"]
    for st in all_statuses:
        a = s1["status_stats"].get(st, {"count": 0, "ratio": 0, "avg_pnl": 0, "total_pnl": 0})
        b = s2["status_stats"].get(st, {"count": 0, "ratio": 0, "avg_pnl": 0, "total_pnl": 0})
        print(f"  {st:<12} | {a['count']:>6} {a['ratio']*100:>6.1f}% "
              f"{fmt(a['avg_pnl'], sign=True):>12} {fmt(a['total_pnl'], sign=True):>12} || "
              f"{b['count']:>6} {b['ratio']*100:>6.1f}% "
              f"{fmt(b['avg_pnl'], sign=True):>12} {fmt(b['total_pnl'], sign=True):>12}")

    print("\n【4. 止损腿分析（亏损主因定位）】")
    print(header)
    print(sep)
    stp1 = s1["status_stats"].get("stopped", {})
    stp2 = s2["status_stats"].get("stopped", {})
    print_row("止损腿数量", stp1.get("count", 0), stp2.get("count", 0), w=12, prec=0)
    print_row("止损腿占比(%)", stp1.get("ratio", 0) * 100, stp2.get("ratio", 0) * 100, w=12, prec=1)
    print_row("止损腿平均亏损", stp1.get("avg_pnl", 0), stp2.get("avg_pnl", 0), sign=True)
    print_row("止损腿总亏损", stp1.get("total_pnl", 0), stp2.get("total_pnl", 0), sign=True)
    exp1 = s1["status_stats"].get("expired", {})
    exp2 = s2["status_stats"].get("expired", {})
    print_row("超时腿数量", exp1.get("count", 0), exp2.get("count", 0), w=12, prec=0)
    print_row("超时腿平均亏损", exp1.get("avg_pnl", 0), exp2.get("avg_pnl", 0), sign=True)
    print_row("超时腿总亏损", exp1.get("total_pnl", 0), exp2.get("total_pnl", 0), sign=True)

    print("\n【5. 持仓时长 vs 盈亏分布】")
    print(f"  {'holding_bars':<14} | {'count':>6} {'win%':>6} {'avg_pnl':>12} {'total_pnl':>12} || "
          f"{'count':>6} {'win%':>6} {'avg_pnl':>12} {'total_pnl':>12}")
    print("  " + "-" * 110)
    bucket_order = ["0-开仓当根", "1-3 (短)", "4-6 (中)", "7-12 (长)", "超时 (>12)"]
    for b in bucket_order:
        a = s1["holding_stats"].get(b, {"count": 0, "avg_pnl": 0, "total_pnl": 0, "win_rate": 0})
        bb = s2["holding_stats"].get(b, {"count": 0, "avg_pnl": 0, "total_pnl": 0, "win_rate": 0})
        if a["count"] == 0 and bb["count"] == 0:
            continue
        print(f"  {b:<14} | {a['count']:>6} {a['win_rate']*100:>5.0f}% "
              f"{fmt(a['avg_pnl'], sign=True):>12} {fmt(a['total_pnl'], sign=True):>12} || "
              f"{bb['count']:>6} {bb['win_rate']*100:>5.0f}% "
              f"{fmt(bb['avg_pnl'], sign=True):>12} {fmt(bb['total_pnl'], sign=True):>12}")

    print("\n【6. 极值】")
    print(header)
    print(sep)
    print_row("单笔最大盈利 max_win", s1["max_win"], s2["max_win"], sign=True)
    print_row("单笔最大亏损 max_loss", s1["max_loss"], s2["max_loss"], sign=True)
    print_row("最大盈/最大亏 比", s1["extreme_ratio"], s2["extreme_ratio"], w=12, prec=2)

    print("\n【7. 赢小亏大验证】")
    print(header)
    print(sep)
    print_row("平均盈利 avg_win", s1["avg_win"], s2["avg_win"], sign=True)
    print_row("|平均亏损| |avg_loss|", abs(s1["avg_loss"]), abs(s2["avg_loss"]), sign=False)
    print_row("盈亏比(avg/avg)", s1["profit_loss_ratio"], s2["profit_loss_ratio"], w=12, prec=2)
    print_row("总盈利 sum_win", s1["sum_win"], s2["sum_win"], sign=True)
    print_row("|总亏损| |sum_loss|", abs(s1["sum_loss"]), abs(s2["sum_loss"]), sign=False)
    # 总盈/总亏比
    tot_ratio1 = abs(s1["sum_win"] / s1["sum_loss"]) if s1["sum_loss"] != 0 else float("inf")
    tot_ratio2 = abs(s2["sum_win"] / s2["sum_loss"]) if s2["sum_loss"] != 0 else float("inf")
    print_row("总盈/总亏 比", tot_ratio1, tot_ratio2, w=12, prec=2)


def print_conclusion(stats_list: list[dict]):
    """给出结论。"""
    s1, s2 = stats_list
    print("\n" + "=" * 70)
    print("结论分析")
    print("=" * 70)

    for s in stats_list:
        code = s["code"]
        print(f"\n--- {code} ---")
        plr = s["profit_loss_ratio"]
        wr = s["win_rate"]
        net = s["net_pnl_with_unrealized"]
        gross = s["gross_pnl"]
        cost = s["total_cost"]
        stp = s["status_stats"].get("stopped", {})
        exp = s["status_stats"].get("expired", {})
        max_win = s["max_win"]
        max_loss = s["max_abs_loss"]

        # 赢小亏大判定：盈亏比 < 1 视为典型赢小亏大
        is_win_small_loss_big = plr < 1.0
        print(f"  胜率={wr*100:.1f}%  盈亏比={plr:.2f}  "
              f"avg_win={s['avg_win']:+.0f}  avg_loss={s['avg_loss']:+.0f}")
        if is_win_small_loss_big:
            print(f"  ⚠ 结构性缺陷【赢小亏大】：盈利单平均 {s['avg_win']:+.0f} 远小于"
                  f"亏损单平均 {s['avg_loss']:+.0f}（盈亏比 {plr:.2f} < 1）")
        else:
            print(f"  ✓ 盈亏比 {plr:.2f} ≥ 1，未呈现典型赢小亏大")

        # 止损是否过度
        stp_ratio = stp.get("ratio", 0)
        stp_total = stp.get("total_pnl", 0)
        if stp_ratio > 0.3:
            print(f"  ⚠ 止损腿占比 {stp_ratio*100:.1f}%（>30%），止损偏频繁；"
                  f"止损腿总亏损 {stp_total:+.0f}")
        else:
            print(f"  止损腿占比 {stp_ratio*100:.1f}%，止损频率正常")

        # 超时腿
        if exp.get("count", 0) > 0:
            print(f"  超时腿 {exp['count']} 条，总亏损 {exp['total_pnl']:+.0f}，"
                  f"avg {exp['avg_pnl']:+.0f}")

        # 问题在盈利端还是亏损端
        # 用最大盈利 vs 最大亏损作辅助判断
        if max_loss > 0 and max_win > 0:
            extreme = max_win / max_loss
            print(f"  极值：max_win={max_win:+.0f} vs |max_loss|={max_loss:+.0f}，"
                  f"比值 {extreme:.2f}")
            if extreme < 0.5:
                print(f"  → 大亏显著超过大赢，亏损端有失控单笔")
            elif plr < 1.0:
                print(f"  → 盈利端规模不足（赢小），非单笔大亏主导")

        # 毛盈亏 vs 净盈亏：成本吞噬
        if gross > 0 and net < 0:
            print(f"  ⚠ 毛盈亏 {gross:+.0f} 为正但净 {net:+.0f} 为负，"
                  f"成本 {cost:+.0f} 吞噬全部 alpha")
        elif gross < 0:
            print(f"  毛盈亏已为负 {gross:+.0f}，策略本身方向亏损，"
                  f"非成本问题")
        else:
            print(f"  毛盈亏 {gross:+.0f} 净 {net:+.0f}，alpha 保留")

    # 对比总结
    print("\n--- 两股对比总结 ---")
    plr1, plr2 = s1["profit_loss_ratio"], s2["profit_loss_ratio"]
    wr1, wr2 = s1["win_rate"], s2["win_rate"]
    print(f"  {s1['code']}: 胜率 {wr1*100:.0f}% / 盈亏比 {plr1:.2f} / net_w_u {s1['net_pnl_with_unrealized']:+.0f}")
    print(f"  {s2['code']}: 胜率 {wr2*100:.0f}% / 盈亏比 {plr2:.2f} / net_w_u {s2['net_pnl_with_unrealized']:+.0f}")

    if plr1 < 1.0 and plr2 < 1.0:
        print("  ★ 两只股票均呈现【赢小亏大】结构性缺陷，盈利端规模系统性不足")
    elif plr2 < 1.0:
        print(f"  ★ 亏损股 {s2['code']} 呈现【赢小亏大】，盈利股 {s1['code']} 盈亏比正常")
    else:
        print("  ★ 两股盈亏比均 ≥ 1，未呈现系统性赢小亏大")


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════
def main():
    codes = ["sh.600184", "sh.603061"]
    stats_list = []
    for code in codes:
        print(f"[run] {code} 回测中 ...", end=" ", flush=True)
        summary, result, trades = run_backtest(code)
        print(f"trades={len(trades)} net={summary['net_pnl']:+.0f} "
              f"net_w_u={summary['net_pnl_with_unrealized']:+.0f}")
        stats_list.append(analyze(code, summary, result, trades))

    print_report(stats_list)
    print_conclusion(stats_list)


if __name__ == "__main__":
    main()
