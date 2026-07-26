from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.local_settings import (
    remove_local_llm_key,
    save_llm_product_preferences,
)
from quantlab.config import Settings
from quantlab.persistence.chat import ChatRepository
from quantlab.persistence.notifications import NotificationRepository
from quantlab.runtime.notification_delivery import (
    MemoryChannelAdapter,
    NotificationDeliveryWorker,
)
from quantlab.workflows.chat import create_chat_conversation


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        values={
            "system": {"database_path": "quantlab.db", "data_dir": "data"},
            "llm": {"provider": "mock"},
        },
        root=tmp_path,
    )


def test_llm_product_preferences_keep_other_provider_secrets_and_validate_compatible_url(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEYS", raising=False)
    monkeypatch.delenv("QUANTLAB_LLM_API_KEY", raising=False)

    save_llm_product_preferences(
        tmp_path,
        provider="deepseek",
        api_key="deepseek-secret",
        model="deepseek-reasoner",
        base_url=None,
    )
    save_llm_product_preferences(
        tmp_path,
        provider="openai",
        api_key="openai-secret",
        model="gpt-test",
        base_url=None,
    )
    save_llm_product_preferences(
        tmp_path,
        provider="openai_compatible",
        api_key="compatible-secret",
        model="compatible-model",
        base_url="https://gateway.example/v1",
    )

    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "QUANTLAB_LLM_PROVIDER=openai_compatible" in content
    assert "DEEPSEEK_API_KEYS=deepseek-secret" in content
    assert "OPENAI_API_KEYS=openai-secret" in content
    assert "QUANTLAB_LLM_API_KEY=compatible-secret" in content
    assert "QUANTLAB_LLM_MODEL=compatible-model" in content

    remove_local_llm_key(tmp_path, provider="openai_compatible")
    assert "compatible-secret" not in (tmp_path / ".env").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="模型名称"):
        save_llm_product_preferences(
            tmp_path,
            provider="openai_compatible",
            api_key=None,
            model="",
            base_url="https://gateway.example/v1",
        )
    with pytest.raises(ValueError, match="HTTPS"):
        save_llm_product_preferences(
            tmp_path,
            provider="openai_compatible",
            api_key=None,
            model="compatible-model",
            base_url="http://gateway.example/v1",
        )


def test_global_chat_context_isolated_by_account_and_page_scope(tmp_path):
    settings = _settings(tmp_path)
    first = create_chat_conversation(
        settings,
        title="交易页账户 A",
        account_id=None,
        page_scope="page:组合与交易",
        idempotency_key="chat-context-a",
    )
    second = create_chat_conversation(
        settings,
        title="研究页",
        account_id=None,
        page_scope="page:研究台",
        idempotency_key="chat-context-b",
    )
    repository = ChatRepository(tmp_path / "quantlab.db")

    trade_conversations = repository.conversations_for_context(
        account_id=None,
        symbol=None,
        research_run_id=None,
        page_scope="page:组合与交易",
    )
    research_conversations = repository.conversations_for_context(
        account_id=None,
        symbol=None,
        research_run_id=None,
        page_scope="page:研究台",
    )

    assert [item["conversation_id"] for item in trade_conversations] == [first["conversation_id"]]
    assert [item["conversation_id"] for item in research_conversations] == [second["conversation_id"]]


def test_email_channel_status_and_test_delivery_stay_in_worker_queue(tmp_path):
    settings = _settings(tmp_path)
    memory = MemoryChannelAdapter(channel="email", delivered=[])
    worker = NotificationDeliveryWorker(
        settings,
        worker_id="test-notification-worker",
        adapters={"email": memory},
    )
    assert worker.channel_status("email")["state"] == "not_configured"
    worker.configure_channel(
        channel="email",
        enabled=True,
        config={
            "smtp_host": "memory-only",
            "from_address": "from@example.com",
            "to_address": "to@example.com",
        },
    )
    assert worker.channel_status("email")["state"] == "ready"

    queued = worker.queue_email_test()
    assert queued["status"] == "queued"
    notifications = NotificationRepository(tmp_path / "quantlab.db")
    created = notifications.list(notification_type="email_delivery_test", limit=1)[0]
    exact_notification = notifications.get_by_dedup_key(created["dedup_key"])
    assert exact_notification is not None
    assert exact_notification["notification_id"] == queued["notification_id"]
    before = worker.channel_status("email")
    assert before["state"] == "queued"
    assert memory.delivered == []

    result = worker.run_once()
    assert result["external_delivered"] == 1
    after = worker.channel_status("email")
    assert after["state"] == "delivered"
    assert len(memory.delivered) == 1
    assert notifications.list(notification_type="email_delivery_test", limit=1)


def test_each_email_test_click_gets_its_own_queued_delivery(tmp_path):
    settings = _settings(tmp_path)
    worker = NotificationDeliveryWorker(
        settings,
        worker_id="test-notification-concurrency",
        adapters={"email": MemoryChannelAdapter(channel="email", delivered=[])},
    )
    worker.configure_channel(
        channel="email",
        enabled=True,
        config={
            "smtp_host": "memory-only",
            "from_address": "from@example.com",
            "to_address": "to@example.com",
        },
    )

    first = worker.queue_email_test()
    second = worker.queue_email_test()
    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(pool.map(lambda _index: worker.queue_email_test(), range(4)))

    notification_ids = {
        item["notification_id"] for item in [first, second, *concurrent]
    }
    assert len(notification_ids) == 6
    with worker.connect() as db:
        rows = db.execute(
            """
            SELECT notification_id,status,attempts
            FROM notification_channel_outbox
            WHERE channel='email'
            """
        ).fetchall()
    assert len(rows) == 6
    assert {row["notification_id"] for row in rows} == notification_ids
    assert {row["status"] for row in rows} == {"pending"}
    assert {row["attempts"] for row in rows} == {0}


def test_global_assistant_opens_and_queues_without_blocking_the_page(tmp_path, monkeypatch):
    database = tmp_path / "global-assistant.db"
    monkeypatch.setenv("QUANTLAB_DATABASE_PATH", str(database))
    monkeypatch.setenv("QUANTLAB_LLM_PROVIDER", "mock")

    app = AppTest.from_file("dashboard/app.py", default_timeout=40).run(timeout=40)
    assert not app.exception

    app.button(key="open_global_ai_assistant").click().run(timeout=40)
    assert not app.exception
    assert len(app.text_area) == 1

    prompt = "Summarize the current page without research tools."
    app.text_area[0].set_value(prompt)
    submit = next(
        button
        for button in app.button
        if str(button.key).startswith("FormSubmitter:global_chat_form_")
    )
    submit.click().run(timeout=40)
    assert not app.exception

    with sqlite3.connect(database) as db:
        job = db.execute(
            "SELECT job_type,status FROM background_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conversation = db.execute(
            "SELECT page_scope FROM chat_conversations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        message = db.execute(
            "SELECT role,content FROM chat_messages ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    assert job == ("chat_request", "queued")
    assert conversation == ("page:今日",)
    assert message == ("user", prompt)
    assert any(
        str(button.key).startswith("cancel_global_chat_job_") for button in app.button
    )
