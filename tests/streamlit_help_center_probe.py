from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from dashboard import product_ui
from quantlab.config import Settings


probe = Path(os.environ["QUANTLAB_HELP_PROBE_FILE"])


def _record(page: str) -> None:
    probe.parent.mkdir(parents=True, exist_ok=True)
    with probe.open("a", encoding="utf-8") as output:
        output.write(f"{page}\n")


def _go_to(page: str, **_kwargs: object) -> None:
    st.session_state["help_probe_route"] = page
    _record(page)


original_go_to = product_ui._go_to
try:
    product_ui._go_to = _go_to
    product_ui.render_help_center(
        Settings(
            values={"system": {"database_path": "help-center-probe.db"}},
            root=probe.parent,
        )
    )
finally:
    product_ui._go_to = original_go_to
