from __future__ import annotations

import inspect

from dashboard import product_ui


def test_customer_facing_labels_hide_raw_storage_values():
    assert product_ui._research_link_label(None) == "未关联研究"
    assert product_ui._research_link_label("run-123") == "已关联研究"
    assert product_ui._status_label("degraded") == "降级"
    assert product_ui._status_label("unknown_internal_value") == "待确认"
    assert product_ui._data_trust_label("server_observed") == "服务器核验"
    assert product_ui._severity_label("warning") == "请复核"
    assert product_ui._adoption_decision_label("partially_adopted") == "部分采纳"


def test_help_center_explains_product_states_in_chinese():
    source = inspect.getsource(product_ui.render_help_center)

    assert "正常、部分可用、数据降级、暂不可用或失败" in source
    assert "available、partial、degraded、unavailable 和 failed" not in source


def test_research_workbench_uses_flat_recent_cards_and_exposes_roundtable_path():
    recent_cards = inspect.getsource(product_ui._render_recent_research_cards)
    workbench = inspect.getsource(product_ui.render_ai_research)

    assert "conclusion.metric(" not in recent_cards
    assert "_render_research_hub(settings)" in workbench
    assert "圆桌复核" in recent_cards
    assert "_open_research_roundtable(item)" in recent_cards


def test_customer_notifications_hide_backend_error_details():
    title, content = product_ui._notification_display(
        {
            "notification_type": "background_job_failed",
            "title": "后台任务失败",
            "content": "Background task wide_forward_registration failed: ValueError: details",
        }
    )

    assert title == "后台更新未完成"
    assert "不完整的数据或结果" in content
    assert "ValueError" not in content

    fallback_title, fallback_content = product_ui._notification_display(
        {
            "notification_type": "new_runtime_failed",
            "title": "Runtime failed",
            "content": "Traceback: hidden implementation detail",
        }
    )
    assert fallback_title == "系统更新未完成"
    assert "Traceback" not in fallback_content


def test_research_evidence_helpers_hide_empty_list_literals_and_translate_known_conditions():
    assert product_ui._research_items([]) == []
    assert product_ui._research_items("  ") == []
    assert product_ui._research_items(["  支持证据  ", ""]) == ["支持证据"]
    assert product_ui._research_condition_label(
        "Price closes below MA_20 (55.20 adjusted) or 20-day return turns negative"
    ) == "价格收盘跌破 20 日均线（55.20，复权），或近 20 日收益转负"


def test_review_outcome_summary_keeps_order_buckets_mutually_exclusive():
    summary = product_ui._review_outcome_summary(
        {"positions": [{"symbol": "sh510300"}]},
        [
            {"status": "filled"},
            {"status": "partially_filled"},
            {"status": "cancelled"},
            {"status": "rejected"},
        ],
        [{"fill_id": "fill-1"}, {"fill_id": "fill-2"}],
        [{"review_id": "review-1"}],
    )

    assert summary["headline"] == "已形成可回看的决策闭环"
    assert summary["filled_orders"] == 1
    assert summary["open_orders"] == 1
    assert summary["stopped_orders"] == 2
    assert sum(
        summary[key] for key in ("filled_percent", "open_percent", "stopped_percent")
    ) == 100.0


def test_review_outcome_summary_does_not_invent_activity_for_an_empty_account():
    summary = product_ui._review_outcome_summary({"positions": []}, [], [], [])

    assert summary["headline"] == "等待第一笔可回溯决策"
    assert summary["orders"] == summary["fills"] == summary["reviews"] == 0
    assert summary["filled_percent"] == summary["open_percent"] == 0.0
