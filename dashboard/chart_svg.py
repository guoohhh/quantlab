"""Market charts for the product UI (price candles + volume bars).

Primary: interactive ECharts (crosshair tooltip, dataZoom range slider,
MA lines) embedded via ``st.components.v1.html`` — 同花顺-style.
Fallback: static pure-SVG chart when the component cannot render, and a
plain caption when no data is available.  The page must never break
because of a chart.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from html import escape
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

# 中国市场约定：涨红、跌绿
_UP = "#c03a2b"
_DOWN = "#3e7c5f"
_AXIS = "#c9c1b3"
_GRID = "rgba(72,100,123,.10)"
_TEXT = "#6f675b"

_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"


def _fetch_daily_bars(
    settings: Any, symbol: str, *, end: date, count: int = 44
) -> tuple[list[Any], str]:
    """Return up to ``count`` daily bars ending at ``end`` and the provider name."""

    from quantlab.market.quotes import ResearchBarService

    service = ResearchBarService.from_settings(settings)
    bars = service.provider.bars([symbol], end - timedelta(days=max(140, count * 2)), end)
    eligible = [b for b in bars if b.symbol == symbol and b.date <= end]
    eligible.sort(key=lambda b: b.date)
    return eligible[-count:], str(getattr(service.provider, "name", "trusted-data"))


def _fmt_price(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _fmt_volume(value: float) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.0f}万"
    return f"{value:.0f}"


def _candles_svg(bars: list[Any], *, symbol: str) -> str:
    width, height = 760, 330
    pad_left, pad_right, pad_top, _pad_bottom = 10, 68, 16, 28
    price_h, vol_h, gap = 200, 54, 12
    plot_w = width - pad_left - pad_right

    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        hi = lo + 0.001
    span = hi - lo
    hi += span * 0.04
    lo -= span * 0.04

    def py(price: float) -> float:
        return pad_top + (hi - price) / (hi - lo) * price_h

    n = len(bars)
    slot = plot_w / n
    body_w = max(2.0, slot * 0.62)

    volumes = [float(getattr(b, "volume", 0.0) or 0.0) for b in bars]
    vmax = max(volumes) if volumes else 1.0
    vol_top = pad_top + price_h + gap

    def vh(volume: float) -> float:
        return (volume / vmax) * vol_h if vmax > 0 else 0.0

    parts: list[str] = []
    # 横向网格 + 右侧价格刻度
    for i in range(5):
        price = lo + (hi - lo) * i / 4
        y = py(price)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" y2="{y:.1f}" stroke="{_GRID}" stroke-width="1"/>'
            f'<text x="{pad_left + plot_w + 8}" y="{y + 3.5:.1f}" font-size="10" fill="{_TEXT}">{_fmt_price(price)}</text>'
        )
    # 成交量区基线
    parts.append(
        f'<line x1="{pad_left}" y1="{vol_top + vol_h}" x2="{pad_left + plot_w}" y2="{vol_top + vol_h}" stroke="{_AXIS}" stroke-width="1"/>'
    )

    for i, bar in enumerate(bars):
        x = pad_left + i * slot + slot / 2
        o, h, lo, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        color = _UP if c >= o else _DOWN
        parts.append(
            f'<line x1="{x:.1f}" y1="{py(h):.1f}" x2="{x:.1f}" y2="{py(lo):.1f}" stroke="{color}" stroke-width="1.1"/>'
        )
        top, bottom = py(max(o, c)), py(min(o, c))
        body_h = max(1.2, bottom - top)
        parts.append(
            f'<rect x="{x - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" height="{body_h:.1f}" fill="{color}" rx="0.5"/>'
        )
        volume = volumes[i]
        bar_h = vh(volume)
        parts.append(
            f'<rect x="{x - body_w / 2:.1f}" y="{vol_top + vol_h - bar_h:.1f}" width="{body_w:.1f}" height="{bar_h:.1f}" fill="{color}" opacity="0.45"/>'
        )

    # X 轴日期刻度（最多 5 个）
    step = max(1, n // 4)
    for i in range(0, n, step):
        x = pad_left + i * slot + slot / 2
        label = bars[i].date.strftime("%m-%d")
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8}" font-size="10" fill="{_TEXT}" text-anchor="middle">{label}</text>'
        )

    # 最新收盘参考线
    last_close = float(bars[-1].close)
    last_y = py(last_close)
    last_color = _UP if float(bars[-1].close) >= float(bars[-1].open) else _DOWN
    parts.append(
        f'<line x1="{pad_left}" y1="{last_y:.1f}" x2="{pad_left + plot_w}" y2="{last_y:.1f}" stroke="{last_color}" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>'
        f'<rect x="{pad_left + plot_w + 2}" y="{last_y - 8:.1f}" width="52" height="15" rx="3" fill="{last_color}"/>'
        f'<text x="{pad_left + plot_w + 28}" y="{last_y + 3.5:.1f}" font-size="10" fill="#fdf6ea" text-anchor="middle">{_fmt_price(last_close)}</text>'
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="ql-chart-svg" role="img" '
        f'aria-label="{escape(symbol)} 日线与成交量图">'
        + "".join(parts)
        + "</svg>"
    )


def _moving_average(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        if i < window - 1:
            out.append(None)
        else:
            out.append(round(sum(closes[i - window + 1 : i + 1]) / window, 4))
    return out


def _echarts_html(bars: list[Any], *, symbol: str) -> str:
    """Build the interactive candle/volume page (ECharts in an iframe)."""

    dates = [b.date.strftime("%Y-%m-%d") for b in bars]
    kdata = [
        [round(float(b.open), 4), round(float(b.close), 4), round(float(b.low), 4), round(float(b.high), 4)]
        for b in bars
    ]
    closes = [float(b.close) for b in bars]
    vols = [
        {
            "value": round(float(getattr(b, "volume", 0.0) or 0.0), 0),
            "itemStyle": {"color": _UP if float(b.close) >= float(b.open) else _DOWN, "opacity": 0.5},
        }
        for b in bars
    ]
    ma5 = _moving_average(closes, 5)
    ma10 = _moving_average(closes, 10)
    ma20 = _moving_average(closes, 20)
    # 默认视窗：最近约 60 个交易日
    zoom_start = max(0.0, 100 - 60 / max(1, len(bars)) * 100)

    option = {
        "backgroundColor": "transparent",
        "animation": False,
        "axisPointer": {"link": [{"xAxisIndex": "all"}], "label": {"backgroundColor": "#48647b"}},
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross", "lineStyle": {"color": "#8a8175"}},
            "backgroundColor": "rgba(255,253,248,.97)",
            "borderColor": "#c9c1b3",
            "textStyle": {"color": "#464b42", "fontSize": 12},
        },
        "grid": [
            {"left": 12, "right": 64, "top": 24, "height": 205},
            {"left": 12, "right": 64, "top": 262, "height": 58},
        ],
        "xAxis": [
            {
                "type": "category",
                "data": dates,
                "gridIndex": 0,
                "axisLine": {"lineStyle": {"color": _AXIS}},
                "axisLabel": {"color": _TEXT, "fontSize": 10},
                "axisTick": {"show": False},
                "splitLine": {"show": False},
            },
            {
                "type": "category",
                "data": dates,
                "gridIndex": 1,
                "axisLine": {"lineStyle": {"color": _AXIS}},
                "axisLabel": {"show": False},
                "axisTick": {"show": False},
                "splitLine": {"show": False},
            },
        ],
        "yAxis": [
            {
                "scale": True,
                "gridIndex": 0,
                "position": "right",
                "axisLabel": {"color": _TEXT, "fontSize": 10},
                "splitLine": {"lineStyle": {"color": _GRID}},
                "axisLine": {"show": False},
            },
            {
                "gridIndex": 1,
                "position": "right",
                "splitNumber": 2,
                "axisLabel": {"color": _TEXT, "fontSize": 9},
                "splitLine": {"show": False},
                "axisLine": {"show": False},
            },
        ],
        "dataZoom": [
            {"type": "inside", "xAxisIndex": [0, 1], "start": zoom_start, "end": 100},
            {
                "type": "slider",
                "xAxisIndex": [0, 1],
                "top": 338,
                "height": 22,
                "start": zoom_start,
                "end": 100,
                "borderColor": _AXIS,
                "backgroundColor": "rgba(244,239,228,.4)",
                "fillerColor": "rgba(192,58,43,.10)",
                "handleStyle": {"color": _UP},
                "moveHandleStyle": {"color": _UP},
                "textStyle": {"color": _TEXT, "fontSize": 9},
                "dataBackground": {
                    "lineStyle": {"color": _AXIS},
                    "areaStyle": {"color": "rgba(72,100,123,.10)"},
                },
            },
        ],
        "series": [
            {
                "name": "K线",
                "type": "candlestick",
                "data": kdata,
                "itemStyle": {
                    "color": _UP,
                    "color0": _DOWN,
                    "borderColor": _UP,
                    "borderColor0": _DOWN,
                },
            },
            {
                "name": "MA5",
                "type": "line",
                "data": ma5,
                "smooth": True,
                "showSymbol": False,
                "lineStyle": {"width": 1.1, "color": "#c8a25e"},
            },
            {
                "name": "MA10",
                "type": "line",
                "data": ma10,
                "smooth": True,
                "showSymbol": False,
                "lineStyle": {"width": 1.1, "color": "#48647b"},
            },
            {
                "name": "MA20",
                "type": "line",
                "data": ma20,
                "smooth": True,
                "showSymbol": False,
                "lineStyle": {"width": 1.1, "color": "#81513f"},
            },
            {
                "name": "成交量",
                "type": "bar",
                "xAxisIndex": 1,
                "yAxisIndex": 1,
                "data": vols,
            },
        ],
    }
    option_json = json.dumps(option, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html, body {{ margin:0; padding:0; background:transparent; }}
  #chart {{ width:100%; height:380px; }}
  #fallback {{ display:none; padding:24px; color:{_TEXT}; font:13px/1.6 sans-serif; }}
</style>
<script src="{_ECHARTS_CDN}"
        onerror="document.getElementById('chart').style.display='none';document.getElementById('fallback').style.display='block';"></script>
</head><body>
<div id="chart"></div>
<div id="fallback">交互行情图加载失败（图表库需要网络加载），标的数据不受影响。</div>
<script>
  var kdata = {json.dumps(kdata)};
  var option = {option_json};
  option.tooltip.formatter = function (params) {{
    var k = null, v = null;
    for (var i = 0; i < params.length; i++) {{
      if (params[i].seriesName === 'K线') k = params[i];
      if (params[i].seriesName === '成交量') v = params[i];
    }}
    if (!k) return '';
    var d = k.data, idx = k.dataIndex;
    var o = d[1], c = d[2], l = d[3], h = d[4];
    var prev = idx > 0 ? kdata[idx - 1][1] : o;
    var chg = prev ? (c / prev - 1) * 100 : 0;
    var color = c >= o ? '{_UP}' : '{_DOWN}';
    function fv(x) {{ return x >= 1e8 ? (x / 1e8).toFixed(2) + '亿' : (x / 1e4).toFixed(0) + '万'; }}
    var html = '<div style="font-weight:700;margin-bottom:4px">' + k.name + '</div>'
      + '开 <b style="color:' + (o >= prev ? '{_UP}' : '{_DOWN}') + '">' + o.toFixed(3) + '</b>&emsp;'
      + '高 <b style="color:{_UP}">' + h.toFixed(3) + '</b><br>'
      + '收 <b style="color:' + color + '">' + c.toFixed(3) + '</b>&emsp;'
      + '低 <b style="color:{_DOWN}">' + l.toFixed(3) + '</b><br>'
      + '涨跌 <b style="color:' + color + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</b>';
    if (v) html += '&emsp;量 <b>' + fv(v.value) + '</b>';
    return html;
  }};
  if (window.echarts) {{
    var chart = echarts.init(document.getElementById('chart'), null, {{renderer: 'canvas'}});
    chart.setOption(option);
    window.addEventListener('resize', function () {{ chart.resize(); }});
  }}
</script>
</body></html>"""


def render_symbol_market_chart(
    settings: Any, symbol: str, *, end: date | None = None
) -> None:
    """Render the interactive candle/volume card (fail-closed: SVG → caption)."""

    end = end or date.today()
    try:
        bars, provider_name = _fetch_daily_bars(settings, symbol, end=end, count=250)
    except Exception:
        bars, provider_name = [], ""
    if len(bars) < 5:
        st.caption("行情图暂时没有足够的数据可画；研究内容与证据不受影响。")
        return

    first, last = bars[0].date, bars[-1].date
    change = (float(bars[-1].close) / float(bars[0].close) - 1) * 100
    trend_class = "ql-chart-up" if change >= 0 else "ql-chart-down"
    st.markdown(
        '<div class="ql-chart-card">'
        '<div class="ql-chart-head">'
        f"<strong>{escape(symbol)} · 日线与成交量</strong>"
        f"<span>{first.isoformat()} ~ {last.isoformat()} · {len(bars)} 个交易日 · "
        f'区间 <b class="{trend_class}">{change:+.1f}%</b> · 来源 {escape(provider_name)}</span>'
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    try:
        components.html(_echarts_html(bars, symbol=symbol), height=392, scrolling=False)
    except Exception:
        st.markdown(_candles_svg(bars[-60:], symbol=symbol), unsafe_allow_html=True)
    st.markdown(
        '<div class="ql-chart-card" style="margin-top:-.6rem;border-top:0;border-radius:0 0 var(--ql-radius-lg) var(--ql-radius-lg);">'
        '<div class="ql-chart-foot">十字光标看每日开高低收 · 底部滑块拖选区间 · 滚轮缩放 · MA5/10/20 · 图表仅用于浏览，研究与订单只使用冻结证据</div>'
        "</div>",
        unsafe_allow_html=True,
    )
