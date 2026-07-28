"""Account equity curve for the simulator page.

Presentation-only: replays the paper-trading ledger (fills) against cached
daily bars to rebuild the day-by-day equity series, then renders an ECharts
area chart.  Read-only — the ledger itself is never touched.  When the
account has no fills yet, the caller shows a caption instead.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from html import escape
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

_LINE = "#48647b"
_AREA = "rgba(72,100,123,.14)"
_TEXT = "#6f675b"
_GRID = "rgba(72,100,123,.10)"

_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def compute_equity_series(
    settings: Any, repository: Any, account_id: str
) -> tuple[list[tuple[str, float]], float]:
    """Replay the ledger day by day.  Returns ((day, equity) series, initial_capital)."""

    account = next(
        (a for a in repository.accounts(include_closed=True) if a["account_id"] == account_id),
        None,
    )
    if account is None:
        return [], 0.0
    initial = float(account.get("initial_capital") or 0.0)
    created = _parse_day(account.get("created_at")) or date.today()
    fills = sorted(
        repository.fills(account_id, limit=500),
        key=lambda f: (str(f.get("trade_date") or ""), str(f.get("created_at") or "")),
    )
    if not fills:
        return [], initial

    today = date.today()
    start = min(created, min((_parse_day(f.get("trade_date")) or today) for f in fills))

    symbols = sorted({str(f.get("symbol") or "") for f in fills if f.get("symbol")})
    close_by: dict[str, dict[date, float]] = {symbol: {} for symbol in symbols}
    if symbols:
        try:
            from quantlab.market.quotes import ResearchBarService

            service = ResearchBarService.from_settings(settings)
            bars = service.provider.bars(symbols, start - timedelta(days=10), today)
            for bar in bars:
                symbol = str(getattr(bar, "symbol", ""))
                day = getattr(bar, "date", None)
                if symbol in close_by and isinstance(day, date):
                    close_by[symbol][day] = float(bar.close)
        except Exception:
            pass  # 行情不可用时退化为按成交价估值

    fills_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for fill in fills:
        day = _parse_day(fill.get("trade_date"))
        if day is not None:
            fills_by_day[day].append(fill)

    cash = initial
    holdings: dict[str, float] = defaultdict(float)
    last_price: dict[str, float] = {}
    series: list[tuple[str, float]] = []

    day = start
    while day <= today:
        for fill in fills_by_day.get(day, []):
            symbol = str(fill.get("symbol") or "")
            quantity = float(fill.get("quantity") or 0)
            price = float(fill.get("fill_price") or 0)
            fees = float(fill.get("transaction_fees") or 0)
            if not symbol or quantity <= 0 or price <= 0:
                continue
            last_price[symbol] = price
            if str(fill.get("side") or "").lower() == "buy":
                cash -= quantity * price + fees
                holdings[symbol] += quantity
            else:
                cash += quantity * price - fees
                holdings[symbol] -= quantity
        for symbol, closes in close_by.items():
            if day in closes:
                last_price[symbol] = closes[day]
        market_value = sum(
            quantity * last_price.get(symbol, 0.0)
            for symbol, quantity in holdings.items()
            if quantity > 0
        )
        series.append((day.isoformat(), round(cash + market_value, 2)))
        day += timedelta(days=1)
    return series, initial


def _equity_html(series: list[tuple[str, float]], *, initial: float) -> str:
    days = [item[0] for item in series]
    values = [item[1] for item in series]
    option = {
        "backgroundColor": "transparent",
        "animation": False,
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "line", "lineStyle": {"color": "#8a8175"}},
            "backgroundColor": "rgba(255,253,248,.97)",
            "borderColor": "#c9c1b3",
            "textStyle": {"color": "#464b42", "fontSize": 12},
            "valueFormatter": None,
        },
        "grid": {"left": 12, "right": 76, "top": 18, "bottom": 26},
        "xAxis": {
            "type": "category",
            "data": days,
            "axisLine": {"lineStyle": {"color": "#c9c1b3"}},
            "axisLabel": {"color": _TEXT, "fontSize": 10},
            "axisTick": {"show": False},
        },
        "yAxis": {
            "scale": True,
            "position": "right",
            "axisLabel": {"color": _TEXT, "fontSize": 10},
            "splitLine": {"lineStyle": {"color": _GRID}},
            "axisLine": {"show": False},
        },
        "series": [
            {
                "name": "净值",
                "type": "line",
                "data": values,
                "smooth": True,
                "showSymbol": False,
                "lineStyle": {"width": 1.8, "color": _LINE},
                "areaStyle": {"color": _AREA},
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "lineStyle": {"color": "#8a8175", "type": "dashed", "width": 1},
                    "label": {"color": _TEXT, "fontSize": 10, "formatter": "初始资金"},
                    "data": [{"yAxis": round(initial, 2)}],
                },
            }
        ],
    }
    option_json = json.dumps(option, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin:0; padding:0; background:transparent; }}
  #chart {{ width:100%; height:270px; }}
  #fallback {{ display:none; padding:20px; color:{_TEXT}; font:13px/1.6 sans-serif; }}
</style>
<script src="{_ECHARTS_CDN}"
        onerror="document.getElementById('chart').style.display='none';document.getElementById('fallback').style.display='block';"></script>
</head><body>
<div id="chart"></div>
<div id="fallback">净值图加载失败（图表库需要网络加载），账户数据不受影响。</div>
<script>
  var option = {option_json};
  option.tooltip.formatter = function (params) {{
    var p = params[0];
    var ret = {initial!r} ? (p.value / {initial!r} - 1) * 100 : 0;
    var color = ret >= 0 ? '#c03a2b' : '#3e7c5f';
    return '<b>' + p.name + '</b><br>净值 <b>¥' + Number(p.value).toLocaleString('zh-CN', {{minimumFractionDigits: 2}})
      + '</b><br>累计收益 <b style="color:' + color + '">' + (ret >= 0 ? '+' : '') + ret.toFixed(2) + '%</b>';
  }};
  if (window.echarts) {{
    var chart = echarts.init(document.getElementById('chart'), null, {{renderer: 'canvas'}});
    chart.setOption(option);
    window.addEventListener('resize', function () {{ chart.resize(); }});
  }}
</script>
</body></html>"""


def render_account_equity_curve(settings: Any, repository: Any, account_id: str) -> None:
    """Render the equity-curve card for one paper account (fail-closed)."""

    try:
        series, initial = compute_equity_series(settings, repository, account_id)
    except Exception:
        series, initial = [], 0.0
    if len(series) < 2:
        st.caption("还没有成交记录；第一笔成交后，这里会画出账户净值曲线。")
        return
    latest = series[-1][1]
    ret = (latest / initial - 1) * 100 if initial else 0.0
    trend_class = "ql-chart-up" if ret >= 0 else "ql-chart-down"
    st.markdown(
        '<div class="ql-chart-card">'
        '<div class="ql-chart-head">'
        "<strong>账户净值曲线</strong>"
        f"<span>{escape(series[0][0])} ~ {escape(series[-1][0])} · "
        f'累计收益 <b class="{trend_class}">{ret:+.2f}%</b> · 按账本与日线回放</span>'
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    try:
        components.html(_equity_html(series, initial=initial), height=282, scrolling=False)
    except Exception:
        st.caption("净值图暂时无法加载；账户数据不受影响。")
    st.markdown(
        '<div class="ql-chart-card" style="margin-top:-.6rem;border-top:0;border-radius:0 0 var(--ql-radius-lg) var(--ql-radius-lg);">'
        '<div class="ql-chart-foot">虚线为初始资金 · 净值=现金+持仓按日收盘估值 · 图表仅用于浏览，账本以系统记录为准</div>'
        "</div>",
        unsafe_allow_html=True,
    )
