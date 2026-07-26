from __future__ import annotations

import streamlit as st

from dashboard.ui_foundation import (
    PRODUCT_ATTENTION_COUNT_KEY,
    PRODUCT_NAVIGATION_COMPACT_KEY,
    apply_product_theme,
    render_product_navigation,
)


st.set_page_config(layout="wide")
st.session_state[PRODUCT_NAVIGATION_COMPACT_KEY] = True
st.session_state[PRODUCT_ATTENTION_COUNT_KEY] = 2
apply_product_theme()
page = render_product_navigation()
st.markdown(f"<h1>当前页面：{page}</h1>", unsafe_allow_html=True)
st.caption("紧凑导航视觉验收探针")
