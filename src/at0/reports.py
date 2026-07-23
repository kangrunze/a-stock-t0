"""
reports 层合并模块
==================
本模块聚合 at0 项目的报告生成能力，包含 json_report + html_report 两类输出：

  - html_report（单股回测 HTML 报告）：读取 backtest_multi_day 的 result + daily_bars，
    生成自包含 HTML 报告，含顶部汇总指标卡片、累计净盈亏曲线、按月净盈亏柱状图、
    每日 K 线 + 交易点图（分时/日/周/月可切换）。
  - html_report（批量回测汇总 HTML 报告）：读取 batch_summary.json，生成自包含 HTML
    报告，含整体汇总指标卡片、按股票净盈亏排行柱状图、胜率分布散点图、按股票明细
    表格（可排序）。

合并来源：
  - scripts/backtest_report_html.py  -> save_html_report
  - scripts/batch_report_html.py     -> save_batch_html_report
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime


# ═══ reports: backtest_report_html（单股回测HTML报告） ═══


def _fmt(v, prefix=""):
    """格式化数字。"""
    if v is None:
        return "N/A"
    if isinstance(v, (int, float)):
        return f"{prefix}{v:,.2f}" if isinstance(v, float) else f"{prefix}{v:,}"
    return str(v)


def _extract_daily_summary(result: dict) -> list[dict]:
    """提取逐日汇总（用于曲线和柱状图）。"""
    rows = []
    for dr in result.get("daily_results", []):
        rows.append({
            "date": dr["date"],
            "net_pnl": round(dr.get("net_pnl", 0), 2),
            "t_trades": dr.get("t_trades", 0),
            "win_rate": round(dr.get("win_rate", 0), 4),
            "eod_status": dr.get("eod_status", ""),
        })
    return rows


def _extract_monthly(daily_summary: list[dict]) -> list[dict]:
    """按月聚合。"""
    from collections import defaultdict
    monthly = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "days": 0})
    for d in daily_summary:
        month = d["date"][:7]
        monthly[month]["pnl"] += d["net_pnl"]
        monthly[month]["trades"] += d["t_trades"]
        monthly[month]["days"] += 1
    return [
        {"month": m, "days": v["days"], "trades": v["trades"],
         "pnl": round(v["pnl"], 2)}
        for m, v in sorted(monthly.items())
    ]


def _get_week_key(date_str: str) -> str:
    """ISO 周键 'YYYY-Www'。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _get_month_key(date_str: str) -> str:
    """月键 'YYYY-MM'。"""
    return date_str[:7]


def _aggregate_daily_candles(daily_bars: dict) -> list[dict]:
    """将日内分钟 bars 聚合为日 K 线（每根 = 1 个交易日）。"""
    candles = []
    for date in sorted(daily_bars.keys()):
        bars = daily_bars[date]
        if not bars:
            continue
        candles.append({
            "t": date,
            "o": round(bars[0]["open"], 3),
            "h": round(max(b["high"] for b in bars), 3),
            "l": round(min(b["low"] for b in bars), 3),
            "c": round(bars[-1]["close"], 3),
            "v": int(sum(b.get("volume", 0) for b in bars)),
        })
    return candles


def _aggregate_period_candles(
    daily_candles: list[dict],
    key_fn,
) -> list[dict]:
    """按 key_fn 分组聚合日 K 线为周/月 K 线。"""
    from collections import OrderedDict
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for dc in daily_candles:
        k = key_fn(dc["t"])
        if k not in groups:
            groups[k] = []
        groups[k].append(dc)

    result = []
    for k, items in groups.items():
        result.append({
            "t": k,
            "o": items[0]["o"],
            "h": max(i["h"] for i in items),
            "l": min(i["l"] for i in items),
            "c": items[-1]["c"],
            "v": sum(i["v"] for i in items),
        })
    return result


def _extract_trades_flat(result: dict) -> list[dict]:
    """提取扁平交易列表（带日期）。"""
    trades = []
    for dr in result.get("daily_results", []):
        d = dr["date"]
        for tr in dr.get("trades", []):
            trades.append({
                "date": d,
                "time": tr.get("time", ""),
                "dir": tr.get("direction", ""),
                "price": round(tr.get("fill_price", 0), 3),
                "shares": tr.get("shares", 0),
                "pnl": round(tr.get("pnl", 0), 2),
                "paired": tr.get("paired", False),
            })
    return trades


def _map_trades_to_period(
    trades: list[dict],
    key_fn,
) -> list[dict]:
    """将交易映射到周/月周期（t = 周期键）。"""
    points = []
    for tr in trades:
        k = key_fn(tr["date"])
        points.append({
            "t": k,
            "dir": tr["dir"],
            "price": tr["price"],
            "shares": tr["shares"],
            "pnl": tr["pnl"],
            "paired": tr["paired"],
            "date": tr["date"],
            "time": tr["time"],
        })
    return points


def _build_intraday_dataset(daily_bars: dict, result: dict) -> list[dict]:
    """
    构建分时图数据：每个交易日的分钟级价格走势 + 买卖点。

    :return: [{"date": "2026-07-22", "points": [{"t":"09:35","price":5.04,"v":123},...],
               "trades": [{"t":"10:15","dir":"buy","price":5.06,...},...]}]
    """
    # 交易按日期分组
    trade_map = {}
    for dr in result.get("daily_results", []):
        d = dr["date"]
        trade_map[d] = dr.get("trades", [])

    intraday_days = []
    for date in sorted(daily_bars.keys()):
        bars = daily_bars[date]
        if not bars:
            continue
        points = []
        for b in bars:
            t = b.get("time", "")
            if " " in t:
                t = t.split(" ")[1][:5]
            elif len(t) >= 16:
                t = t[11:16]
            points.append({
                "t": t,
                "price": round(b["close"], 3),
                "v": int(b.get("volume", 0)),
            })
        # 当日交易点
        day_trades = trade_map.get(date, [])
        trade_points = []
        for tr in day_trades:
            tm = tr.get("time", "")
            if " " in tm:
                tm = tm.split(" ")[1][:5]
            elif len(tm) >= 16:
                tm = tm[11:16]
            trade_points.append({
                "t": tm,
                "dir": tr.get("direction", ""),
                "price": round(tr.get("fill_price", 0), 3),
                "shares": tr.get("shares", 0),
                "pnl": round(tr.get("pnl", 0), 2),
                "paired": tr.get("paired", False),
            })
        intraday_days.append({
            "date": date,
            "points": points,
            "trades": trade_points,
        })

    return intraday_days


def _build_kline_datasets(daily_bars: dict, result: dict) -> dict:
    """
    构建分时/日/周/月四个维度的 K 线 + 交易点数据集。

    :return: {"intraday": [...], "day": {"candles":[...], "trades":[...]}, "week": {...}, "month": {...}}
    """
    daily_candles = _aggregate_daily_candles(daily_bars)
    weekly_candles = _aggregate_period_candles(daily_candles, _get_week_key)
    monthly_candles = _aggregate_period_candles(daily_candles, _get_month_key)
    intraday_days = _build_intraday_dataset(daily_bars, result)

    trades_flat = _extract_trades_flat(result)

    return {
        "intraday": intraday_days,
        "day": {
            "candles": daily_candles,
            "trades": [{"t": t["date"], "dir": t["dir"], "price": t["price"],
                        "shares": t["shares"], "pnl": t["pnl"], "paired": t["paired"],
                        "date": t["date"], "time": t["time"]}
                       for t in trades_flat],
        },
        "week": {
            "candles": weekly_candles,
            "trades": _map_trades_to_period(trades_flat, _get_week_key),
        },
        "month": {
            "candles": monthly_candles,
            "trades": _map_trades_to_period(trades_flat, _get_month_key),
        },
    }


def save_html_report(
    result: dict,
    daily_bars: dict,
    out_path: Path,
    code: str = "",
    start_date: str = "",
    end_date: str = "",
) -> Path:
    """
    生成自包含 HTML 回测报告。

    :param result: backtest_multi_day 返回的 result dict
    :param daily_bars: {date: [bar, ...]} 日内 K 线数据
    :param out_path: 输出 HTML 路径
    :param code: 股票代码
    :param start_date: 起始日期
    :param end_date: 结束日期
    :return: 输出路径
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    daily_summary = _extract_daily_summary(result)
    monthly = _extract_monthly(daily_summary)
    kline_datasets = _build_kline_datasets(daily_bars, result)

    # 汇总指标
    total_pnl = result.get("net_pnl", 0)
    unrealized = result.get("unrealized_pnl", 0)
    total_with_unrealized = result.get("net_pnl_with_unrealized", total_pnl + unrealized)
    win_rate = result.get("win_rate", 0)
    total_trades = result.get("total_trades", 0)
    avg_trades = result.get("avg_trades_per_day", 0)
    total_days = result.get("total_days", len(daily_summary))

    # 累计盈亏
    cum = []
    s = 0
    for d in daily_summary:
        s += d["net_pnl"]
        cum.append(round(s, 2))
    cum_peak = max(cum) if cum else 0
    cum_trough = min(cum) if cum else 0

    # 盈亏天数
    win_days = sum(1 for d in daily_summary if d["net_pnl"] > 0)
    loss_days = sum(1 for d in daily_summary if d["net_pnl"] < 0)
    no_trade_days = sum(1 for d in daily_summary if d["t_trades"] == 0)

    # 序列化数据嵌入 HTML
    daily_json = json.dumps(daily_summary, ensure_ascii=False)
    monthly_json = json.dumps(monthly, ensure_ascii=False)
    kline_json = json.dumps(kline_datasets, ensure_ascii=False)
    cum_json = json.dumps(cum, ensure_ascii=False)

    pnl_class = "neg" if total_with_unrealized < 0 else "pos"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{code} 回测报告 {start_date}~{end_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #f5f6f8; color: #1f2937; padding: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
.header {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.title {{ font-size: 20px; font-weight: 600; }}
.subtitle {{ font-size: 13px; color: #6b7280; margin-top: 4px; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }}
.metric {{ background: #f7f8fa; border-radius: 8px; padding: 12px 16px; }}
.metric-label {{ font-size: 12px; color: #6b7280; }}
.metric-value {{ font-size: 18px; font-weight: 600; margin-top: 4px; font-family: "SF Mono", Menlo, Consolas, monospace; }}
.pos {{ color: #10b981; }}
.neg {{ color: #ef4444; }}
.chart-block {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px;
               box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.chart-title {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; }}
.chart-wrap {{ position: relative; height: 300px; }}
.chart-wrap-sm {{ position: relative; height: 220px; }}
.legend {{ display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: #6b7280; }}
.legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; font-weight: 500; color: #6b7280; padding: 8px 12px; border-bottom: 1px solid #e5e7eb; font-size: 12px; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #e5e7eb; }}
tr:last-child td {{ border-bottom: none; }}
.kline-controls {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
.kline-controls select {{ padding: 6px 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 13px; }}
.kline-controls button {{ padding: 6px 12px; border: 1px solid #d1d5db; border-radius: 6px; background: #fff;
                          cursor: pointer; font-size: 13px; }}
.kline-controls button:hover {{ background: #f3f4f6; }}
.kline-canvas-wrap {{ position: relative; width: 100%; overflow-x: auto; }}
#klineCanvas {{ display: block; }}
.kline-legend {{ font-size: 12px; color: #6b7280; margin-top: 8px; display: flex; gap: 16px; flex-wrap: wrap; }}
.kline-info {{ font-size: 13px; color: #374151; margin-top: 8px; }}
.bar-cell {{ display: inline-block; height: 8px; border-radius: 2px; vertical-align: middle; }}
.section-tabs {{ display: flex; gap: 4px; margin-bottom: 12px; }}
.tab {{ padding: 8px 16px; border: 1px solid #d1d5db; border-radius: 6px; background: #fff; cursor: pointer; font-size: 13px; }}
.tab.active {{ background: #7c3aed; color: #fff; border-color: #7c3aed; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">{code} 回测报告</div>
    <div class="subtitle">{start_date} ~ {end_date} · {total_days} 个交易日 · 数据源见报告 JSON</div>
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">净盈亏（含浮盈）</div>
        <div class="metric-value {pnl_class}">{_fmt(total_with_unrealized, '+' if total_with_unrealized>=0 else '')}</div>
      </div>
      <div class="metric">
        <div class="metric-label">胜率</div>
        <div class="metric-value">{win_rate*100:.1f}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">总交易笔数</div>
        <div class="metric-value">{total_trades}</div>
      </div>
      <div class="metric">
        <div class="metric-label">日均T次数</div>
        <div class="metric-value">{avg_trades:.2f}</div>
      </div>
    </div>
    <div class="metrics" style="margin-top:8px;">
      <div class="metric">
        <div class="metric-label">已实现净盈亏</div>
        <div class="metric-value {'neg' if total_pnl<0 else 'pos'}">{_fmt(total_pnl, '+' if total_pnl>=0 else '')}</div>
      </div>
      <div class="metric">
        <div class="metric-label">未配对浮盈浮亏</div>
        <div class="metric-value {'neg' if unrealized<0 else 'pos'}">{_fmt(unrealized, '+' if unrealized>=0 else '')}</div>
      </div>
      <div class="metric">
        <div class="metric-label">盈利/亏损天数</div>
        <div class="metric-value">{win_days} / {loss_days}</div>
      </div>
      <div class="metric">
        <div class="metric-label">无交易天数</div>
        <div class="metric-value">{no_trade_days}</div>
      </div>
    </div>
  </div>

  <div class="chart-block">
    <div class="chart-title">累计净盈亏曲线</div>
    <div class="chart-wrap"><canvas id="cumChart"></canvas></div>
    <div class="legend">
      <span><span class="legend-dot" style="background:#7c3aed"></span>累计净盈亏</span>
      <span>峰值 +{cum_peak:,.2f} · 谷值 {cum_trough:,.2f}</span>
    </div>
  </div>

  <div class="chart-block">
    <div class="chart-title">按月净盈亏</div>
    <div class="chart-wrap-sm"><canvas id="monthChart"></canvas></div>
  </div>

  <div class="chart-block">
    <div class="chart-title">月度明细</div>
    <table>
      <thead><tr><th>月份</th><th>交易日</th><th>交易笔数</th><th>净盈亏</th><th>盈亏占比</th></tr></thead>
      <tbody id="monthBody"></tbody>
    </table>
  </div>

  <div class="chart-block">
    <div class="chart-title">K线 + 交易点</div>
    <div class="section-tabs">
      <button class="tab kline-tab" data-period="intraday">分时</button>
      <button class="tab kline-tab active" data-period="day">日K</button>
      <button class="tab kline-tab" data-period="week">周K</button>
      <button class="tab kline-tab" data-period="month">月K</button>
      <span id="klineStatus" style="font-size:12px;color:#6b7280;margin-left:8px;align-self:center;"></span>
    </div>
    <div id="intradayControls" class="kline-controls" style="display:none;">
      <button id="prevIntraday">◀ 上一日</button>
      <select id="intradaySelect"></select>
      <button id="nextIntraday">下一日 ▶</button>
    </div>
    <div class="kline-canvas-wrap">
      <canvas id="klineCanvas" width="1100" height="400"></canvas>
    </div>
    <div class="kline-legend">
      <span id="legendKline"><span class="legend-dot" style="background:#10b981"></span>阳线（收盘≥开盘）</span>
      <span id="legendKline2"><span class="legend-dot" style="background:#ef4444"></span>阴线（收盘&lt;开盘）</span>
      <span><span class="legend-dot" style="background:#3b82f6;border-radius:50%"></span>买入点</span>
      <span><span class="legend-dot" style="background:#f59e0b;border-radius:50%"></span>卖出点</span>
      <span id="legendIntraday" style="display:none;"><span class="legend-dot" style="background:#7c3aed"></span>分时价格线</span>
    </div>
    <div class="kline-info" id="klineInfo"></div>
  </div>
</div>

<script>
var dailyData = {daily_json};
var monthlyData = {monthly_json};
var klineData = {kline_json};
var cumData = {cum_json};

// ── 月度表格 ──
var maxAbs = Math.max.apply(null, monthlyData.map(function(x){{return Math.abs(x.pnl);}}));
var monthBody = document.getElementById('monthBody');
monthlyData.forEach(function(row){{
  var pct = maxAbs > 0 ? Math.abs(row.pnl) / maxAbs * 50 : 0;
  var isPos = row.pnl > 0;
  var tr = document.createElement('tr');
  tr.innerHTML = '<td>' + row.month + '</td><td>' + row.days + '</td><td>' + row.trades + '</td>'
    + '<td style="color:' + (isPos ? '#10b981' : (row.pnl<0?'#ef4444':'#6b7280')) + '">' + (row.pnl>=0?'+':'') + row.pnl.toFixed(2) + '</td>'
    + '<td><span class="bar-cell" style="width:' + pct + '%;background:' + (isPos?'#10b981':'#ef4444') + '"></span></td>';
  monthBody.appendChild(tr);
}});

// ── Chart.js: 累计盈亏 ──
if (typeof Chart !== 'undefined') {{
  new Chart(document.getElementById('cumChart'), {{
    type: 'line',
    data: {{
      labels: dailyData.map(function(x){{return x.date;}}),
      datasets: [{{
        label: '累计净盈亏',
        data: cumData,
        borderColor: '#7c3aed',
        backgroundColor: 'rgba(124,58,237,0.1)',
        fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{display:false}}, tooltip: {{mode:'index', intersect:false}} }},
      scales: {{
        x: {{ ticks: {{maxTicksLimit:10, font:{{size:11}}}}, grid:{{display:false}} }},
        y: {{ ticks: {{font:{{size:11}}}}, grid:{{color:'#e5e7eb'}} }}
      }}
    }}
  }});

  new Chart(document.getElementById('monthChart'), {{
    type: 'bar',
    data: {{
      labels: monthlyData.map(function(x){{return x.month;}}),
      datasets: [{{
        label: '月度净盈亏',
        data: monthlyData.map(function(x){{return x.pnl;}}),
        backgroundColor: monthlyData.map(function(x){{return x.pnl>=0?'#10b981':'#ef4444';}}),
        borderRadius: 4
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{display:false}} }},
      scales: {{
        x: {{ ticks: {{font:{{size:11}}}}, grid:{{display:false}} }},
        y: {{ ticks: {{font:{{size:11}}}}, grid:{{color:'#e5e7eb'}} }}
      }}
    }}
  }});
}}

// ── K线 + 交易点绘制（分时/日/周/月切换）──
var klineCanvas = document.getElementById('klineCanvas');
var kctx = klineCanvas.getContext('2d');
var klineInfo = document.getElementById('klineInfo');
var klineStatus = document.getElementById('klineStatus');
var intradayControls = document.getElementById('intradayControls');
var intradaySelect = document.getElementById('intradaySelect');
var legendKline = document.getElementById('legendKline');
var legendKline2 = document.getElementById('legendKline2');
var legendIntraday = document.getElementById('legendIntraday');
var curPeriod = 'day';
var curIntradayIdx = 0;

// 填充分时日期下拉
var intradayDays = klineData['intraday'] || [];
intradayDays.forEach(function(d, i){{
  var opt = document.createElement('option');
  opt.value = i;
  opt.text = d.date + ' (T' + d.trades.length + '笔)';
  intradaySelect.appendChild(opt);
}});

function drawIntraday(idx){{
  if (idx < 0 || idx >= intradayDays.length) return;
  curIntradayIdx = idx;
  intradaySelect.value = idx;
  var day = intradayDays[idx];
  var points = day.points;
  var trades = day.trades;
  var n = points.length;
  if (n === 0) return;

  var W = klineCanvas.width;
  var H = klineCanvas.height;
  var padL = 50, padR = 60, padT = 20, padB = 40;
  var plotW = W - padL - padR;
  var plotH = H - padT - padB;
  var gap = plotW / n;

  // 价格范围
  var pMin = Infinity, pMax = -Infinity, vMax = 0;
  points.forEach(function(p){{
    pMin = Math.min(pMin, p.price);
    pMax = Math.max(pMax, p.price);
    vMax = Math.max(vMax, p.v);
  }});
  trades.forEach(function(t){{ pMax = Math.max(pMax, t.price); pMin = Math.min(pMin, t.price); }});
  var pRange = pMax - pMin;
  if (pRange < 0.01) {{ pRange = pMax * 0.02; pMin -= pRange/2; pMax += pRange/2; }}
  pMin -= pRange * 0.05; pMax += pRange * 0.05;

  kctx.clearRect(0, 0, W, H);

  // 网格 + Y轴标签
  kctx.strokeStyle = '#e5e7eb';
  kctx.fillStyle = '#6b7280';
  kctx.font = '11px sans-serif';
  kctx.lineWidth = 1;
  var ySteps = 5;
  for (var i = 0; i <= ySteps; i++) {{
    var y = padT + plotH * i / ySteps;
    var price = pMax - (pMax - pMin) * i / ySteps;
    kctx.beginPath();
    kctx.moveTo(padL, y);
    kctx.lineTo(W - padR, y);
    kctx.stroke();
    kctx.textAlign = 'right';
    kctx.fillText(price.toFixed(2), padL - 4, y + 4);
  }}

  // 成交量（底部）
  var volH = padB * 0.5;
  var volBaseY = H - padB + volH;
  points.forEach(function(p, i){{
    var x = padL + gap * (i + 0.5);
    var vh = vMax > 0 ? (p.v / vMax) * volH : 0;
    kctx.fillStyle = 'rgba(124,58,237,0.15)';
    kctx.fillRect(x - gap*0.3, volBaseY - vh, gap*0.6, vh);
  }});

  // 分时价格折线
  kctx.strokeStyle = '#7c3aed';
  kctx.lineWidth = 1.5;
  kctx.beginPath();
  points.forEach(function(p, i){{
    var x = padL + gap * (i + 0.5);
    var y = padT + (pMax - p.price) / (pMax - pMin) * plotH;
    if (i === 0) kctx.moveTo(x, y);
    else kctx.lineTo(x, y);
  }});
  kctx.stroke();

  // 价格填充区
  kctx.lineTo(padL + gap * (n - 0.5), padT + plotH);
  kctx.lineTo(padL + gap * 0.5, padT + plotH);
  kctx.closePath();
  kctx.fillStyle = 'rgba(124,58,237,0.06)';
  kctx.fill();

  // X轴时间标签
  kctx.fillStyle = '#6b7280';
  kctx.textAlign = 'center';
  var labelStep = Math.max(1, Math.floor(n / 10));
  for (var i = 0; i < n; i += labelStep) {{
    var x = padL + gap * (i + 0.5);
    kctx.fillText(points[i].t, x, H - 4);
  }}

  // 交易点
  var tradeCount = 0;
  trades.forEach(function(t){{
    var tIdx = -1;
    for (var i = 0; i < n; i++) {{
      if (points[i].t === t.t) {{ tIdx = i; break; }}
    }}
    if (tIdx < 0) tIdx = Math.floor(n / 2);
    var x = padL + gap * (tIdx + 0.5);
    var y = padT + (pMax - t.price) / (pMax - pMin) * plotH;

    var ptColor = t.dir === 'buy' ? '#3b82f6' : '#f59e0b';
    kctx.fillStyle = ptColor;
    kctx.strokeStyle = '#fff';
    kctx.lineWidth = 2;
    kctx.beginPath();
    kctx.arc(x, y, 6, 0, Math.PI * 2);
    kctx.fill();
    kctx.stroke();

    // 标注
    kctx.fillStyle = ptColor;
    kctx.font = 'bold 10px sans-serif';
    kctx.textAlign = 'center';
    var label = (t.dir === 'buy' ? 'B' : 'S') + ' ' + t.price.toFixed(2);
    kctx.fillText(label, x, t.dir === 'buy' ? y + 18 : y - 10);
    tradeCount++;
  }});

  // 状态信息
  klineStatus.textContent = '分时 · ' + day.date + ' · ' + n + '根 · ' + tradeCount + '笔交易';

  // 交易明细
  if (trades.length > 0) {{
    var html = '<b>' + day.date + ' 分时交易明细（' + trades.length + '笔）：</b><br>';
    trades.forEach(function(t){{
      var color = t.dir === 'buy' ? '#3b82f6' : '#f59e0b';
      var pairTag = t.paired ? ' <span style="color:#10b981">已配对</span>' : ' <span style="color:#9ca3af">未配对</span>';
      html += '<span style="color:' + color + '">' + (t.dir==='buy'?'买入':'卖出') + '</span> '
            + t.t + ' ' + t.shares + '股 @' + t.price.toFixed(3)
            + ' PnL=' + (t.pnl>=0?'+':'') + t.pnl.toFixed(2) + pairTag + '<br>';
    }});
    klineInfo.innerHTML = html;
  }} else {{
    klineInfo.innerHTML = '<span style="color:#9ca3af">当日无交易</span>';
  }}
}}

function drawKline(period){{
  curPeriod = period;

  // 分时模式：显示日期选择器，切换 legend
  if (period === 'intraday') {{
    intradayControls.style.display = 'flex';
    legendKline.style.display = 'none';
    legendKline2.style.display = 'none';
    legendIntraday.style.display = '';
    // 默认选最后一个有交易的日
    var initIdx = intradayDays.length - 1;
    for (var i = intradayDays.length - 1; i >= 0; i--) {{
      if (intradayDays[i].trades.length > 0) {{ initIdx = i; break; }}
    }}
    drawIntraday(initIdx);
    return;
  }}

  // K线模式：隐藏日期选择器
  intradayControls.style.display = 'none';
  legendKline.style.display = '';
  legendKline2.style.display = '';
  legendIntraday.style.display = 'none';

  var ds = klineData[period];
  if (!ds || !ds.candles || ds.candles.length === 0) {{
    klineStatus.textContent = '无数据';
    klineInfo.innerHTML = '<span style="color:#9ca3af">该周期无K线数据</span>';
    return;
  }}
  var candles = ds.candles;
  var trades = ds.trades;
  var n = candles.length;

  var W = klineCanvas.width;
  var H = klineCanvas.height;
  var padL = 50, padR = 60, padT = 20, padB = 40;
  var plotW = W - padL - padR;
  var plotH = H - padT - padB;
  var candleW = Math.max(3, plotW / n * 0.7);
  var gap = plotW / n;

  // 价格范围
  var pMin = Infinity, pMax = -Infinity, vMax = 0;
  candles.forEach(function(c){{
    pMin = Math.min(pMin, c.l);
    pMax = Math.max(pMax, c.h);
    vMax = Math.max(vMax, c.v);
  }});
  trades.forEach(function(t){{ pMax = Math.max(pMax, t.price); pMin = Math.min(pMin, t.price); }});
  var pRange = pMax - pMin;
  if (pRange < 0.01) {{ pRange = pMax * 0.02; pMin -= pRange/2; pMax += pRange/2; }}
  pMin -= pRange * 0.05; pMax += pRange * 0.05;

  kctx.clearRect(0, 0, W, H);

  // 网格 + Y轴标签
  kctx.strokeStyle = '#e5e7eb';
  kctx.fillStyle = '#6b7280';
  kctx.font = '11px sans-serif';
  kctx.lineWidth = 1;
  var ySteps = 5;
  for (var i = 0; i <= ySteps; i++) {{
    var y = padT + plotH * i / ySteps;
    var price = pMax - (pMax - pMin) * i / ySteps;
    kctx.beginPath();
    kctx.moveTo(padL, y);
    kctx.lineTo(W - padR, y);
    kctx.stroke();
    kctx.textAlign = 'right';
    kctx.fillText(price.toFixed(2), padL - 4, y + 4);
  }}

  // 成交量（底部）
  var volH = padB * 0.5;
  var volBaseY = H - padB + volH;
  candles.forEach(function(c, i){{
    var x = padL + gap * (i + 0.5);
    var vh = vMax > 0 ? (c.v / vMax) * volH : 0;
    var isUp = c.c >= c.o;
    kctx.fillStyle = isUp ? 'rgba(16,185,129,0.3)' : 'rgba(239,68,68,0.3)';
    kctx.fillRect(x - candleW/2, volBaseY - vh, candleW, vh);
  }});

  // K线
  candles.forEach(function(c, i){{
    var x = padL + gap * (i + 0.5);
    var yOpen = padT + (pMax - c.o) / (pMax - pMin) * plotH;
    var yClose = padT + (pMax - c.c) / (pMax - pMin) * plotH;
    var yHigh = padT + (pMax - c.h) / (pMax - pMin) * plotH;
    var yLow = padT + (pMax - c.l) / (pMax - pMin) * plotH;
    var isUp = c.c >= c.o;
    var color = isUp ? '#10b981' : '#ef4444';

    // 影线
    kctx.strokeStyle = color;
    kctx.lineWidth = 1;
    kctx.beginPath();
    kctx.moveTo(x, yHigh);
    kctx.lineTo(x, yLow);
    kctx.stroke();

    // 实体
    kctx.fillStyle = color;
    var bodyTop = Math.min(yOpen, yClose);
    var bodyH = Math.max(1, Math.abs(yClose - yOpen));
    kctx.fillRect(x - candleW/2, bodyTop, candleW, bodyH);
  }});

  // X轴标签（每隔若干根显示一个）
  kctx.fillStyle = '#6b7280';
  kctx.textAlign = 'center';
  var labelStep = Math.max(1, Math.floor(n / 10));
  for (var i = 0; i < n; i += labelStep) {{
    var x = padL + gap * (i + 0.5);
    kctx.fillText(candles[i].t, x, H - 4);
  }}

  // 交易点
  var tradeCount = 0;
  trades.forEach(function(t){{
    var tIdx = -1;
    for (var i = 0; i < n; i++) {{
      if (candles[i].t === t.t) {{ tIdx = i; break; }}
    }}
    if (tIdx < 0) return;
    var x = padL + gap * (tIdx + 0.5);
    var y = padT + (pMax - t.price) / (pMax - pMin) * plotH;

    var ptColor = t.dir === 'buy' ? '#3b82f6' : '#f59e0b';
    kctx.fillStyle = ptColor;
    kctx.strokeStyle = '#fff';
    kctx.lineWidth = 2;
    kctx.beginPath();
    kctx.arc(x, y, 5, 0, Math.PI * 2);
    kctx.fill();
    kctx.stroke();
    tradeCount++;
  }});

  // 状态信息
  var periodLabel = period === 'day' ? '日K' : (period === 'week' ? '周K' : '月K');
  klineStatus.textContent = periodLabel + ' · ' + n + '根K线 · ' + tradeCount + '笔交易';

  // 交易明细
  if (trades.length > 0) {{
    var html = '<b>' + periodLabel + ' 交易明细（' + trades.length + '笔）：</b><br>';
    trades.forEach(function(t){{
      var color = t.dir === 'buy' ? '#3b82f6' : '#f59e0b';
      var pairTag = t.paired ? ' <span style="color:#10b981">已配对</span>' : ' <span style="color:#9ca3af">未配对</span>';
      var dateTag = t.date ? t.date + ' ' : '';
      var timeTag = t.time || t.t;
      html += '<span style="color:' + color + '">' + (t.dir==='buy'?'买入':'卖出') + '</span> '
            + dateTag + (timeTag || '') + ' ' + t.shares + '股 @' + t.price.toFixed(3)
            + ' PnL=' + (t.pnl>=0?'+':'') + t.pnl.toFixed(2) + pairTag + '<br>';
    }});
    klineInfo.innerHTML = html;
  }} else {{
    klineInfo.innerHTML = '<span style="color:#9ca3af">该周期无交易</span>';
  }}
}}

// Tab 切换
document.querySelectorAll('.kline-tab').forEach(function(btn){{
  btn.addEventListener('click', function(){{
    document.querySelectorAll('.kline-tab').forEach(function(b){{ b.classList.remove('active'); }});
    btn.classList.add('active');
    drawKline(btn.dataset.period);
  }});
}});

// 分时日期切换
intradaySelect.addEventListener('change', function(){{ drawIntraday(parseInt(this.value)); }});
document.getElementById('prevIntraday').addEventListener('click', function(){{
  if (curIntradayIdx > 0) drawIntraday(curIntradayIdx - 1);
}});
document.getElementById('nextIntraday').addEventListener('click', function(){{
  if (curIntradayIdx < intradayDays.length - 1) drawIntraday(curIntradayIdx + 1);
}});

// 默认画日K
drawKline('day');
</script>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    return out_path


# ═══ reports: batch_report_html（批量回测汇总HTML报告） ═══


def save_batch_html_report(
    summary: dict,
    out_path: Path,
) -> Path:
    """
    生成批量回测 HTML 报告。

    :param summary: batch_summary.json 的 dict 结构
    :param out_path: 输出 HTML 路径
    :return: 输出路径
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    overall = summary.get("overall", {})
    per_stock = summary.get("per_stock", [])
    start = summary.get("start", "")
    end = summary.get("end", "")
    source = summary.get("source", "")

    # 序列化数据
    per_stock_json = json.dumps(per_stock, ensure_ascii=False)

    net_pnl = overall.get("net_pnl", 0)
    unrealized = overall.get("unrealized_pnl", 0)
    total_with_unrealized = overall.get("net_pnl_with_unrealized", net_pnl + unrealized)
    win_rate = overall.get("win_rate", 0)
    total_trades = overall.get("total_trades", 0)
    paired_trades = overall.get("paired_trades", 0)
    stocks = overall.get("stocks", 0)
    profitable = overall.get("profitable_stocks", 0)
    losing = overall.get("losing_stocks", 0)
    final_legs = overall.get("final_open_legs_count", 0)
    gross_pnl = overall.get("gross_pnl", 0)
    total_cost = overall.get("total_cost", 0)

    pnl_class = "neg" if total_with_unrealized < 0 else "pos"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>批量回测报告 {start}~{end}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #f5f6f8; color: #1f2937; padding: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
.header {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.title {{ font-size: 20px; font-weight: 600; }}
.subtitle {{ font-size: 13px; color: #6b7280; margin-top: 4px; }}
.metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }}
.metric {{ background: #f7f8fa; border-radius: 8px; padding: 12px 16px; }}
.metric-label {{ font-size: 12px; color: #6b7280; }}
.metric-value {{ font-size: 18px; font-weight: 600; margin-top: 4px; font-family: "SF Mono", Menlo, Consolas, monospace; }}
.pos {{ color: #10b981; }}
.neg {{ color: #ef4444; }}
.chart-block {{ background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 16px;
               box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.chart-title {{ font-size: 15px; font-weight: 600; margin-bottom: 12px; }}
.chart-wrap {{ position: relative; height: 320px; }}
.chart-wrap-tall {{ position: relative; height: 500px; }}
.legend {{ display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: #6b7280; flex-wrap: wrap; }}
.legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: right; font-weight: 500; color: #6b7280; padding: 8px 12px; border-bottom: 2px solid #e5e7eb; font-size: 12px; cursor: pointer; white-space: nowrap; }}
th:first-child {{ text-align: left; }}
th:hover {{ color: #7c3aed; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #e5e7eb; text-align: right; font-family: "SF Mono", Menlo, Consolas, monospace; }}
td:first-child {{ text-align: left; font-family: -apple-system, sans-serif; }}
tr:hover {{ background: #f9fafb; }}
.bar-cell {{ display: inline-block; height: 10px; border-radius: 2px; vertical-align: middle; }}
.sort-arrow {{ font-size: 10px; color: #9ca3af; margin-left: 2px; }}
.sort-arrow.active {{ color: #7c3aed; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">批量回测报告</div>
    <div class="subtitle">{start} ~ {end} · {stocks} 只股票 · 数据源 {source} · 底仓 {summary.get('base_shares', 3000)}股</div>
    <div class="metrics">
      <div class="metric">
        <div class="metric-label">净盈亏（含浮盈）</div>
        <div class="metric-value {pnl_class}">{total_with_unrealized:+,.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">整体胜率</div>
        <div class="metric-value">{win_rate*100:.1f}%</div>
      </div>
      <div class="metric">
        <div class="metric-label">总交易笔数</div>
        <div class="metric-value">{total_trades}</div>
      </div>
      <div class="metric">
        <div class="metric-label">配对笔数</div>
        <div class="metric-value">{paired_trades}</div>
      </div>
    </div>
    <div class="metrics" style="margin-top:8px;">
      <div class="metric">
        <div class="metric-label">已实现净盈亏</div>
        <div class="metric-value {'neg' if net_pnl<0 else 'pos'}">{net_pnl:+,.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">未配对浮盈浮亏</div>
        <div class="metric-value {'neg' if unrealized<0 else 'pos'}">{unrealized:+,.2f}</div>
      </div>
      <div class="metric">
        <div class="metric-label">盈利/亏损股票</div>
        <div class="metric-value">{profitable} / {losing}</div>
      </div>
      <div class="metric">
        <div class="metric-label">回测结束未配对腿</div>
        <div class="metric-value">{final_legs}</div>
      </div>
    </div>
  </div>

  <div class="chart-block">
    <div class="chart-title">按股票净盈亏（含浮盈，降序）</div>
    <div class="chart-wrap-tall"><canvas id="pnlChart"></canvas></div>
    <div class="legend">
      <span><span class="legend-dot" style="background:#10b981"></span>盈利</span>
      <span><span class="legend-dot" style="background:#ef4444"></span>亏损</span>
    </div>
  </div>

  <div class="chart-block">
    <div class="chart-title">胜率 vs 净盈亏散点图</div>
    <div class="chart-wrap"><canvas id="scatterChart"></canvas></div>
    <div class="legend">
      <span><span class="legend-dot" style="background:#7c3aed;border-radius:50%"></span>每只股票</span>
      <span>X轴：胜率（%） · Y轴：净盈亏含浮盈（元）</span>
    </div>
  </div>

  <div class="chart-block">
    <div class="chart-title">按股票明细（点击表头排序）</div>
    <table id="stockTable">
      <thead>
        <tr>
          <th data-key="code">代码</th>
          <th data-key="total_trades">总交易<span class="sort-arrow"></span></th>
          <th data-key="paired_trades">配对<span class="sort-arrow"></span></th>
          <th data-key="win_rate">胜率<span class="sort-arrow"></span></th>
          <th data-key="net_pnl">已实现<span class="sort-arrow"></span></th>
          <th data-key="unrealized_pnl">浮盈浮亏<span class="sort-arrow"></span></th>
          <th data-key="net_pnl_with_unrealized">含浮盈净盈亏<span class="sort-arrow active">▼</span></th>
          <th data-key="final_open_legs_count">未配对腿<span class="sort-arrow"></span></th>
          <th>盈亏占比</th>
        </tr>
      </thead>
      <tbody id="stockBody"></tbody>
    </table>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
var perStock = {per_stock_json};
var sortKey = 'net_pnl_with_unrealized';
var sortDesc = true;

// ── 表格渲染 ──
var maxAbsPnl = Math.max.apply(null, perStock.map(function(s){{return Math.abs(s.net_pnl_with_unrealized || 0);}}));

function renderTable(){{
  var sorted = perStock.slice().sort(function(a, b){{
    var va = a[sortKey] || 0, vb = b[sortKey] || 0;
    if (typeof va === 'string') return sortDesc ? vb.localeCompare(va) : va.localeCompare(vb);
    return sortDesc ? vb - va : va - vb;
  }});
  var tbody = document.getElementById('stockBody');
  tbody.innerHTML = '';
  sorted.forEach(function(s){{
    var pnl = s.net_pnl_with_unrealized || 0;
    var isPos = pnl > 0;
    var pnlColor = isPos ? '#10b981' : (pnl < 0 ? '#ef4444' : '#6b7280');
    var pct = maxAbsPnl > 0 ? Math.abs(pnl) / maxAbsPnl * 60 : 0;
    var wr = s.paired_trades > 0 ? (s.win_rate * 100).toFixed(1) + '%' : 'N/A';
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + s.code + '</td>'
      + '<td>' + s.total_trades + '</td>'
      + '<td>' + s.paired_trades + '</td>'
      + '<td>' + wr + '</td>'
      + '<td style="color:' + (s.net_pnl>=0?'#10b981':'#ef4444') + '">' + (s.net_pnl>=0?'+':'') + s.net_pnl.toFixed(2) + '</td>'
      + '<td style="color:' + (s.unrealized_pnl>=0?'#10b981':'#ef4444') + '">' + (s.unrealized_pnl>=0?'+':'') + s.unrealized_pnl.toFixed(2) + '</td>'
      + '<td style="color:' + pnlColor + ';font-weight:600">' + (pnl>=0?'+':'') + pnl.toFixed(2) + '</td>'
      + '<td>' + s.final_open_legs_count + '</td>'
      + '<td><span class="bar-cell" style="width:' + pct + '%;background:' + (isPos?'#10b981':'#ef4444') + '"></span></td>';
    tbody.appendChild(tr);
  }});

  // 更新排序箭头
  document.querySelectorAll('th[data-key]').forEach(function(th){{
    var arrow = th.querySelector('.sort-arrow');
    if (!arrow) return;
    if (th.dataset.key === sortKey) {{
      arrow.textContent = sortDesc ? '▼' : '▲';
      arrow.classList.add('active');
    }} else {{
      arrow.textContent = '';
      arrow.classList.remove('active');
    }}
  }});
}}

// 表头点击排序
document.querySelectorAll('th[data-key]').forEach(function(th){{
  th.addEventListener('click', function(){{
    var key = th.dataset.key;
    if (sortKey === key) {{
      sortDesc = !sortDesc;
    }} else {{
      sortKey = key;
      sortDesc = true;
    }}
    renderTable();
  }});
}});

renderTable();

// ── Chart.js: 按股票净盈亏柱状图 ──
if (typeof Chart !== 'undefined') {{
  var sortedByPnl = perStock.slice().sort(function(a, b){{
    return (b.net_pnl_with_unrealized||0) - (a.net_pnl_with_unrealized||0);
  }});
  new Chart(document.getElementById('pnlChart'), {{
    type: 'bar',
    data: {{
      labels: sortedByPnl.map(function(s){{return s.code;}}),
      datasets: [{{
        label: '净盈亏（含浮盈）',
        data: sortedByPnl.map(function(s){{return s.net_pnl_with_unrealized || 0;}}),
        backgroundColor: sortedByPnl.map(function(s){{return (s.net_pnl_with_unrealized||0) >= 0 ? '#10b981' : '#ef4444';}}),
        borderRadius: 3
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{display:false}},
        tooltip: {{ callbacks: {{
          label: function(ctx) {{
            var s = sortedByPnl[ctx.dataIndex];
            return ['净盈亏: ' + ctx.parsed.y.toFixed(2),
                    '胜率: ' + (s.paired_trades>0 ? (s.win_rate*100).toFixed(1)+'%' : 'N/A'),
                    '交易: ' + s.total_trades + '笔 (配对' + s.paired_trades + ')'];
          }}
        }}}}
      }},
      scales: {{
        x: {{ ticks: {{font:{{size:10}}, maxRotation:45, minRotation:45}}, grid:{{display:false}} }},
        y: {{ ticks: {{font:{{size:11}}}}, grid:{{color:'#e5e7eb'}} }}
      }}
    }}
  }});

  // ── 散点图：胜率 vs 净盈亏 ──
  var scatterData = perStock.filter(function(s){{return s.paired_trades > 0;}}).map(function(s){{
    return {{
      x: s.win_rate * 100,
      y: s.net_pnl_with_unrealized || 0,
      code: s.code,
      trades: s.total_trades
    }};
  }});
  new Chart(document.getElementById('scatterChart'), {{
    type: 'scatter',
    data: {{
      datasets: [{{
        label: '股票',
        data: scatterData,
        backgroundColor: 'rgba(124,58,237,0.6)',
        borderColor: '#7c3aed',
        pointRadius: 6,
        pointHoverRadius: 9
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{display:false}},
        tooltip: {{ callbacks: {{
          label: function(ctx) {{
            var d = ctx.raw;
            return [d.code, '胜率: ' + d.x.toFixed(1) + '%', '净盈亏: ' + d.y.toFixed(2)];
          }}
        }}}}
      }},
      scales: {{
        x: {{
          title: {{display:true, text:'胜率（%）', font:{{size:12}}}},
          ticks: {{font:{{size:11}}}}, grid:{{color:'#e5e7eb'}}
        }},
        y: {{
          title: {{display:true, text:'净盈亏含浮盈（元）', font:{{size:12}}}},
          ticks: {{font:{{size:11}}}}, grid:{{color:'#e5e7eb'}}
        }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    return out_path
