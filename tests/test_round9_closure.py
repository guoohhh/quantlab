from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.persistence import EvidenceRepository
from quantlab.persistence.migrations import initialize_or_upgrade_database
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.runtime.operations import (
    backup_database,
    restore_database_dry_run,
    verify_database_backup,
)
from quantlab.runtime.worker import default_job_handlers
from quantlab.workflows.experiment_recorder import (
    ExperimentRecorder,
    checkpoint_signature,
    next_trading_day_acceptance_report,
)
from quantlab.workflows.investment_thesis import (
    _classify_block_for_thesis,
    _context_price_change,
    check_investment_thesis,
    edit_investment_thesis_draft,
    freeze_investment_thesis_revision,
)
from quantlab.workflows.reflection import controlled_research_memory
from quantlab.workflows.product_demo import run_historical_research_demo
from quantlab.workflows.decision_lifecycle import (
    authoritative_reflection_settlement,
    controlled_memory_refresh,
    thesis_event_check,
    thesis_due_scan,
    thesis_price_invalidation_check,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": str(tmp_path / "quantlab.db"),
                "data_dir": str(tmp_path / "data"),
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "runtime": {
                "backup_directory": str(tmp_path / "backups"),
                "demo_directory": str(tmp_path / "demo"),
                "trusted_data_minimum_field_coverage": 0.8,
            },
            "llm": {
                "provider": "mock",
                "model": "fixture-model-a",
                "maximum_task_cost_usd": 1.0,
            },
            "learning": {"reflection_minimum_mature_samples": 30},
            "strategies": {
                "forward_primary": {"candidate_count": 3},
                "a_share_v4": {"protocol_version": "round9-test"},
                "etf_rotation": {"universe": ["sh510300"]},
            },
            "risk": {"max_single_position": 0.15},
            "costs": {
                "stock": {"trade_lot": 100},
                "etf": {
                    "commission_rate": 0.0001,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0,
                    "transfer_fee_rate": 0.0,
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 15.0,
                    "trade_lot": 100,
                },
            },
        },
        root=PROJECT_ROOT,
    )


def _pack(
    symbol: str,
    *,
    price: float = 12.0,
    red_line: bool = False,
    cutoff: datetime | None = None,
) -> AnalysisContextPack:
    observed = cutoff or datetime.now(UTC) - timedelta(minutes=1)
    blocks = [
        EvidenceBlock(
            block_id=f"market:{symbol}:{observed.timestamp()}",
            domain=EvidenceDomain.MARKET,
            title="market",
            source="server_fixture",
            methodology="authoritative quote",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            quality=EvidenceQuality.AVAILABLE,
            payload={"current_raw_price": price},
        ),
        EvidenceBlock(
            block_id=f"event:{symbol}:{observed.timestamp()}",
            domain=EvidenceDomain.EVENT,
            title="event",
            source="server_fixture",
            methodology="structured event",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            quality=EvidenceQuality.AVAILABLE,
            payload={
                "events": (
                    [{"event_type": "regulatory", "impact_score": 0.95, "sentiment": -1}]
                    if red_line
                    else []
                )
            },
        ),
    ]
    return AnalysisContextPack(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=observed.date(),
        cutoff_at=observed,
        generated_at=observed,
        blocks=blocks,
    )


def _thesis(repository: Round8Repository, symbol: str = "sh600001") -> dict:
    thesis = repository.create_thesis(
        {
            "portfolio_id": "portfolio-1",
            "symbol": symbol,
            "initial_price": 10.0,
            "core_thesis": "The thesis is valid only while frozen assumptions remain true.",
            "user_decision": "adopted",
            "red_lines": ["regulatory"],
            "invalidation_conditions": ["regulatory"],
            "assumptions": [
                {"statement": "earnings remain sound", "verification_metric": "earnings"},
                {"statement": "risk remains bounded", "verification_metric": "risk"},
                {"statement": "red line stays absent", "verification_metric": "events"},
            ],
        }
    )
    revisions = Round9Repository(repository.path)
    draft = revisions.create_thesis_revision(
        thesis["thesis_id"],
        payload={"schema_version": "test", **_draft(thesis["core_thesis"])},
        source="test_fixture",
        edited_by="test",
    )
    revisions.freeze_thesis_revision(draft["revision_id"], thesis_id=thesis["thesis_id"])
    return repository.thesis(thesis["thesis_id"])


def _draft(core_logic: str) -> dict:
    assumptions = []
    for index in range(3):
        assumptions.append(
            {
                "statement": f"Assumption {index} remains verifiable with current evidence.",
                "verification_metric": f"metric-{index}",
                "current_evidence": [],
                "supporting_evidence_refs": [],
                "opposing_evidence_refs": [],
                "check_frequency": "weekly",
                "red_lines": [f"red-line-{index}"],
                "invalidation_conditions": [f"invalid-{index}"],
            }
        )
    return {
        "core_logic": core_logic,
        "assumptions": assumptions,
        "valuation_anchor": "Current price is only an anchor, not a guarantee.",
        "overall_red_lines": ["material regulatory event"],
        "overall_invalidation_conditions": ["facts no longer support the thesis"],
        "needs_review": True,
    }


def test_async_checkpoint_is_zero_cost_on_replay_and_recovers_only_failed_step(tmp_path):
    settings = _settings(tmp_path)
    recorder = ExperimentRecorder(settings)
    run_kwargs = {
        "experiment_name": "round9-checkpoint",
        "experiment_type": "research",
        "run_type": "committee",
        "evidence_boundary": "research_only",
        "idempotency_key": "round9-checkpoint-run",
        "prompt_version": "prompt-v1",
        "context_fingerprint": "context-v1",
        "model_routing": {"provider": "mock", "model": "fixture-model-a"},
    }
    run = recorder.start(**run_kwargs)
    calls = {"primary": 0, "reviewer": 0}

    async def primary():
        calls["primary"] += 1
        return {"answer": 42}

    first = asyncio.run(
        recorder.checkpointed_async_step(
            run["run_id"], step_name="primary", signature="sig-primary", callback=primary
        )
    )
    replay = asyncio.run(
        recorder.checkpointed_async_step(
            run["run_id"], step_name="primary", signature="sig-primary", callback=primary
        )
    )
    assert first["resumed"] is False
    assert replay["resumed"] is True
    assert calls["primary"] == 1

    async def failing_reviewer():
        calls["reviewer"] += 1
        raise RuntimeError("transient provider failure")

    with pytest.raises(RuntimeError, match="transient"):
        asyncio.run(
            recorder.checkpointed_async_step(
                run["run_id"],
                step_name="reviewer",
                signature="sig-reviewer",
                callback=failing_reviewer,
            )
        )
    recorder.fail(run["run_id"], error_detail="RuntimeError")
    resumed_run = recorder.start(**run_kwargs)
    assert resumed_run["run_id"] == run["run_id"]
    assert resumed_run["status"] == "running"
    assert Round9Repository(
        settings.resolve(settings.get("system.database_path"))
    ).run_resume_events(run["run_id"])

    async def successful_reviewer():
        calls["reviewer"] += 1
        return {"status": "accepted"}

    primary_after_resume = asyncio.run(
        recorder.checkpointed_async_step(
            run["run_id"], step_name="primary", signature="sig-primary", callback=primary
        )
    )
    reviewer_after_resume = asyncio.run(
        recorder.checkpointed_async_step(
            run["run_id"],
            step_name="reviewer",
            signature="sig-reviewer",
            callback=successful_reviewer,
        )
    )
    assert primary_after_resume["resumed"] is True
    assert reviewer_after_resume["resumed"] is False
    assert calls == {"primary": 1, "reviewer": 2}


def test_checkpoint_signature_changes_for_model_prompt_context_and_source(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    def signature(**overrides):
        return checkpoint_signature(
            settings,
            workflow_structure="workflow-v1",
            model_routing=overrides.get("model", {"provider": "mock", "model": "a"}),
            prompt_version=overrides.get("prompt", "prompt-a"),
            context_fingerprint=overrides.get("context", "context-a"),
        )

    base = signature()
    assert signature(model={"provider": "mock", "model": "b"}) != base
    assert signature(prompt="prompt-b") != base
    assert signature(context="context-b") != base

    original = __import__(
        "quantlab.workflows.experiment_recorder", fromlist=["source_build_manifest"]
    ).source_build_manifest

    def changed_source(current_settings):
        manifest = original(current_settings)
        return {**manifest, "source_fingerprint": "f" * 64}

    monkeypatch.setattr(
        "quantlab.workflows.experiment_recorder.source_build_manifest", changed_source
    )
    assert signature() != base


def test_context_pack_thesis_checks_reject_forgery_and_keep_price_separate(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    lifecycle = Round8Repository(path)
    evidence = EvidenceRepository(path)
    thesis = _thesis(lifecycle)
    assumption_id = thesis["assumptions"][0]["assumption_id"]
    price_only = _pack("sh600001", price=12.0)
    evidence.save_context(price_only)

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        check_investment_thesis(
            settings,
            thesis_id=thesis["thesis_id"],
            context_id=price_only.context_id,
            context_fingerprint="forged",
            evidence_refs=[],
        )
    other = _pack("sz000001")
    evidence.save_context(other)
    with pytest.raises(ValueError, match="symbol"):
        check_investment_thesis(
            settings,
            thesis_id=thesis["thesis_id"],
            context_id=other.context_id,
            context_fingerprint=other.fingerprint,
            evidence_refs=[],
        )
    future = _pack("sh600001", cutoff=datetime.now(UTC) + timedelta(hours=1))
    evidence.save_context(future)
    with pytest.raises(ValueError, match="future"):
        check_investment_thesis(
            settings,
            thesis_id=thesis["thesis_id"],
            context_id=future.context_id,
            context_fingerprint=future.fingerprint,
            evidence_refs=[],
        )

    price_check = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=price_only.context_id,
        context_fingerprint=price_only.fingerprint,
        evidence_refs=[
            {
                "assumption_id": assumption_id,
                "block_id": price_only.block(EvidenceDomain.MARKET).block_id,
            }
        ],
    )
    assert price_check["price_change_pct"] == pytest.approx(20.0)
    assert price_check["proposed_status"] != "strengthened"

    ignored_thesis = _thesis(lifecycle, symbol="sh600002")
    red_line = _pack("sh600002", price=12.0, red_line=True)
    evidence.save_context(red_line)
    ignored = check_investment_thesis(
        settings,
        thesis_id=ignored_thesis["thesis_id"],
        context_id=red_line.context_id,
        context_fingerprint=red_line.fingerprint,
        evidence_refs=[
            {
                "assumption_id": ignored_thesis["assumptions"][0]["assumption_id"],
                "block_id": red_line.block(EvidenceDomain.EVENT).block_id,
            }
        ],
        user_resolution="ignored",
    )
    assert ignored["proposed_status"] in {"damaged", "broken"}
    assert ignored["final_status"] == "active"


def test_thesis_user_edits_are_versioned_and_frozen_revision_is_immutable(tmp_path):
    settings = _settings(tmp_path)
    lifecycle = Round8Repository(settings.resolve(settings.get("system.database_path")))
    thesis = _thesis(lifecycle)
    first = edit_investment_thesis_draft(
        settings,
        thesis_id=thesis["thesis_id"],
        payload=_draft("The first structured thesis draft remains subject to review."),
    )
    frozen = freeze_investment_thesis_revision(
        settings, thesis_id=thesis["thesis_id"], revision_id=first["revision_id"]
    )
    second = edit_investment_thesis_draft(
        settings,
        thesis_id=thesis["thesis_id"],
        payload=_draft("The second structured thesis draft records a user revision."),
    )
    revisions = Round9Repository(lifecycle.path).thesis_revisions(thesis["thesis_id"])
    by_id = {item["revision_id"]: item for item in revisions}
    assert frozen["status"] == "frozen"
    assert second["revision_number"] == first["revision_number"] + 1
    assert by_id[first["revision_id"]]["payload"]["core_logic"].startswith("The first")
    assert by_id[first["revision_id"]]["status"] == "frozen"
    assert by_id[second["revision_id"]]["payload"]["core_logic"].startswith("The second")
    assert sum(item["status"] == "frozen" for item in revisions) == 1


def test_thesis_due_scan_is_idempotent_and_missing_reflection_tables_fail_closed(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    lifecycle = Round8Repository(path)
    thesis = _thesis(lifecycle)
    with lifecycle.connect() as db:
        db.execute(
            "UPDATE investment_theses SET next_check_at=? WHERE thesis_id=?",
            (date.today().isoformat(), thesis["thesis_id"]),
        )
    first = thesis_due_scan(settings, as_of=date.today())
    second = thesis_due_scan(settings, as_of=date.today())
    assert first["due"] == 1
    assert second["tasks"][0]["task_id"] == first["tasks"][0]["task_id"]

    isolated = _settings(tmp_path / "missing-forward")
    result = authoritative_reflection_settlement(isolated)
    assert result["settled"] == 0
    assert "required settlement tables missing" in result["unavailable"][0]["reason"]


@pytest.mark.parametrize(
    ("override", "failed_gate"),
    [
        ({"primary_start_count": 0}, "primary_exactly_once"),
        ({"primary_start_count": 2}, "primary_exactly_once"),
        ({"formal_samples": 0}, "formal_samples_registered"),
        ({"shadow_accounts": 6}, "seven_independent_shadow_accounts"),
        ({"schema_ready": False, "missing_schema": ["forward_registration_runs"]}, "acceptance_schema_ready"),
    ],
)
def test_next_trading_day_acceptance_fails_closed_on_each_mandatory_gate(
    tmp_path, monkeypatch, override, failed_gate
):
    settings = _settings(tmp_path)
    trade_date = date(2026, 7, 20)
    Round8Repository(settings.resolve(settings.get("system.database_path"))).record_provider_selections(
        "refresh-current",
        {
            "calendar": {
                "selected_provider": "baostock",
                "reason": "priority",
                "related_failures": [],
                "attempts": [],
            }
        },
    )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "start_allowed": True,
            "blockers": [],
            "processes": {"worker": {"healthy": True}, "scheduler": {"healthy": True}},
            "quality_gate": {"ready": True},
            "llm": {"real_endpoint_count": 1},
            "data": {
                "calendar_day": {"trade_date": trade_date.isoformat(), "is_open": True},
                "point_in_time_pool": {"ready": True, "total_members": 10, "eligible_members": 10},
                "source_states": {
                    "point_in_time_pool": {
                        "date_end": trade_date.isoformat(),
                        "detail": {"required_field_coverage": {"industry": 0.95}},
                    }
                },
            },
        },
    )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.formal_experiment_status",
        lambda *_args, **_kwargs: {
            "experiment": {
                "experiment_id": "experiment-1",
                "cohort_id": "cohort-1",
                "candidate_count": 3,
            },
            "formal_samples": 1,
            "shadow_trading_scorecard": {"accounts": []},
        },
    )
    checks = {
        "primary_start_count": 1,
        "formal_samples": 1,
        "shadow_accounts": 7,
        "shadow_variants": [str(index) for index in range(7)],
        "shadow_accounts_independent": True,
        "duplicate_formal_samples": 0,
        "demo_pollution": 0,
        "schema_ready": True,
        "missing_schema": [],
        **override,
    }
    monkeypatch.setattr(
        "quantlab.workflows.experiment_recorder._formal_acceptance_database_checks",
        lambda *_args, **_kwargs: checks,
    )
    monkeypatch.setattr(
        "quantlab.workflows.experiment_recorder._provider_refresh_acceptance_checks",
        lambda *_args, **_kwargs: {
            "provider_refresh_id": "refresh-current",
            "provider_refresh_market_date": trade_date.isoformat(),
            "provider_components_expected": [],
            "provider_components_observed": [],
            "missing_provider_components": [],
            "unavailable_provider_components": [],
            "pool_refresh_matches": True,
            "provider_selection_passed": True,
            "selected_providers": [],
            "component_checks": {},
            "explicit_provider_failure": False,
        },
    )
    report = next_trading_day_acceptance_report(settings, trade_date=trade_date)
    assert report["status"] == "blocked"
    assert report["checks"]["mandatory"][failed_gate] is False


def test_decision_run_export_is_idempotent_and_contains_context_checkpoint(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    pack = _pack("sh600001")
    EvidenceRepository(path).save_context(pack)
    recorder = ExperimentRecorder(settings)
    run = recorder.start(
        experiment_name="audit-export",
        experiment_type="research",
        run_type="decision",
        evidence_boundary="research_only",
        idempotency_key="round9-audit-export",
        context_fingerprint=pack.fingerprint,
    )
    recorder.checkpointed_step(
        run["run_id"],
        step_name="context_pack_ready",
        signature="context-signature",
        callback=lambda: {"context_id": pack.context_id},
    )
    recorder.link(
        run["run_id"],
        entity_type="context_pack",
        entity_id=pack.context_id,
        relation="input",
    )
    recorder.complete(run["run_id"], result_summary={"action": "watch"})
    repository = Round9Repository(path)
    first = repository.export_decision_run(run["run_id"])
    second = repository.export_decision_run(run["run_id"])
    assert second["export_id"] == first["export_id"]
    assert second["artifact_fingerprint"] == first["artifact_fingerprint"]
    assert first["checkpoints"][run["run_id"]][0]["status"] == "completed"
    snapshot = first["entity_snapshots"]["context_pack"][0]
    assert snapshot["status"] == "available"
    assert snapshot["record"]["fingerprint"] == pack.fingerprint


def test_backup_verify_and_restore_dry_run_never_modify_production_database(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    initialize_or_upgrade_database(path)
    backup = backup_database(settings, label="round9")
    verified = verify_database_backup(
        settings, backup_path=backup["database"], expected_sha256=backup["sha256"]
    )
    with sqlite3.connect(path) as db:
        before_dry_run = hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()
    dry_run = restore_database_dry_run(
        settings, backup_path=backup["database"], expected_sha256=backup["sha256"]
    )
    assert verified["verified"] is True
    assert dry_run["dry_run"] is True
    assert dry_run["production_database_modified"] is False
    assert dry_run["post_migration_integrity"] == "ok"
    with sqlite3.connect(path) as db:
        after_dry_run = hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()
    assert after_dry_run == before_dry_run


def test_historical_demo_scorecard_is_research_only_and_honest_about_pit_gap(tmp_path):
    settings = _settings(tmp_path)
    first = run_historical_research_demo(settings)
    second = run_historical_research_demo(settings)
    scorecard = first["historical_scorecard"]
    assert second["historical_scorecard"]["scorecard_id"] == scorecard["scorecard_id"]
    assert scorecard["evidence_boundary"] == "research_only"
    assert scorecard["payload"]["point_in_time_verified"] is False
    assert scorecard["metrics"]["llm_fusion_metrics"] == "unavailable"
    assert first["formal_experiments_in_demo_database"] == 0


def test_automatic_thesis_checks_use_frozen_context_and_fail_closed_per_thesis(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    repository = Round8Repository(settings.resolve(settings.get("system.database_path")))
    available = _thesis(repository, "sh600001")
    unavailable = _thesis(repository, "sh600002")

    class FakeLifecycleRepository:
        def __init__(self, _path):
            pass

        def theses(self, *, statuses):
            assert "active" in statuses
            return [available, unavailable]

    build_calls: list[dict] = []
    check_calls: list[dict] = []

    def fake_build(_settings, *, symbol, as_of, account_id, include_events, save):
        build_calls.append(
            {
                "symbol": symbol,
                "as_of": as_of,
                "account_id": account_id,
                "include_events": include_events,
                "save": save,
            }
        )
        if symbol == "sh600002":
            raise RuntimeError("authoritative context unavailable")
        return _pack(symbol).model_dump(mode="json")

    def fake_check(_settings, **kwargs):
        check_calls.append(kwargs)
        return {"thesis_id": kwargs["thesis_id"], "status": "unchanged"}

    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.Round8Repository", FakeLifecycleRepository
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.build_analysis_context_pack", fake_build
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.check_investment_thesis", fake_check
    )

    event_result = thesis_event_check(settings, as_of=date(2026, 7, 20))
    price_result = thesis_price_invalidation_check(settings, as_of=date(2026, 7, 20))

    assert event_result["checked"] == 2
    assert event_result["results"][1] == {
        "thesis_id": unavailable["thesis_id"],
        "status": "unavailable",
        "reason": "RuntimeError",
    }
    assert price_result["checked"] == 2
    assert [item["include_events"] for item in build_calls] == [True, True, False, False]
    assert all(item["save"] is True for item in build_calls)
    assert check_calls[0]["user_resolution"] == "system_verified"
    assert check_calls[0]["trigger_type"] == "automatic_event_check"
    assert check_calls[1]["trigger_type"] == "automatic_price_check"
    assert check_calls[0]["evidence_refs"]


def test_controlled_memory_refresh_only_promotes_to_manual_challenge_stage(tmp_path):
    settings = _settings(tmp_path)
    settings.values["learning"]["reflection_minimum_mature_samples"] = 0
    Round8Repository(settings.resolve(settings.get("system.database_path")))

    result = controlled_memory_refresh(settings)

    assert result["matured_authoritative_reflections"] == 0
    assert result["minimum_for_challenge"] == 0
    assert result["challenge_eligible_updated"] == 0
    assert result["automatic_rule_change"] is False
    assert result["automatic_weight_change"] is False
    assert result["automatic_threshold_change"] is False


def test_controlled_research_memory_enforces_age_count_bytes_and_cross_symbol_weight(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    memories = [
        {
            "memory_id": "old",
            "reflection_id": "reflection-old",
            "symbol": "sh600001",
            "scope": "symbol",
            "lesson": "expired",
            "weight": 1.0,
            "status": "candidate",
            "challenge_eligible": 0,
            "created_at": (now - timedelta(days=800)).isoformat(),
        },
        {
            "memory_id": "same-candidate",
            "reflection_id": "reflection-1",
            "symbol": "sh600001",
            "scope": "symbol",
            "lesson": "same symbol candidate lesson",
            "weight": 0.8,
            "status": "candidate",
            "challenge_eligible": 0,
            "created_at": now.isoformat(),
        },
        {
            "memory_id": "same-challenge",
            "reflection_id": "reflection-2",
            "symbol": "sh600001",
            "scope": "symbol",
            "lesson": "same symbol challenge lesson",
            "weight": 1.2,
            "status": "candidate",
            "challenge_eligible": 1,
            "created_at": now.isoformat(),
        },
        {
            "memory_id": "cross",
            "reflection_id": "reflection-3",
            "symbol": "sz000001",
            "scope": "cross_symbol",
            "lesson": "cross symbol auxiliary lesson",
            "weight": 0.9,
            "status": "candidate",
            "challenge_eligible": 1,
            "created_at": now.isoformat(),
        },
    ]

    class FakeMemoryRepository:
        def __init__(self, _path):
            pass

        def memories(self, _symbol):
            return memories

    monkeypatch.setattr(
        "quantlab.workflows.reflection.Round8Repository", FakeMemoryRepository
    )

    result = controlled_research_memory(
        settings,
        symbol="sh600001",
        maximum_items=3,
        maximum_bytes=10_000,
        lookback_days=730,
    )
    limited = controlled_research_memory(
        settings,
        symbol="sh600001",
        maximum_items=1,
        maximum_bytes=10_000,
        lookback_days=730,
    )

    assert [item["memory_id"] for item in result["lessons"]] == [
        "same-candidate",
        "same-challenge",
        "cross",
    ]
    assert result["same_symbol"][1]["classification"] == "challenge_eligible"
    assert result["cross_symbol_low_weight"][0]["classification"] == "auxiliary"
    assert result["cross_symbol_low_weight"][0]["weight"] == pytest.approx(0.25)
    assert limited["limits"]["used_items"] == 1
    assert "not current facts or guarantees" in result["claim_boundary"]


def test_thesis_structured_classification_and_price_helpers_preserve_fact_boundaries():
    observed = datetime.now(UTC) - timedelta(minutes=1)
    supported = EvidenceBlock(
        block_id="financial:supported",
        domain=EvidenceDomain.FINANCIAL,
        title="financial",
        source="server_fixture",
        methodology="structured",
        as_of=observed,
        available_at=observed,
        fetched_at=observed,
        quality=EvidenceQuality.AVAILABLE,
        payload={"thesis_evaluations": {"assumption-1": {"status": "supports"}}},
    )
    contradicted = supported.model_copy(
        update={
            "block_id": "financial:contradicted",
            "payload": {
                "thesis_evaluations": [
                    "invalid",
                    {"assumption_id": "other", "status": "supports"},
                    {"assumption_id": "assumption-1", "classification": "contradicts"},
                ]
            },
        }
    )
    red_line = supported.model_copy(
        update={
            "block_id": "financial:red-line",
            "payload": {"red_line_triggered": True, "red_line_reason": "material breach"},
        }
    )

    assert _classify_block_for_thesis(
        supported, assumption_id="assumption-1"
    ) == ("supports", None)
    assert _classify_block_for_thesis(
        contradicted, assumption_id="assumption-1"
    ) == ("contradicts", None)
    assert _classify_block_for_thesis(
        red_line, assumption_id="assumption-1"
    ) == ("red_line", "material breach")
    assert _context_price_change(_pack("sh600001", price=12.0), 10.0) == pytest.approx(20.0)
    assert _context_price_change(_pack("sh600001"), 0.0) is None


def test_round9_worker_handlers_delegate_with_cancellation_checks(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    calls: list[tuple[str, object]] = []

    class Context:
        worker_id = "round9-worker"

        def progress(self, value, message, payload=None):
            calls.append(("progress", (value, message, payload)))

        def check_cancelled(self):
            calls.append(("cancel", None))

    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.thesis_due_scan",
        lambda _settings, *, as_of: {"handler": "due", "as_of": as_of.isoformat()},
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.thesis_event_check",
        lambda _settings, *, as_of: {"handler": "event", "as_of": as_of.isoformat()},
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.thesis_price_invalidation_check",
        lambda _settings, *, as_of: {"handler": "price", "as_of": as_of.isoformat()},
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.authoritative_reflection_settlement",
        lambda _settings, *, limit: {"handler": "reflection", "limit": limit},
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_lifecycle.controlled_memory_refresh",
        lambda _settings: {"handler": "memory"},
    )
    monkeypatch.setattr(
        "quantlab.workflows.decision_tasks.refresh_decision_tasks",
        lambda _settings: {"handler": "tasks"},
    )

    handlers = default_job_handlers(settings)
    context = Context()
    as_of = "2026-07-20"

    assert handlers["thesis_due_scan"](context, {"as_of": as_of})["handler"] == "due"
    assert handlers["thesis_event_check"](context, {"as_of": as_of})["handler"] == "event"
    assert (
        handlers["thesis_price_invalidation_check"](context, {"as_of": as_of})["handler"]
        == "price"
    )
    assert handlers["authoritative_reflection_settlement"](
        context, {"limit": 7}
    ) == {"handler": "reflection", "limit": 7}
    assert handlers["controlled_memory_refresh"](context, {}) == {"handler": "memory"}
    assert handlers["decision_task_refresh"](context, {}) == {"handler": "tasks"}
    assert sum(kind == "cancel" for kind, _payload in calls) == 6
    assert sum(kind == "progress" for kind, _payload in calls) == 6
