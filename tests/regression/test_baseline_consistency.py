#!/usr/bin/env python3
"""
Phase C 回归测试 — 回测 + 优化两条核心链路 + baseline 一致性
==============================================================
覆盖用户指定的两条核心链路：

1. 回测链路（python -m at0.cli backtest）
   - 引擎端到端跑通无异常
   - 信号触发（交易 > 0，证明 FIFO/T+1/regime/频率自适应被真正执行）
   - 跨日未配对腿归零（P0-9 账本闭环）
   - P0-1 风控收紧落地（单次 T 股数 ≤ 0.25×底仓，非旧 0.5）
   - 确定性：相同输入两次回测，报告字节一致（baseline 一致性守护）

2. 优化链路（python -m at0.cli optimize）
   - 候选池全部股票处理（无"无数据"/异常）
   - 聚合指标合理（净盈亏有限、胜率∈[0,1]、结束未配对腿=0）
   - baseline 落盘（batch_summary.json）
   - 确定性：两次优化，overall 字节一致

数据说明：
  沙箱无实时行情，测试依赖 scripts/gen_synthetic_bars.py 生成的确定性合成缓存
  （固定 seed + 稳定 code 派生，不受 PYTHONHASHSEED 影响）。合成数据仅用于
  链路/一致性验证，不代表真实收益分布。

运行：
  python tests/regression/test_baseline_consistency.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CACHE = ROOT / "data" / "multi_day_cache"
BT_OUT = ROOT / "outputs" / "backtest"
POOL = BT_OUT / "candidate_pool.json"

BT_START, BT_END = "2026-07-17", "2026-07-24"
OPT_START, OPT_END = "2026-06-22", "2026-07-22"
BASE_SHARES = 3000
SEED = 42

# ── 导入合成数据生成器（scripts/ 非包，用 importlib）──
_spec = importlib.util.spec_from_file_location(
    "gen_synthetic_bars", ROOT / "scripts" / "gen_synthetic_bars.py")
_gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gen)


def _candidate_codes() -> list[str]:
    d = json.load(open(POOL))
    return [c["code"].replace("sh.", "").replace("sz.", "")
            for c in d.get("candidates", [])]


def ensure_caches() -> list[str]:
    """确保回测 + 优化所需的合成缓存存在（缺失则生成，确定性）。"""
    codes = _candidate_codes()
    _gen.generate("600000", BT_START, BT_END, SEED, 11.50)
    _save("600000", BT_START, BT_END, SEED, 11.50)
    for c in codes:
        _save(c, OPT_START, OPT_END, SEED, 11.50)
    return codes


def _save(code: str, start: str, end: str, seed: int, base: float) -> None:
    path = CACHE / f"{code}_{start}_{end}.json"
    if path.exists():
        return
    payload = _gen.generate(code, start, end, seed, base)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_cli(entry: str, argv: list[str]) -> None:
    """调用 CLI 入口（run_backtest_cli / batch_main），吞掉 stdout。"""
    if entry == "backtest":
        from at0.cli import run_backtest_cli as fn
    else:
        from at0.cli import batch_main as fn
    old = sys.argv
    sys.argv = ["cli"] + argv
    try:
        with io.StringIO() as buf:
            with __import__("contextlib").redirect_stdout(buf):
                fn()
    finally:
        sys.argv = old


class Runner:
    def __init__(self):
        self.p = 0
        self.f = 0
        self.fail: list[str] = []

    def check(self, name: str, cond: bool, extra: str = "") -> None:
        if cond:
            self.p += 1
        else:
            self.f += 1
            self.fail.append(f"{name} {extra}".strip())

    def summary(self) -> tuple[int, int, list[str]]:
        return self.p, self.f, self.fail


def test_backtest_link(codes: list[str]) -> Runner:
    r = Runner()
    rep_path = BT_OUT / f"600000_{BT_START}_{BT_END}_report.json"
    trades_path = BT_OUT / f"600000_{BT_START}_{BT_END}_trades.json"

    # 注意：直接调用 run_backtest_cli 入口，不需传 "backtest" 子命令名
    _run_cli("backtest", ["--code", "600000",
                           "--start", BT_START, "--end", BT_END])

    rep = json.load(open(rep_path))
    r.check("回测无异常且报告有效", rep and rep.get("total_days", 0) > 0)
    r.check("信号触发(引擎被执行, 交易>0)", rep["total_trades"] > 0,
            f"trades={rep['total_trades']}")
    r.check("跨日未配对腿归零(P0-9 账本闭环)",
            rep["final_open_legs_count"] == 0,
            f"open={rep['final_open_legs_count']}")
    r.check("净盈亏为有限数值", isinstance(rep["net_pnl"], (int, float)))

    # P0-1：首次 T 股数应 = 0.25×底仓（无跨日carry时=750）；旧值 0.5 → 1500
    # 带跨日carry日 base 放大，股数可到 ~900（P0-9 正常），故上限给 1000 余量
    trades = json.load(open(trades_path))
    share_vals = [t["shares"] for t in trades]
    min_shares = min(share_vals, default=0)
    max_shares = max(share_vals, default=0)
    r.check("P0-1 风控收紧落地(首日股数≤750, 非旧1500)",
            min_shares <= 750, f"min_shares={min_shares}")
    r.check("单次T股数合理(≤1000, 排除旧0.5比例)",
            max_shares <= 1000, f"max_shares={max_shares}")
    return r


def test_backtest_determinism() -> Runner:
    r = Runner()
    rep_path = BT_OUT / f"600000_{BT_START}_{BT_END}_report.json"
    b1 = rep_path.read_bytes()
    _run_cli("backtest", ["--code", "600000",
                          "--start", BT_START, "--end", BT_END])
    b2 = rep_path.read_bytes()
    r.check("回测确定性(两次报告字节一致=baseline可复现)", b1 == b2)
    return r


def test_optimize_link(codes: list[str]) -> Runner:
    r = Runner()
    _run_cli("optimize", [])

    summ = json.load(open(BT_OUT / "batch_summary.json"))
    r.check("baseline 已保存(batch_summary.json)", summ is not None)
    r.check("候选池全部处理(无无数据/异常)",
            summ["overall"]["stocks"] == len(codes),
            f"stocks={summ['overall']['stocks']} want={len(codes)}")
    r.check("整体净盈亏有限且>0", summ["overall"]["net_pnl"] > 0,
            f"net={summ['overall']['net_pnl']}")
    r.check("整体胜率∈[0,1]", 0 <= summ["overall"]["win_rate"] <= 1)
    r.check("回测结束未配对腿=0", summ["overall"]["final_open_legs_count"] == 0,
            f"open={summ['overall']['final_open_legs_count']}")
    errs = [s for s in summ["per_stock"] if "error" in s]
    r.check("每只股票均无异常", len(errs) == 0, f"errors={len(errs)}")
    return r


def test_optimize_determinism() -> Runner:
    r = Runner()
    p = BT_OUT / "batch_summary.json"
    b1 = p.read_bytes()
    _run_cli("optimize", [])
    b2 = p.read_bytes()
    r.check("优化确定性(两次baseline字节一致)", b1 == b2)
    return r


def main() -> int:
    codes = ensure_caches()
    print(f"[setup] 合成缓存就绪：600000(回测) + {len(codes)}只(优化)")

    suites = [
        ("回测链路", test_backtest_link(codes)),
        ("回测确定性", test_backtest_determinism()),
        ("优化链路", test_optimize_link(codes)),
        ("优化确定性", test_optimize_determinism()),
    ]

    total_p = total_f = 0
    for name, rr in suites:
        p, f, fail = rr.summary()
        total_p += p
        total_f += f
        flag = "PASS" if f == 0 else "FAIL"
        print(f"[{flag}] {name}: {p} 通过, {f} 失败")
        for x in fail:
            print(f"      ✗ {x}")

    print(f"\n总计: {total_p} 通过, {total_f} 失败")
    return 1 if total_f else 0


if __name__ == "__main__":
    sys.exit(main())
