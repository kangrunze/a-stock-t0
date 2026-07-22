#!/usr/bin/env python3
"""
使用 BaoStock 下载沪深300分钟级别历史数据。

重要说明：
  1. BaoStock 不支持 1 分钟线，最细粒度为 5 分钟，支持 5/15/30/60 分钟。
  2. BaoStock 不提供「指数」(sh.000300) 的分钟线，只提供指数日线。
     因此用「沪深300ETF」(sh.510300) 的分钟线作为指数分钟走势的代理
     （ETF 紧密跟踪指数，价格水平不同但走势一致）。
  如需指数本身的真实分钟线，请改用 mootdx / westock-data 等数据源。

用法：
  python fetch_csi300_baostock.py                  # 默认近一周 5 分钟线 (ETF代理)
  python fetch_csi300_baostock.py --days 10 --frequency 15
  python fetch_csi300_baostock.py --code sh.000300 --frequency d  # 指数日线
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

import baostock as bs

# 沪深300ETF（作为指数分钟走势代理）
DEFAULT_CODE = "sh.510300"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data"


def fetch_minute_kline(
    code: str,
    start_date: str,
    end_date: str,
    frequency: str = "5",
) -> list[dict]:
    """
    拉取指定周期分钟K线。

    :param code: BaoStock 代码，如 sh.000300
    :param start_date: YYYY-MM-DD
    :param end_date:   YYYY-MM-DD
    :param frequency:  "5" / "15" / "30" / "60" / "d"(日线)
    :return: 行列表，每行一个 dict
    """
    # 分钟线含 time 字段；日线不含 time
    if frequency == "d":
        fields = "date,open,high,low,close,volume,amount"
    else:
        fields = "date,time,open,high,low,close,volume,amount"

    print(f"[INFO] 登录 BaoStock ...")
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")

    try:
        print(
            f"[INFO] 查询 {code} {frequency}分钟线  "
            f"{start_date} ~ {end_date}"
        )
        rs = bs.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag="3",  # 指数无需复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"查询失败: {rs.error_msg}")

        rows: list[dict] = []
        fields = rs.fields  # 字段名列表
        while rs.next():
            row = rs.get_row_data()  # list，顺序与 fields 一致
            rows.append(dict(zip(fields, row)))

        print(f"[INFO] 共获取 {len(rows)} 条记录")
        return rows
    finally:
        bs.logout()


def save_to_csv(rows: list[dict], out_path: Path) -> None:
    """保存到 CSV。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print("[WARN] 无数据，跳过写入 CSV")
        return
    headers = list(rows[0].keys())
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[INFO] 已保存 -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BaoStock 下载沪深300指数分钟级历史数据"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="回溯天数（自然日），默认 7（近一周）",
    )
    parser.add_argument(
        "--frequency",
        default="5",
        choices=["5", "15", "30", "60", "d"],
        help="周期：5/15/30/60 分钟，或 d 日线；默认 5",
    )
    parser.add_argument(
        "--code",
        default=DEFAULT_CODE,
        help=f"BaoStock 代码，默认 {DEFAULT_CODE}(沪深300ETF)",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="结束日期 YYYY-MM-DD，默认今天",
    )
    args = parser.parse_args()

    end = (
        datetime.strptime(args.end_date, "%Y-%m-%d")
        if args.end_date
        else datetime.now()
    )
    start = end - timedelta(days=args.days)
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")

    rows = fetch_minute_kline(args.code, start_date, end_date, args.frequency)

    freq_tag = args.frequency + "min" if args.frequency != "d" else "day"
    code_tag = args.code.replace(".", "")
    out_path = (
        OUTPUT_DIR
        / f"{code_tag}_{freq_tag}_{start_date}_{end_date}.csv"
    )
    save_to_csv(rows, out_path)

    # 简要预览
    if rows:
        print("\n[预览] 前 3 行:")
        for r in rows[:3]:
            print(" ", r)
        print("[预览] 后 3 行:")
        for r in rows[-3:]:
            print(" ", r)


if __name__ == "__main__":
    main()
