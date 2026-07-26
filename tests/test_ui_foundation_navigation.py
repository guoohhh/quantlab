from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from dashboard import ui_foundation
from dashboard.ui_foundation import (
    PRODUCT_NAVIGATION_COMPACT_KEY,
    PRODUCT_MOBILE_NAVIGATION_OPEN_KEY,
    PRODUCT_PAGE_ICONS,
    PRODUCT_PAGES,
    PRODUCT_NAVIGATION_KEY,
    PRODUCT_PAGE_KEY,
    PRODUCT_PAGE_TARGET_KEY,
    product_navigation_is_compact,
    set_product_navigation_compact,
    set_product_page,
)


def test_navigation_density_is_explicit_session_state_and_icons_cover_every_page():
    state: dict[str, object] = {}

    assert product_navigation_is_compact(state) is False
    set_product_navigation_compact(state, True)
    assert state[PRODUCT_NAVIGATION_COMPACT_KEY] is True
    assert product_navigation_is_compact(state) is True
    set_product_navigation_compact(state, False)
    assert product_navigation_is_compact(state) is False

    assert set(PRODUCT_PAGE_ICONS) == set(PRODUCT_PAGES)
    assert all(icon.startswith(":material/") and icon.endswith(":") for icon in PRODUCT_PAGE_ICONS.values())


def test_product_theme_replaces_native_collapse_controls_and_hides_heading_anchors():
    source = Path(ui_foundation.__file__).read_text(encoding="utf-8")

    assert 'data-testid="stSidebarCollapseButton"' in source
    assert 'data-testid="stExpandSidebarButton"' in source
    assert 'data-testid="stSidebarCollapsedControl"' in source
    assert 'data-testid="stHeading"] a[href^="#"]' in source
    assert 'data-testid="stHeaderActionElements"' in source
    assert 'data-testid="stToolbar"' in source
    assert "ql-navigation-mode-compact" in source
    assert "letter-spacing:-" not in source


def test_mobile_navigation_uses_an_explicit_drawer_state_without_hiding_routes():
    source = Path(ui_foundation.__file__).read_text(encoding="utf-8")
    state: dict[str, object] = {}

    assert PRODUCT_MOBILE_NAVIGATION_OPEN_KEY not in state
    assert "ql-mobile-navigation-open" in source
    assert "product_mobile_navigation_toggle" in source
    assert "product_mobile_navigation_close" in source
    assert ":not(:has(.ql-mobile-navigation-open))" in source
    assert '[data-testid="stHeader"] * { pointer-events:none !important; }' in source
    assert '.st-key-product_mobile_navigation_toggle [data-testid="stMarkdownContainer"] {' in source
    assert "clip:rect(0,0,0,0) !important;" in source


def test_global_assistant_only_floats_after_the_user_opens_it():
    source = Path(ui_foundation.__file__).read_text(encoding="utf-8")

    assert ".st-key-global_ai_assistant { position:fixed;" in source
    assert ".st-key-global_ai_assistant:has(.st-key-open_global_ai_assistant) {" in source
    assert "@media (max-width: 1050px)" in source
    assert ".st-key-global_ai_assistant { position:static; width:auto; max-height:none;" in source


def test_programmatic_navigation_queues_the_widget_value_for_the_next_rerun():
    state: dict[str, object] = {PRODUCT_NAVIGATION_KEY: "今日"}

    set_product_page(state, "专家圆桌")

    assert state[PRODUCT_PAGE_KEY] == "专家圆桌"
    assert state[PRODUCT_PAGE_TARGET_KEY] == "专家圆桌"
    assert state[PRODUCT_NAVIGATION_KEY] == "今日"


def test_compact_navigation_keeps_every_page_reachable(tmp_path, monkeypatch):
    probe = tmp_path / "compact-navigation-renders.txt"
    monkeypatch.setenv("QUANTLAB_UI_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_product_probe.py")
    app.session_state[PRODUCT_NAVIGATION_COMPACT_KEY] = True
    app.run(timeout=30)

    assert not app.exception
    buttons = {button.key: button for button in app.button if button.key}
    compact_page_buttons = {
        key: button
        for key, button in buttons.items()
        if key.startswith("product_compact_navigation_page_")
    }
    assert len(compact_page_buttons) == len(PRODUCT_PAGES)
    assert all(button.help.startswith("前往") for button in compact_page_buttons.values())

    compact_page_buttons["product_compact_navigation_page_2"].click().run(timeout=30)
    assert not app.exception
    assert probe.read_text(encoding="utf-8").splitlines() == ["今日=1", "研究台=1"]


def test_support_workspaces_stay_reachable_without_expanding_primary_navigation(
    tmp_path, monkeypatch
):
    probe = tmp_path / "utility-navigation-renders.txt"
    monkeypatch.setenv("QUANTLAB_UI_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_product_probe.py").run(timeout=30)

    assert not app.exception
    assert tuple(PRODUCT_PAGES) == ("今日", "市场与发现", "研究台", "组合与交易", "决策复盘")
    buttons = {button.key: button for button in app.button if button.key}
    buttons["open_product_help_center"].click().run(timeout=30)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "帮助中心"
    assert app.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] is False
    assert probe.read_text(encoding="utf-8").splitlines() == ["今日=1", "帮助中心=1"]


def test_mobile_navigation_closes_after_selecting_a_destination(tmp_path, monkeypatch):
    probe = tmp_path / "mobile-navigation-renders.txt"
    monkeypatch.setenv("QUANTLAB_UI_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_product_probe.py")
    app.run(timeout=30)

    buttons = {button.key: button for button in app.button if button.key}
    buttons["product_mobile_navigation_toggle"].click().run(timeout=30)
    assert app.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] is True

    app.radio[0].set_value("研究台").run(timeout=30)

    assert not app.exception
    assert app.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] is False
    assert app.session_state[PRODUCT_PAGE_KEY] == "研究台"
