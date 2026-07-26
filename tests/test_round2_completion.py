from __future__ import annotations

from datetime import UTC, date, datetime

from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.persistence import NotificationRepository
from quantlab.workflows.notification_rules import (
    emit_context_quality_notifications,
    emit_llm_runtime_notifications,
)


def test_context_conflict_and_llm_runtime_notifications(settings):
    timestamp = datetime(2026, 7, 17, 7, tzinfo=UTC)
    pack = AnalysisContextPack(
        context_id="conflict-context",
        symbol="sh600001",
        asset_type=AssetType.STOCK,
        as_of=date(2026, 7, 17),
        cutoff_at=datetime(2026, 7, 17, 15, tzinfo=UTC),
        blocks=[
            EvidenceBlock(
                block_id="conflicting-price",
                domain=EvidenceDomain.MARKET,
                title="conflict",
                source="source-a;source-b",
                methodology="isolated comparison",
                as_of=timestamp,
                available_at=timestamp,
                fetched_at=timestamp,
                freshness="fresh",
                quality=EvidenceQuality.CONFLICT,
                payload={"difference_pct": 5.0},
            )
        ],
    )
    emitted = emit_context_quality_notifications(settings, pack)
    assert "data_source_conflict" in emitted

    health = {
        "governance": {
            "task_id": "task-1",
            "budget": {
                "maximum_calls": 2,
                "maximum_total_tokens": 100,
                "maximum_cost_usd": 1.0,
            },
            "usage": {"calls": 2, "input_tokens": 50, "output_tokens": 25, "cost_usd": 0.5},
        },
        "recent_call_log": [
            {"endpoint_id": "primary", "status": "error"},
            {"endpoint_id": "fallback", "status": "ok"},
        ],
    }
    runtime = emit_llm_runtime_notifications(
        settings,
        health=health,
        run_id="run-1",
        symbol="sh600001",
        as_of="2026-07-17",
    )
    assert set(runtime) == {"llm_budget_reached", "provider_fallback"}
    notification_types = {
        item["notification_type"]
        for item in NotificationRepository(
            settings.resolve(settings.get("system.database_path"))
        ).list(limit=20)
    }
    assert {"data_source_conflict", "llm_budget_reached", "provider_fallback"} <= notification_types
