"""
A-T0 T-eligible 候选筛选器
========================
方案 v0.2 第二节：对持仓/候选标的进行 T-eligible 筛选。

筛选条件:
  1. 20日平均振幅 ≥ 3.5%（振幅太小扣完成本无利可图）
  2. 20日日均成交额 ≥ 1亿元（保证挂单能成交，滑点可控）
  3. 当日非一字板/一字跌停（封死无法成交）
  4. 单笔预期捕获空间 ≥ 0.6%（覆盖来回交易成本后正边际）

数据源:
  - westock kline（日级，取最近20日）+ quote（当日状态）  [默认]
  - baostock 日线（westock 不可用时降级，仅检查条件1+2，跳过3+4）

独立性: 仅依赖 at0.data（westock CLI 封装），不依赖其他业务模块。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .data import run_westock, to_westock_symbol


@dataclass
class ScreenerParams:
    """候选筛选参数（方案 v0.2 第二节起始值）。"""
    min_20d_amplitude: float = 0.035       # 20日平均振幅 ≥ 3.5%
    min_20d_amount: float = 1.0e8          # 20日日均成交额 ≥ 1亿元
    min_capture_spread: float = 0.006      # 单笔预期捕获空间 ≥ 0.6%


DEFAULT_SCREENER_PARAMS = ScreenerParams()


@dataclass
class ScreenResult:
    """单只股票的筛选结果。"""
    code: str
    eligible: bool
    reasons: list[str]
    avg_amplitude_20d: Optional[float] = None
    avg_amount_20d: Optional[float] = None
    is_one_word_board: Optional[bool] = None
    expected_capture: Optional[float] = None


def screen_candidate(code: str, params: Optional[ScreenerParams] = None) -> ScreenResult:
    """
    对单只股票进行 T-eligible 筛选。

    返回 ScreenResult。任何数据源失败时对应检查项跳过（不阻塞其他项）。
    """
    params = params or DEFAULT_SCREENER_PARAMS
    symbol = to_westock_symbol(code)
    result = ScreenResult(code=code, eligible=False, reasons=[])

    # ── 检查 1+2: 20日振幅 + 日均成交额（从日 K 线获取）──
    kline_data = run_westock(f"kline {symbol} --period daily --count 20")
    if isinstance(kline_data, list) and len(kline_data) >= 20:
        amplitudes = []
        amounts = []
        for bar in kline_data[-20:]:
            high = float(bar.get("high", 0))
            low = float(bar.get("low", 0))
            prev_close = float(bar.get("prev_close", 0))
            amount = float(bar.get("amount", 0))
            if prev_close > 0:
                amplitudes.append((high - low) / prev_close)
            amounts.append(amount)
        if amplitudes:
            result.avg_amplitude_20d = sum(amplitudes) / len(amplitudes)
            if result.avg_amplitude_20d >= params.min_20d_amplitude:
                result.reasons.append(f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% ≥ {params.min_20d_amplitude*100:.1f}% ✓")
            else:
                result.reasons.append(f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% < {params.min_20d_amplitude*100:.1f}% ✗")
        if amounts:
            result.avg_amount_20d = sum(amounts) / len(amounts)
            if result.avg_amount_20d >= params.min_20d_amount:
                result.reasons.append(f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 ≥ 1亿 ✓")
            else:
                result.reasons.append(f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 < 1亿 ✗")
    else:
        result.reasons.append("日K线数据不足，跳过振幅/成交额检查 ⚠")

    # ── 检查 3: 当日非一字板（从 quote 获取）──
    quote_data = run_westock(f"quote {symbol}")
    if isinstance(quote_data, list) and quote_data:
        q = quote_data[0]
        open_price = float(q.get("open", 0))
        high = float(q.get("high", 0))
        low = float(q.get("low", 0))
        ceiling = float(q.get("price_ceiling", 0))
        floor = float(q.get("price_floor", 0))
        if ceiling > 0 and open_price >= ceiling and high == low:
            result.is_one_word_board = True
            result.reasons.append("当日一字涨停板，无法做T ✗")
        elif floor > 0 and open_price <= floor and high == low:
            result.is_one_word_board = True
            result.reasons.append("当日一字跌停板，无法做T ✗")
        else:
            result.is_one_word_board = False
            result.reasons.append("非一字板，可成交 ✓")

        # ── 检查 4: 单笔预期捕获空间（用当日振幅近似）──
        prev_close = float(q.get("prev_close", 0))
        if prev_close > 0 and high > low:
            result.expected_capture = (high - low) / prev_close
            if result.expected_capture >= params.min_capture_spread:
                result.reasons.append(f"预期捕获 {result.expected_capture*100:.2f}% ≥ {params.min_capture_spread*100:.1f}% ✓")
            else:
                result.reasons.append(f"预期捕获 {result.expected_capture*100:.2f}% < {params.min_capture_spread*100:.1f}% ✗")
    else:
        result.reasons.append("无实时报价，跳过一字板/捕获空间检查 ⚠")

    # ── 综合判定：所有 ✗ 项不出现即为 eligible ──
    has_fail = any("✗" in r for r in result.reasons)
    result.eligible = not has_fail and len(result.reasons) >= 3
    return result


if __name__ == "__main__":
    print("=== T-eligible 候选筛选自检（实盘 sh600000）===")
    r = screen_candidate("sh600000")
    print(f"  code: {r.code}")
    print(f"  eligible: {r.eligible}")
    print(f"  20日均振幅: {r.avg_amplitude_20d*100:.2f}%" if r.avg_amplitude_20d else "  20日均振幅: N/A")
    print(f"  20日均额: {r.avg_amount_20d/1e8:.2f}亿" if r.avg_amount_20d else "  20日均额: N/A")
    print(f"  一字板: {r.is_one_word_board}")
    print(f"  预期捕获: {r.expected_capture*100:.2f}%" if r.expected_capture else "  预期捕获: N/A")
    for reason in r.reasons:
        print(f"    {reason}")


# ═══════════════════════════════════════════════════════════════
# baostock 数据源（westock 不可用时降级）
# ═══════════════════════════════════════════════════════════════
def _normalize_to_bs_code(code: str) -> Optional[str]:
    """把任意格式代码归一化为 baostock 代码（sh.600000 / sz.000001）。"""
    s = code.strip().lower().replace(".sh", "").replace(".sz", "")
    s = s.replace("sh", "").replace("sz", "").replace(".", "")
    if not (len(s) == 6 and s.isdigit()):
        return None
    head = s[0]
    if head == "6":
        return f"sh.{s}"
    elif head in ("0", "3"):
        return f"sz.{s}"
    return None


def _screen_candidate_bs_core(
    bs_code: str,
    params: ScreenerParams,
    start_str: str,
    end_str: str,
    result: ScreenResult,
) -> None:
    """
    baostock 筛选核心逻辑（假设已登录）。

    查询日线 → 计算振幅/成交额 → 填充 result.reasons。
    任何失败写入 result.reasons 带 ✗ 标记，由调用方判定 eligible。
    """
    import baostock as bs

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,preclose,amount",
        start_date=start_str,
        end_date=end_str,
        frequency="d",
        adjustflag="2",  # 2=前复权
    )
    if rs.error_code != "0":
        result.reasons.append(f"baostock 查询失败: {rs.error_msg} ✗")
        return

    rows = []
    while rs.next():
        rows.append(rs.get_row_data())

    if len(rows) < 20:
        result.reasons.append(f"日线数据不足({len(rows)}<20)，跳过 ✗")
        return

    rows = rows[-20:]
    amplitudes = []
    amounts = []
    for row in rows:
        try:
            high = float(row[2])
            low = float(row[3])
            preclose = float(row[5])
            amount = float(row[6])
            if preclose > 0:
                amplitudes.append((high - low) / preclose)
            amounts.append(amount)
        except (ValueError, IndexError):
            continue

    if amplitudes:
        result.avg_amplitude_20d = sum(amplitudes) / len(amplitudes)
        if result.avg_amplitude_20d >= params.min_20d_amplitude:
            result.reasons.append(
                f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% ≥ {params.min_20d_amplitude*100:.1f}% ✓"
            )
        else:
            result.reasons.append(
                f"20日均振幅 {result.avg_amplitude_20d*100:.2f}% < {params.min_20d_amplitude*100:.1f}% ✗"
            )

    if amounts:
        result.avg_amount_20d = sum(amounts) / len(amounts)
        if result.avg_amount_20d >= params.min_20d_amount:
            result.reasons.append(
                f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 ≥ 1亿 ✓"
            )
        else:
            result.reasons.append(
                f"20日均额 {result.avg_amount_20d/1e8:.2f}亿 < 1亿 ✗"
            )

    result.is_one_word_board = None
    result.expected_capture = result.avg_amplitude_20d
    result.reasons.append("baostock 无实时报价，跳过一字板/捕获空间检查 ⚠")


def screen_candidate_baostock(
    code: str,
    params: Optional[ScreenerParams] = None,
    end_date: Optional[str] = None,
) -> ScreenResult:
    """
    用 baostock 日线数据进行 T-eligible 筛选（条件1+2）。

    baostock 无法获取当日实时 quote，故跳过条件3（一字板）和条件4（预期捕获空间）。
    筛选结果仅基于 20 日历史振幅 + 成交额，适合作为批量候选池预筛。

    :param code: 任意格式代码（sh600000 / 600000.SH / sh.600000 均可）
    :param end_date: 截止日期 YYYY-MM-DD，默认今天
    """
    import baostock as bs
    from datetime import datetime, timedelta

    params = params or DEFAULT_SCREENER_PARAMS
    result = ScreenResult(code=code, eligible=False, reasons=[])

    bs_code = _normalize_to_bs_code(code)
    if bs_code is None:
        result.reasons.append(f"代码格式错误: {code} ✗")
        return result

    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    start = end - timedelta(days=35)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        result.reasons.append(f"baostock 登录失败: {lg.error_msg} ✗")
        return result
    try:
        _screen_candidate_bs_core(bs_code, params, start_str, end_str, result)
    finally:
        bs.logout()

    has_fail = any("✗" in r for r in result.reasons)
    result.eligible = not has_fail and len(result.reasons) >= 2
    return result


def screen_hs300_baostock(
    params: Optional[ScreenerParams] = None,
    end_date: Optional[str] = None,
    max_count: int = 20,
) -> list[ScreenResult]:
    """
    从沪深300成分股中批量筛选 T-eligible 候选。

    共享单个 baostock session（避免 300 次 login/logout）。
    :return: 符合条件的 ScreenResult 列表（按振幅降序）
    """
    import baostock as bs
    from datetime import datetime, timedelta

    params = params or DEFAULT_SCREENER_PARAMS

    end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()
    start = end - timedelta(days=35)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    lg = bs.login()
    if lg.error_code != "0":
        print(f"[screener] baostock 登录失败: {lg.error_msg}")
        return []

    try:
        rs = bs.query_hs300_stocks()
        if rs.error_code != "0":
            print(f"[screener] 获取沪深300成分股失败: {rs.error_msg}")
            return []

        hs300_codes = []
        while rs.next():
            row = rs.get_row_data()
            hs300_codes.append((row[1], row[2] if len(row) > 2 else ""))
        print(f"[screener] 沪深300成分股共 {len(hs300_codes)} 只，开始批量筛选...")

        eligible_list: list[ScreenResult] = []
        for i, (code, name) in enumerate(hs300_codes):
            result = ScreenResult(code=code, eligible=False, reasons=[])
            _screen_candidate_bs_core(code, params, start_str, end_str, result)

            has_fail = any("✗" in r for r in result.reasons)
            result.eligible = not has_fail and len(result.reasons) >= 2

            if result.eligible:
                eligible_list.append(result)
                print(f"  [{i+1}/{len(hs300_codes)}] {code} {name}  ✓ 振幅={result.avg_amplitude_20d*100:.2f}% 额={result.avg_amount_20d/1e8:.1f}亿")
            else:
                fails = [x for x in result.reasons if "✗" in x]
                if i % 20 == 0:
                    print(f"  [{i+1}/{len(hs300_codes)}] 进度... 当前入选 {len(eligible_list)} 只")

            if len(eligible_list) >= max_count:
                print(f"[screener] 已达目标数量 {max_count}，停止（扫描了 {i+1} 只）")
                break
    finally:
        bs.logout()

    eligible_list.sort(key=lambda x: x.avg_amplitude_20d or 0, reverse=True)
    return eligible_list
