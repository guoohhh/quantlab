from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import streamlit as st

from dashboard import product_ui
from dashboard.ui_foundation import (
    cache_research_report,
    product_context,
    set_product_page,
)
from quantlab.config import Settings


if "navigation_probe_initialized" not in st.session_state:
    report = {
        "run_id": "run-navigation",
        "symbol": "sh510300",
        "as_of": "2026-07-17",
        "data": {
            "source": "probe",
            "bars": 120,
            "effective_as_of": "2026-07-17",
        },
        "decision": {"action": "watch", "confidence": 0.6, "target_weight": 0.1},
        "analysis_context_pack": {"quality_score": 0.8, "blocks": []},
    }
    cache_research_report(
        st.session_state,
        report,
        symbol="sh510300",
        requested_as_of=date(2026, 7, 19),
    )
    set_product_page(
        st.session_state,
        "研究台",
        symbol="sh510300",
    )
    st.session_state["product_research_symbol"] = "sh510300"
    st.session_state["product_research_date"] = date(2026, 7, 19)
    st.session_state["navigation_probe_initialized"] = True
original_chat = product_ui._render_chat
original_usage = product_ui.record_product_usage
try:
    product_ui._render_chat = Mock()
    product_ui.record_product_usage = Mock()
    product_ui.render_ai_research(
        Settings(
            values={"system": {"database_path": "navigation-probe.db"}},
            root=".",
        )
    )
finally:
    product_ui._render_chat = original_chat
    product_ui.record_product_usage = original_usage
context = product_context(st.session_state)
st.write(
    "context:"
    + "|".join(
        [
            context.symbol or "",
            context.research_requested_as_of or "",
            context.research_effective_as_of or "",
            context.research_run_id or "",
        ]
    )
)
