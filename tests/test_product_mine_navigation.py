from __future__ import annotations

from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from dashboard import product_ui
from quantlab.config import Settings
from quantlab.persistence.jobs import JobRepository
from quantlab.runtime.worker import JobWorker


def test_professional_space_renders_only_the_selected_user_view(monkeypatch):
    state: dict[str, object] = {}
    renderers = {
        "account": Mock(),
        "attention": Mock(),
        "chat": Mock(),
        "runtime": Mock(),
        "advanced": Mock(),
    }
    monkeypatch.setattr(product_ui.st, "session_state", state)
    monkeypatch.setattr(product_ui.st, "info", Mock())
    monkeypatch.setattr(product_ui.st, "segmented_control", lambda *_args, **_kwargs: "AI 对话")
    monkeypatch.setattr(product_ui, "_render_account_workspace", renderers["account"])
    monkeypatch.setattr(product_ui, "_render_mine_attention", renderers["attention"])
    monkeypatch.setattr(product_ui, "_render_mine_chat", renderers["chat"])
    monkeypatch.setattr(product_ui, "_render_mine_runtime", renderers["runtime"])
    monkeypatch.setattr(product_ui, "_render_mine_advanced", renderers["advanced"])

    product_ui.render_mine(Mock())

    renderers["chat"].assert_called_once()
    for name, renderer in renderers.items():
        if name != "chat":
            renderer.assert_not_called()


def test_opening_a_professional_notification_selects_the_notification_view(monkeypatch):
    state: dict[str, object] = {}

    class Notifications:
        def mark_read(self, notification_id: str) -> bool:
            assert notification_id == "notification-1"
            return True

    monkeypatch.setattr(product_ui.st, "session_state", state)
    monkeypatch.setattr(product_ui, "NotificationRepository", lambda _path: Notifications())
    monkeypatch.setattr(product_ui, "_notification_target", lambda _settings, _item: {
        "page": "专业空间",
        "context": {},
        "message": "打开通知中心。",
    })
    monkeypatch.setattr(product_ui, "_queue_product_feedback", Mock())
    monkeypatch.setattr(product_ui, "_go_to", Mock())

    product_ui._open_notification(
        Mock(resolve=lambda _value: "data/quantlab.db", get=lambda _name: "data/quantlab.db"),
        {
            "notification_id": "notification-1",
            "notification_type": "research_completed",
            "created_at": "2026-07-22T12:00:00",
        },
    )

    # The destination widgets own their values after they render.  Navigation
    # therefore queues one-shot targets rather than mutating widget state.
    assert state["product_mine_view_target"] == "提醒与任务"
    assert state["product_mine_attention_view_target"] == "通知中心"


def test_professional_space_switches_views_without_rendering_an_exception(tmp_path, monkeypatch):
    database = tmp_path / "mine-navigation.db"
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(database))
    app = AppTest.from_file("tests/streamlit_frontend_e2e_app.py").run(timeout=45)

    next(
        item for item in app.button if item.key == "open_product_account_workspace"
    ).click().run(timeout=45)
    assert not app.exception
    mine_view = next(item for item in app.button_group if item.key == "product_mine_view")
    assert mine_view.value == "账户与论文"

    mine_view.set_value("AI 对话").run(timeout=45)
    assert not app.exception
    assert any(item.value == "AI 对话" for item in app.subheader)


def test_ai_chat_workspace_creates_an_unbound_conversation_and_answers(tmp_path, monkeypatch):
    """The user-facing Chat entry works without requiring a research symbol first."""

    database = tmp_path / "mine-chat.db"
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(database))
    app = AppTest.from_file("tests/streamlit_frontend_e2e_app.py").run(timeout=45)

    next(
        item for item in app.button if item.key == "open_product_account_workspace"
    ).click().run(timeout=45)
    mine_view = next(item for item in app.button_group if item.key == "product_mine_view")
    mine_view.set_value("AI 对话").run(timeout=45)
    next(item for item in app.button if item.key == "chat_create_all:unlinked").click().run(
        timeout=45
    )

    chat_input = next(
        item for item in app.chat_input if item.key == "chat_input_all:unlinked"
    )
    chat_input.set_value("这个系统能做什么？").run(timeout=45)

    assert not app.exception
    assert [item.name for item in app.chat_message] == ["user"]
    jobs = JobRepository(database).jobs(job_type="chat_request", limit=10)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"

    completed = JobWorker(Settings.load(), worker_id="mine-chat-test-worker").run_once()
    assert completed is not None
    assert completed["status"] == "completed"

    app.run(timeout=45)
    assert [item.name for item in app.chat_message] == ["user", "assistant"]
    assert not app.error
