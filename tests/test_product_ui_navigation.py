from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import product_ui
from dashboard.ui_foundation import (
    PRODUCT_NAVIGATION_KEY,
    PRODUCT_PAGE_KEY,
    cache_research_report,
    cached_research_report,
    consume_product_context,
    context_matches_research,
    current_product_page,
    product_context,
    research_identity,
    set_product_page,
    update_product_selection,
)


def _research_probe(
    tmp_path: Path,
    monkeypatch,
    *,
    cached_detail: bool = False,
) -> tuple[AppTest, Path]:
    probe = tmp_path / "research-route-events.txt"
    monkeypatch.setenv("QUANTLAB_RESEARCH_PROBE_FILE", str(probe))
    if cached_detail:
        monkeypatch.setenv("QUANTLAB_RESEARCH_PROBE_START_ROUTE", "cached_detail")
    else:
        monkeypatch.delenv("QUANTLAB_RESEARCH_PROBE_START_ROUTE", raising=False)
    return AppTest.from_file("tests/streamlit_research_identity_probe.py").run(timeout=30), probe


def _help_probe(tmp_path: Path, monkeypatch) -> tuple[AppTest, Path]:
    probe = tmp_path / "help-route-events.txt"
    monkeypatch.setenv("QUANTLAB_HELP_PROBE_FILE", str(probe))
    return AppTest.from_file("tests/streamlit_help_center_probe.py").run(timeout=30), probe


def _button_by_key(app: AppTest, key: str):
    return next(button for button in app.button if button.key == key)


def _input_by_key(app: AppTest, key: str):
    return next(input_ for input_ in app.text_input if input_.key == key)


def _select_by_key(app: AppTest, key: str):
    return next(select for select in app.selectbox if select.key == key)


def _probe_events(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_product_page_state_is_validated_and_context_survives_navigation():
    state: dict[str, object] = {}

    assert current_product_page(state) == "今日"
    set_product_page(
        state,
        "组合与交易",
        symbol="sh600519",
        research_run_id="run-1",
        research_requested_as_of="2026-07-19",
        research_effective_as_of="2026-07-17",
        account_id="account-1",
        order_id="order-1",
    )

    assert current_product_page(state) == "组合与交易"
    context = product_context(state)
    assert context.symbol == "sh600519"
    assert context.research_run_id == "run-1"
    assert context.research_requested_as_of == "2026-07-19"
    assert context.research_effective_as_of == "2026-07-17"
    assert context.account_id == "account-1"
    assert context.order_id == "order-1"
    with pytest.raises(ValueError, match="unknown product page"):
        set_product_page(state, "不存在")


def test_navigation_context_is_consumed_once_and_user_selection_invalidates_research():
    state: dict[str, object] = {}
    set_product_page(
        state,
        "研究台",
        symbol="sh510300",
        research_run_id="run-a",
        research_requested_as_of="2026-07-19",
        research_effective_as_of="2026-07-17",
    )

    first = consume_product_context(state, "ai_research")
    assert first is not None
    assert first.research_run_id == "run-a"
    assert consume_product_context(state, "ai_research") is None

    assert (
        update_product_selection(
            state,
            symbol="sh600519",
            requested_as_of="2026-07-19",
        )
        is True
    )
    changed = product_context(state)
    assert changed.symbol == "sh600519"
    assert changed.research_run_id is None
    assert changed.research_requested_as_of is None
    assert changed.research_effective_as_of is None


def test_research_date_change_invalidates_bound_run_without_deleting_cache():
    state: dict[str, object] = {}
    report = {
        "run_id": "run-date",
        "symbol": "sh510300",
        "as_of": "2026-07-17",
        "data": {"effective_as_of": "2026-07-17"},
    }
    identity = cache_research_report(
        state,
        report,
        symbol="sh510300",
        requested_as_of="2026-07-19",
    )
    set_product_page(
        state,
        "研究台",
        symbol=identity.symbol,
        research_run_id=identity.run_id,
        research_requested_as_of=identity.requested_as_of,
        research_effective_as_of=identity.effective_as_of,
    )

    assert (
        update_product_selection(
            state,
            symbol="sh510300",
            requested_as_of="2026-07-18",
        )
        is True
    )
    assert product_context(state).research_run_id is None
    assert (
        cached_research_report(
            state,
            symbol="sh510300",
            requested_as_of="2026-07-19",
        )
        is not None
    )


def test_research_cache_requires_symbol_requested_effective_and_run_identity():
    state: dict[str, object] = {}
    report = {
        "run_id": "run-1",
        "symbol": "sh600519",
        "as_of": "2026-07-17",
        "data": {"effective_as_of": "2026-07-17"},
    }
    identity = cache_research_report(
        state,
        report,
        symbol="sh600519",
        requested_as_of="2026-07-19",
    )

    assert identity.requested_as_of == "2026-07-19"
    assert (
        cached_research_report(state, symbol="sh600519", requested_as_of="2026-07-19") is not None
    )
    assert cached_research_report(state, symbol="sh600000", requested_as_of="2026-07-19") is None
    assert cached_research_report(state, symbol="sh600519", requested_as_of="2026-07-18") is None
    stored = cached_research_report(state, symbol="sh600519", requested_as_of="2026-07-19")
    parsed = research_identity(stored)
    assert parsed == identity

    set_product_page(
        state,
        "组合与交易",
        symbol=identity.symbol,
        research_run_id=identity.run_id,
        research_requested_as_of=identity.requested_as_of,
        research_effective_as_of=identity.effective_as_of,
    )
    assert context_matches_research(product_context(state), stored) is True
    state["product_context_symbol"] = "sh600000"
    assert context_matches_research(product_context(state), stored) is False


def test_product_app_dispatches_only_the_selected_renderer(monkeypatch):
    state = {PRODUCT_PAGE_KEY: "研究台", PRODUCT_NAVIGATION_KEY: "研究台"}
    renderers = {
        "今日": Mock(),
        "市场与发现": Mock(),
        "研究台": Mock(),
        "组合与交易": Mock(),
        "决策复盘": Mock(),
        "专业空间": Mock(),
        "帮助中心": Mock(),
    }
    monkeypatch.setattr(product_ui.st, "session_state", state)
    monkeypatch.setattr(product_ui, "apply_product_theme", Mock())
    monkeypatch.setattr(product_ui, "render_product_navigation", lambda: "研究台")
    monkeypatch.setattr(product_ui, "_render_workspace_header", Mock())
    monkeypatch.setattr(product_ui, "record_product_usage", Mock())
    monkeypatch.setattr(product_ui, "render_home", renderers["今日"])
    monkeypatch.setattr(product_ui, "render_market_and_discovery", renderers["市场与发现"])
    monkeypatch.setattr(product_ui, "render_ai_research", renderers["研究台"])
    monkeypatch.setattr(product_ui, "render_simulator", renderers["组合与交易"])
    monkeypatch.setattr(product_ui, "render_review", renderers["决策复盘"])
    monkeypatch.setattr(product_ui, "render_mine", renderers["专业空间"])
    monkeypatch.setattr(product_ui, "render_help_center", renderers["帮助中心"])

    product_ui.render_product_app(Mock())

    renderers["研究台"].assert_called_once()
    for page in renderers:
        if page != "研究台":
            renderers[page].assert_not_called()


def test_streamlit_navigation_rerun_executes_only_the_current_page(tmp_path, monkeypatch):
    probe = tmp_path / "render-counts.txt"
    monkeypatch.setenv("QUANTLAB_UI_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_product_probe.py").run(timeout=30)

    assert not app.exception
    assert probe.read_text(encoding="utf-8") == "今日=1"
    app.radio[0].set_value("研究台").run(timeout=30)

    assert not app.exception
    assert probe.read_text(encoding="utf-8").splitlines() == ["今日=1", "研究台=1"]


def test_help_center_opens_concrete_documents_and_routes_to_product_workspaces(
    tmp_path,
    monkeypatch,
):
    app, probe = _help_probe(tmp_path, monkeypatch)

    assert not app.exception
    document_buttons = {
        button.key: button for button in app.button if button.key and button.key.startswith("help_open_document_")
    }
    assert set(document_buttons) == {
        "help_open_document_quick_start",
        "help_open_document_research_roundtable",
        "help_open_document_simulation",
        "help_open_document_review_notifications",
        "help_open_document_assistant_settings",
        "help_open_document_status_faq",
    }
    assert document_buttons["help_open_document_quick_start"].disabled is True

    document_buttons["help_open_document_research_roundtable"].click().run(timeout=30)
    assert not app.exception
    assert app.session_state["product_help_document"] == "research_roundtable"
    assert any(button.key == "help_open_research" for button in app.button)

    _button_by_key(app, "help_open_research").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["help_probe_route"] == "研究台"
    assert probe.read_text(encoding="utf-8").splitlines() == ["研究台"]

    _button_by_key(app, "help_open_document_simulation").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["product_help_document"] == "simulation"
    assert any(button.key == "help_open_simulator" for button in app.button)


def test_research_hub_lists_filters_and_paginates_lightweight_indexes(tmp_path, monkeypatch):
    app, probe = _research_probe(tmp_path, monkeypatch)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "研究台"
    assert any("活跃研究报告 <em>9 份</em>" in item.value for item in app.markdown)
    assert _probe_events(probe) == ["index|page=1|query=|action=|stage="]

    _button_by_key(app, "research_index_next_page").click().run(timeout=30)
    assert not app.exception
    assert "index|page=2|query=|action=|stage=" in _probe_events(probe)

    _input_by_key(app, "product_research_index_query").set_value("seed-1").run(timeout=30)
    assert not app.exception
    assert "index|page=1|query=seed-1|action=|stage=" in _probe_events(probe)

    _select_by_key(app, "product_research_index_action").set_value("buy").run(timeout=30)
    assert not app.exception
    assert "index|page=1|query=seed-1|action=buy|stage=" in _probe_events(probe)
    assert not any(event.startswith(("analysis|", "detail-get|")) for event in _probe_events(probe))


def test_research_generation_routes_to_detail_and_cache_never_reruns_ai(tmp_path, monkeypatch):
    app, probe = _research_probe(tmp_path, monkeypatch)

    _button_by_key(app, "product_run_research_from_hub").click().run(timeout=30)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "研究详情"
    assert any(item.value == "route:研究详情" for item in app.caption)
    assert {item.label for item in app.metric} >= {
        "研究结论",
        "置信度",
        "建议仓位上限",
        "证据质量",
    }
    captions = [item.value for item in app.caption]
    rendered = captions + [item.value for item in app.markdown]
    assert any("没有生成足够的可展示支持证据" in item for item in captions)
    assert any("没有生成可展示的反对证据" in item for item in captions)
    assert "[]" not in rendered
    assert [event for event in _probe_events(probe) if event.startswith("analysis|")] == [
        "analysis|sh510300|2026-07-23"
    ]


def test_cached_research_detail_rerun_never_restarts_research(tmp_path, monkeypatch):
    app, probe = _research_probe(tmp_path, monkeypatch, cached_detail=True)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "研究详情"
    assert not any(event.startswith(("analysis|", "detail-get|")) for event in _probe_events(probe))

    app.run(timeout=30)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "研究详情"
    assert not any(event.startswith(("analysis|", "detail-get|")) for event in _probe_events(probe))


def test_blank_research_ticker_disables_generation_without_ai_call(tmp_path, monkeypatch):
    app, probe = _research_probe(tmp_path, monkeypatch)

    _input_by_key(app, "product_research_symbol_new").set_value("").run(timeout=30)

    assert not app.exception
    assert _button_by_key(app, "product_run_research_from_hub").disabled is True
    assert not any(event.startswith("analysis|") for event in _probe_events(probe))


def test_research_labels_translate_actions_and_evidence_stages():
    assert product_ui._research_action_label("watch") == "持续观察"
    assert product_ui._research_action_label("avoid") == "暂不参与"
    assert product_ui._research_stage_label("research_only") == "探索研究，不计正式成绩"
    assert product_ui._thesis_status_label("damaged") == "出现损伤，优先复核"
    assert product_ui._research_job_label("chat_request") == "研究追问"


def test_roundtable_source_revalidates_the_current_report_identity(monkeypatch):
    identity = SimpleNamespace(
        run_id="run-current",
        symbol="sh600519",
        requested_as_of="2026-07-21",
        effective_as_of="2026-07-21",
    )

    class Repository:
        def __init__(self, _path):
            pass

        def get(self, run_id):
            assert run_id == "run-current"
            return {"run_id": run_id}

    monkeypatch.setattr(product_ui, "DecisionRepository", Repository)
    monkeypatch.setattr(
        product_ui,
        "validate_research_record",
        lambda record, *, run_id, symbol: {
            "run_id": run_id,
            "symbol": symbol,
            "requested_as_of": date(2026, 7, 21),
            "effective_as_of": date(2026, 7, 21),
        },
    )

    validated = product_ui._validated_roundtable_source("research.db", identity)

    assert validated["run_id"] == "run-current"
    assert validated["symbol"] == "sh600519"


def test_research_hub_roundtable_review_routes_to_standalone_workspace(tmp_path, monkeypatch):
    app, probe = _research_probe(tmp_path, monkeypatch)

    _button_by_key(app, "research_workbench_roundtable_seed-9").click().run(timeout=30)

    assert not app.exception
    assert app.session_state[PRODUCT_PAGE_KEY] == "专家圆桌"
    assert any(item.value == "route:专家圆桌" for item in app.caption)
    assert any(expander.label == "发起新的圆桌讨论" for expander in app.expander)
    assert not any(event.startswith("analysis|") for event in _probe_events(probe))
    assert "detail-get|seed-9" in _probe_events(probe)


def test_pretrade_card_and_chat_conversation_scope_include_research_identity():
    check = {
        "account_id": "account-1",
        "symbol": "sh510300",
        "side": "buy",
        "requested_quantity": 100,
        "research_run_id": "run-a",
        "research_link_status": "linked",
    }
    assert product_ui._pretrade_request_matches(
        check,
        account_id="account-1",
        symbol="sh510300",
        side="buy",
        quantity=100,
        research_run_id="run-a",
        research_link_status="linked",
    )
    assert not product_ui._pretrade_request_matches(
        check,
        account_id="account-1",
        symbol="sh510300",
        side="buy",
        quantity=100,
        research_run_id="run-b",
        research_link_status="linked",
    )
    assert not product_ui._pretrade_request_matches(
        {**check, "research_link_status": "unavailable"},
        account_id="account-1",
        symbol="sh510300",
        side="buy",
        quantity=100,
        research_run_id="run-a",
        research_link_status="linked",
    )

    conversations = [
        {"conversation_id": "a", "symbol": "sh510300", "research_run_id": "run-a"},
        {"conversation_id": "b", "symbol": "sh600519", "research_run_id": "run-b"},
        {"conversation_id": "u", "symbol": "sh510300", "research_run_id": None},
    ]
    assert [
        item["conversation_id"]
        for item in product_ui._chat_conversations_for_context(
            conversations,
            symbol="sh510300",
            research_run_id="run-a",
        )
    ] == ["a"]
    assert [
        item["conversation_id"]
        for item in product_ui._chat_conversations_for_context(
            conversations,
            symbol="sh510300",
            research_run_id=None,
        )
    ] == ["u"]


def test_notification_targets_preserve_user_confirmation_and_research_identity(monkeypatch):
    order_target = product_ui._notification_target(
        Mock(),
        {
            "action_type": "view_order",
            "symbol": "sh600519",
            "account_id": "account-1",
            "order_id": "order-1",
            "action_payload": {},
        },
    )
    assert order_target["page"] == "组合与交易"
    assert order_target["context"] == {
        "symbol": "sh600519",
        "account_id": "account-1",
        "order_id": "order-1",
    }
    assert "单独确认" in order_target["message"]

    class Repository:
        def __init__(self, _path):
            pass

        def get(self, run_id):
            assert run_id == "run-1"
            return {"run_id": run_id}

    monkeypatch.setattr(product_ui, "DecisionRepository", Repository)
    monkeypatch.setattr(
        product_ui,
        "validate_research_record",
        lambda record, *, run_id, symbol: {
            "run_id": run_id,
            "symbol": symbol,
            "requested_as_of": date(2026, 7, 21),
            "effective_as_of": date(2026, 7, 21),
        },
    )
    settings = Mock()
    settings.get.return_value = "data/quantlab.db"
    settings.resolve.return_value = "data/quantlab.db"
    research_target = product_ui._notification_target(
        settings,
        {
            "action_type": "view_research",
            "symbol": "sh600519",
            "research_run_id": "run-1",
            "action_payload": {},
        },
    )
    assert research_target["page"] == "研究台"
    assert research_target["context"] == {
        "symbol": "sh600519",
        "research_run_id": "run-1",
        "research_requested_as_of": "2026-07-21",
        "research_effective_as_of": "2026-07-21",
    }


def test_mark_visible_notifications_read_never_updates_hidden_rows():
    class Notifications:
        def __init__(self):
            self.marked: list[str] = []

        def mark_read(self, notification_id: str) -> bool:
            self.marked.append(notification_id)
            return True

    notifications = Notifications()
    marked = product_ui._mark_visible_notifications_read(
        notifications,
        [
            {"notification_id": "visible-unread", "read": False},
            {"notification_id": "visible-read", "read": True},
        ],
    )
    assert marked == 1
    assert notifications.marked == ["visible-unread"]


def test_simulator_passes_matching_run_and_rerun_never_auto_submits(tmp_path, monkeypatch):
    probe = tmp_path / "simulator-calls.txt"
    monkeypatch.setenv("QUANTLAB_SIMULATOR_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_simulator_identity_probe.py").run(timeout=30)

    assert not app.exception
    assert not probe.exists()
    assert any("run-simulator" in item.value for item in app.info)

    next(button for button in app.button if button.label == "运行 AI 交易前检查").click().run(
        timeout=30
    )
    assert not app.exception
    assert probe.read_text(encoding="utf-8").splitlines() == [
        "PRETRADE|account-1|sh510300|buy|100|run-simulator"
    ]
    assert "确认创建盘中模拟委托" in [button.label for button in app.button]

    app.run(timeout=30)
    assert probe.read_text(encoding="utf-8").splitlines() == [
        "PRETRADE|account-1|sh510300|buy|100|run-simulator"
    ]

    next(button for button in app.button if button.label == "确认创建盘中模拟委托").click().run(
        timeout=30
    )
    assert not app.exception
    assert probe.read_text(encoding="utf-8").splitlines() == [
        "PRETRADE|account-1|sh510300|buy|100|run-simulator",
        "SUBMIT|check-1|100|sh510300|intraday_simulation",
    ]
    app.run(timeout=30)
    assert probe.read_text(encoding="utf-8").splitlines() == [
        "PRETRADE|account-1|sh510300|buy|100|run-simulator",
        "SUBMIT|check-1|100|sh510300|intraday_simulation",
    ]


def test_simulator_requires_explicit_close_reference_acknowledgement_for_next_open(tmp_path, monkeypatch):
    probe = tmp_path / "simulator-next-open-calls.txt"
    monkeypatch.setenv("QUANTLAB_SIMULATOR_PROBE_FILE", str(probe))
    monkeypatch.setenv("QUANTLAB_SIMULATOR_PROBE_QUOTE_MODE", "next_open")
    app = AppTest.from_file("tests/streamlit_simulator_identity_probe.py").run(timeout=30)

    next(button for button in app.button if button.label == "运行 AI 交易前检查").click().run(
        timeout=30
    )
    assert not app.exception
    confirmation = next(
        button
        for button in app.button
        if button.label == "确认创建下一交易日模拟委托"
    )
    assert confirmation.disabled is True
    acknowledgement = next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label.startswith("我明白收盘价只用于参考")
    )
    acknowledgement.set_value(True).run(timeout=30)
    confirmation = next(
        button
        for button in app.button
        if button.label == "确认创建下一交易日模拟委托"
    )
    assert confirmation.disabled is False

    next(
        button for button in app.button if button.label == "确认创建下一交易日模拟委托"
    ).click().run(timeout=30)
    assert not app.exception
    assert probe.read_text(encoding="utf-8").splitlines() == [
        "PRETRADE|account-1|sh510300|buy|100|run-simulator",
        "SUBMIT|check-1|100|sh510300|next_open_simulation",
    ]


def test_simulator_symbol_change_clears_link_and_hides_confirmation(tmp_path, monkeypatch):
    probe = tmp_path / "simulator-symbol-change.txt"
    monkeypatch.setenv("QUANTLAB_SIMULATOR_PROBE_FILE", str(probe))
    app = AppTest.from_file("tests/streamlit_simulator_identity_probe.py").run(timeout=30)

    app.text_input[0].set_value("sh600519").run(timeout=30)
    assert not app.exception
    assert app.text_input[0].value == "sh600519"
    assert not probe.exists()
    assert "确认创建盘中模拟委托" not in [button.label for button in app.button]
    assert any("未关联研究" in item.value for item in app.caption)


def test_home_and_task_center_do_not_refresh_tasks_without_user_action(tmp_path):
    from quantlab.config import Settings

    settings = Settings(
        values={
            "system": {
                "database_path": str(tmp_path / "ui-read-only.db"),
                "data_dir": str(tmp_path / "data"),
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "llm": {"provider": "mock", "allow_mock_fallback": True},
        },
        root=tmp_path,
    )
    text = Path(product_ui.__file__).read_text(encoding="utf-8")
    home_block = text.split("def render_home", 1)[1].split("def render_market_and_discovery", 1)[0]
    task_block = text.split("def _render_decision_task_center", 1)[1].split("def _render_chat", 1)[
        0
    ]
    assert "refresh_decision_tasks" not in home_block
    assert "if refresh_column.button" in task_block
    assert task_block.index("if refresh_column.button") < task_block.index("refresh_decision_tasks")
    assert settings.resolve(settings.get("system.database_path")).name == "ui-read-only.db"
