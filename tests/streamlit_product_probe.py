from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import streamlit as st

from dashboard import product_ui
from quantlab.config import Settings


state_path = Path(os.environ["QUANTLAB_UI_PROBE_FILE"])


def renderer(name: str):
    def render(_settings: Settings) -> None:
        counts = {}
        if state_path.exists():
            for line in state_path.read_text(encoding="utf-8").splitlines():
                page, value = line.split("=", 1)
                counts[page] = int(value)
        counts[name] = counts.get(name, 0) + 1
        state_path.write_text(
            "\n".join(f"{page}={count}" for page, count in sorted(counts.items())),
            encoding="utf-8",
        )
        st.write(f"rendered:{name}")

    return render


originals = {
    "record_product_usage": product_ui.record_product_usage,
    "render_home": product_ui.render_home,
    "render_market_and_discovery": product_ui.render_market_and_discovery,
    "render_ai_research": product_ui.render_ai_research,
    "render_simulator": product_ui.render_simulator,
    "render_review": product_ui.render_review,
    "render_mine": product_ui.render_mine,
    "render_help_center": product_ui.render_help_center,
}
try:
    product_ui.record_product_usage = Mock()
    product_ui.render_home = renderer("今日")
    product_ui.render_market_and_discovery = renderer("市场与发现")
    product_ui.render_ai_research = renderer("研究台")
    product_ui.render_simulator = renderer("组合与交易")
    product_ui.render_review = renderer("决策复盘")
    product_ui.render_mine = renderer("专业空间")
    product_ui.render_help_center = renderer("帮助中心")
    product_ui.render_product_app(Settings(values={}, root=state_path.parent))
finally:
    for name, value in originals.items():
        setattr(product_ui, name, value)
