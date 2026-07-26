from __future__ import annotations

import threading
import time
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
    MarketQuote,
)
from quantlab.domain.data_governance import DataTrustLevel
from quantlab.market import QuoteService
from quantlab.persistence import EvidenceRepository
from quantlab.data.provider_router import (
    ProviderCallInFlight,
    ProviderCallTimeout,
    call_single_flight,
    provider_flight_active,
)
from quantlab.persistence.round7 import Round7Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.runtime.soak import soak_report
from quantlab.workflows.experiment_recorder import (
    ExperimentRecorder,
    checkpoint_signature,
    next_trading_day_acceptance_report,
)
from quantlab.workflows.investment_thesis import check_investment_thesis
from quantlab.workflows.reflection import record_outcome_reflection
from quantlab.workflows.decision_lifecycle import authoritative_reflection_settlement
from quantlab.workflows.forward_ablation import (
    create_round3_forward_cohort,
    freeze_forward_ablation_sample,
)
from quantlab.workflows.trusted_data_adapters import FreeTrustedDataAdapter
from quantlab.workflows.chat import ChatToolRegistry


def _settings(tmp_path) -> Settings:
    return Settings(
        values={
            "system": {
                "database_path": str(tmp_path / "quantlab.db"),
                "data_dir": str(tmp_path / "data"),
                "test_mode": True,
                "timezone": "Asia/Shanghai",
            },
            "runtime": {"backup_directory": str(tmp_path / "backups")},
            "llm": {
                "provider": "mock",
                "allow_mock_fallback": True,
                "maximum_task_cost_usd": 1.0,
            },
            "learning": {"reflection_minimum_mature_samples": 30},
            "strategies": {
                "forward_primary": {
                    "candidate_count": 1,
                    "minimum_trust_level": "server_observed",
                },
                "a_share_v4": {"protocol_version": "round8-test"},
                "etf_rotation": {"universe": ["sh510300"]},
            },
            "costs": {"stock": {"trade_lot": 100}, "etf": {"trade_lot": 100}},
            "risk": {"max_single_position": 0.15},
        },
        root=tmp_path,
    )


class _AuthoritativeQuoteProvider:
    provider_name = "round9_authoritative_fixture"
    provider_version = "fixture-v1"
    authoritative = True

    def __init__(self, quote: MarketQuote):
        self._quote = quote

    def quote(self, symbol: str, *, asset_type: AssetType, as_of: date) -> MarketQuote:
        if symbol != self._quote.symbol or self._quote.as_of > as_of:
            raise ValueError("authoritative fixture quote unavailable")
        return self._quote.model_copy(update={"asset_type": asset_type})


def _context_pack(
    symbol: str,
    *,
    as_of: date,
    current_price: float = 10.0,
    red_line: bool = False,
    cutoff_at: datetime | None = None,
) -> AnalysisContextPack:
    observed = cutoff_at or datetime.combine(as_of, datetime.min.time(), tzinfo=UTC).replace(
        hour=8
    )
    blocks = [
        EvidenceBlock(
            block_id=f"market:{symbol}:{as_of.isoformat()}",
            domain=EvidenceDomain.MARKET,
            title="market",
            source="authoritative_fixture",
            methodology="server_quote",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            quality=EvidenceQuality.AVAILABLE,
            payload={"current_raw_price": current_price},
        ),
        EvidenceBlock(
            block_id=f"event:{symbol}:{as_of.isoformat()}",
            domain=EvidenceDomain.EVENT,
            title="events",
            source="authoritative_fixture",
            methodology="structured_events",
            as_of=observed,
            available_at=observed,
            fetched_at=observed,
            quality=EvidenceQuality.AVAILABLE,
            payload={
                "events": (
                    [{"event_type": "regulatory", "impact_score": 0.95, "sentiment": -1.0}]
                    if red_line
                    else []
                )
            },
        ),
    ]
    return AnalysisContextPack(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        as_of=as_of,
        cutoff_at=observed,
        generated_at=observed,
        blocks=blocks,
        deterministic_summary={"fixture": True},
    )


def _freeze_test_thesis(repository: Round8Repository, thesis: dict) -> dict:
    revision_repository = Round9Repository(repository.path)
    revision = revision_repository.create_thesis_revision(
        thesis["thesis_id"],
        payload={
            "schema_version": "round9-test",
            "core_logic": thesis["core_thesis"],
            "assumptions": [
                {
                    "statement": item["statement"],
                    "verification_metric": item["verification_metric"],
                    "current_evidence": [],
                    "supporting_evidence_refs": [],
                    "opposing_evidence_refs": [],
                    "check_frequency": "weekly",
                    "red_lines": ["material adverse event"],
                    "invalidation_conditions": ["facts no longer support the thesis"],
                }
                for item in thesis["assumptions"]
            ],
            "valuation_anchor": "Current price is an audit anchor, not a guarantee.",
            "overall_red_lines": thesis.get("red_lines") or ["material adverse event"],
            "overall_invalidation_conditions": thesis.get("invalidation_conditions")
            or ["facts no longer support the thesis"],
            "needs_review": True,
        },
        source="test_fixture",
        edited_by="test",
    )
    revision_repository.freeze_thesis_revision(
        revision["revision_id"], thesis_id=thesis["thesis_id"]
    )
    return repository.thesis(thesis["thesis_id"])


def _authoritative_forward_fixture(
    settings: Settings, *, symbol: str, realized_return_pct: float
) -> tuple[dict, dict]:
    signal_date = date.today()
    pack = _context_pack(
        symbol,
        as_of=signal_date,
        cutoff_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(pack)
    start_quote = MarketQuote(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        raw_price=10.0,
        as_of=signal_date,
        available_at=pack.cutoff_at,
        observed_at=pack.cutoff_at,
        source="authoritative_fixture",
        provider="round9_authoritative_fixture",
        source_version="fixture-v1",
        session_status="closed",
        quote_kind="current_close",
        authoritative=True,
        evidence_stage="production",
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        license_status="test_fixture",
    )
    cohort = create_round3_forward_cohort(settings)
    rows = freeze_forward_ablation_sample(
        settings,
        cohort_id=cohort["cohort_id"],
        symbol=symbol,
        horizon_days=5,
        quote_service=QuoteService(_AuthoritativeQuoteProvider(start_quote)),
        context_pack=pack,
        committee_runner=lambda *_args, **_kwargs: {
            "action": "buy",
            "confidence": 0.7,
            "suggested_weight_max": 0.1,
            "degraded_roles": [],
        },
        registration_origin="automatic_primary",
        as_of=signal_date,
    )
    matured_due_at = datetime.now(UTC) - timedelta(minutes=1)
    evidence_repository = StrategyEvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    with evidence_repository.connect() as db:
        db.execute(
            """UPDATE forward_ablation_predictions SET due_at=?
               WHERE cohort_id=? AND sample_key=? AND horizon_days=5""",
            (matured_due_at.isoformat(), cohort["cohort_id"], rows[0]["sample_key"]),
        )
    due_date = matured_due_at.date()
    end_quote = start_quote.model_copy(
        update={
            "raw_price": 10.0 * (1.0 + realized_return_pct / 100.0),
            "as_of": due_date,
            "available_at": datetime.now(UTC) - timedelta(minutes=2),
            "observed_at": datetime.now(UTC),
        }
    )
    evidence_repository.settle_forward_sample(
        cohort_id=cohort["cohort_id"],
        sample_key=rows[0]["sample_key"],
        horizon_days=5,
        realized_return_pct=realized_return_pct,
        outcome_source="round9_authoritative_fixture:fixture-v1",
        observed_at=datetime.now(UTC),
        transaction_cost_pct_by_variant={item["variant"]: 0.1 for item in rows},
        payload={
            "end_price": end_quote.raw_price,
            "quote": end_quote.model_dump(mode="json"),
            "maximum_favorable_excursion": max(0.0, realized_return_pct),
        },
    )
    recorder = ExperimentRecorder(settings)
    run = recorder.start(
        experiment_name=f"forward-reflection-{symbol}",
        experiment_type="forward",
        run_type="settlement",
        evidence_boundary="forward_shadow",
        idempotency_key=f"forward-reflection-{symbol}",
        context_fingerprint=pack.fingerprint,
        parameters={"cohort_id": cohort["cohort_id"], "symbol": symbol},
    )
    recorder.complete(run["run_id"], result_summary={"settled": True})
    prediction = next(item for item in rows if item["variant"] == "full_system")
    recorder.link(
        run["run_id"],
        entity_type="forward_prediction",
        entity_id=prediction["prediction_id"],
        relation="settled_source",
    )
    return run, prediction


def test_provider_timeout_is_single_flight_across_retry_component_and_refresh():
    release = threading.Event()
    calls = 0

    def slow_call():
        nonlocal calls
        calls += 1
        release.wait(2)
        return ["done"]

    provider_key = f"round8-provider-{time.monotonic_ns()}"
    with pytest.raises(ProviderCallTimeout):
        call_single_flight(provider_key, slow_call, 0.01)
    assert provider_flight_active(provider_key) is True
    with pytest.raises(ProviderCallInFlight):
        call_single_flight(provider_key, slow_call, 0.01)
    with pytest.raises(ProviderCallInFlight):
        call_single_flight(provider_key, lambda: ["second-component"], 0.01)
    assert calls == 1
    release.set()
    deadline = time.monotonic() + 2
    while provider_flight_active(provider_key) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert call_single_flight(provider_key, lambda: ["next-refresh"], 0.2) == [
        "next-refresh"
    ]
    assert calls == 1


def test_adapter_max_attempts_two_never_overlaps_timed_provider(tmp_path):
    settings = _settings(tmp_path)
    settings.values["runtime"].update(
        {
            "trusted_provider_timeout_seconds": 0.01,
            "trusted_provider_max_attempts": 2,
            "trusted_provider_failure_threshold": 10,
            "trusted_provider_cooldown_seconds": 1,
        }
    )
    release = threading.Event()
    calls = 0

    def slow_call():
        nonlocal calls
        calls += 1
        release.wait(2)
        return ["late"]

    adapter = FreeTrustedDataAdapter(settings)
    output = {
        "provider_attempts": [],
        "failures": [],
        "selected_providers": {},
    }
    assert adapter._collect_component(  # noqa: SLF001 - explicit concurrency contract
        output,
        provider_key="round8-adapter-provider",
        component="calendar",
        callback=slow_call,
        default=[],
    ) == []
    assert calls == 1
    assert len(output["provider_attempts"]) == 1
    assert output["provider_attempts"][0]["attempt"] == 1
    assert adapter._collect_component(  # noqa: SLF001
        output,
        provider_key="round8-adapter-provider",
        component="security_master",
        callback=slow_call,
        default=[],
    ) == []
    assert calls == 1

    next_refresh = FreeTrustedDataAdapter(settings)
    next_output = {
        "provider_attempts": [],
        "failures": [],
        "selected_providers": {},
    }
    assert next_refresh._collect_component(  # noqa: SLF001
        next_output,
        provider_key="round8-adapter-provider",
        component="calendar",
        callback=slow_call,
        default=[],
    ) == []
    assert calls == 1
    release.set()
    deadline = time.monotonic() + 2
    while provider_flight_active("round8-adapter-provider") and time.monotonic() < deadline:
        time.sleep(0.01)


def test_experiment_recorder_is_idempotent_traceable_and_checkpoint_safe(tmp_path):
    settings = _settings(tmp_path)
    recorder = ExperimentRecorder(settings)
    kwargs = {
        "experiment_name": "round8-ledger",
        "experiment_type": "research",
        "run_type": "committee",
        "evidence_boundary": "research_only",
        "idempotency_key": "round8-ledger-idempotency",
        "prompt_version": "v1",
        "context_fingerprint": "context-fingerprint",
        "model_routing": {"provider": "mock", "model": "fixture"},
        "parameters": {"symbol": "sh600001"},
    }
    first = recorder.start(**kwargs)
    replay = recorder.start(**kwargs)
    assert replay["run_id"] == first["run_id"]
    assert replay["idempotent"] is True
    with pytest.raises(ValueError, match="different frozen inputs"):
        recorder.start(**{**kwargs, "parameters": {"symbol": "sz000001"}})

    callbacks = 0

    def run_step():
        nonlocal callbacks
        callbacks += 1
        return {"answer": 42}

    completed = recorder.checkpointed_step(
        first["run_id"],
        step_name="llm_committee",
        signature="signature-v1",
        callback=run_step,
    )
    resumed = recorder.checkpointed_step(
        first["run_id"],
        step_name="llm_committee",
        signature="signature-v1",
        callback=run_step,
    )
    assert completed["resumed"] is False
    assert resumed["resumed"] is True
    assert callbacks == 1
    with pytest.raises(ValueError, match="callback was not executed"):
        recorder.checkpointed_step(
            first["run_id"],
            step_name="llm_committee",
            signature="signature-v2",
            callback=run_step,
        )
    assert callbacks == 1

    artifact = recorder.artifact(
        first["run_id"],
        artifact_type="decision",
        name="decision.json",
        payload={"action": "watch", "context": "context-fingerprint"},
    )
    assert len(artifact["fingerprint"]) == 64
    recorder.link(
        first["run_id"],
        entity_type="context_pack",
        entity_id="context-1",
        relation="input",
    )
    final = recorder.complete(
        first["run_id"], result_summary={"action": "watch"}
    )
    assert final["status"] == "completed"
    assert final["links"][0]["entity_id"] == "context-1"
    assert final["artifacts"][0]["fingerprint"] == artifact["fingerprint"]


def test_round8_repository_adversarial_edges_and_memory_scope(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round8Repository(path)
    repository.record_provider_selections(
        "refresh-1",
        {
            "calendar": {
                "selected_provider": "baostock",
                "reason": "priority",
                "related_failures": [],
                "attempts": [{"status": "available"}],
            }
        },
    )
    assert repository.provider_selections()[0]["selected_provider"] == "baostock"
    with pytest.raises(ValueError, match="invalid run terminal status"):
        repository.finish_run("missing", status="unknown")
    with pytest.raises(ValueError, match="not found"):
        repository.finish_run("missing", status="failed")
    with pytest.raises(ValueError, match="not found"):
        repository.link_entity(
            "missing", entity_type="context", entity_id="context", relation="input"
        )
    with pytest.raises(ValueError, match="not found"):
        repository.save_artifact(
            "missing", artifact_type="report", name="x", payload={}
        )
    with pytest.raises(ValueError, match="not found"):
        repository.save_checkpoint(
            "missing",
            step_name="x",
            checkpoint_signature="signature",
            payload={},
        )
    with pytest.raises(ValueError, match="3 to 7"):
        repository.create_thesis(
            {
                "portfolio_id": "p",
                "symbol": "sh600001",
                "initial_price": 1,
                "core_thesis": "x",
                "user_decision": "adopted",
                "assumptions": [],
            }
        )
    thesis = repository.create_thesis(
        {
            "portfolio_id": "p",
            "symbol": "sh600001",
            "recommendation_id": "recommendation-1",
            "initial_price": 10,
            "core_thesis": "x",
            "user_decision": "adopted",
            "assumptions": [
                {"statement": "a", "verification_metric": "a"},
                {"statement": "b", "verification_metric": "b"},
                {"statement": "c", "verification_metric": "c"},
            ],
        }
    )
    assert repository.create_thesis(
        {
            "portfolio_id": "p",
            "symbol": "sh600001",
            "recommendation_id": "recommendation-1",
            "initial_price": 10,
            "core_thesis": "ignored duplicate",
            "user_decision": "adopted",
            "assumptions": [
                {"statement": "a", "verification_metric": "a"},
                {"statement": "b", "verification_metric": "b"},
                {"statement": "c", "verification_metric": "c"},
            ],
        }
    )["thesis_id"] == thesis["thesis_id"]
    with pytest.raises(ValueError, match="not found"):
        repository.save_thesis_check(
            "missing", {"final_status": "active", "assumption_results": []}
        )
    with pytest.raises(ValueError, match="waiting_for_user_confirmation"):
        repository.save_thesis_check(
            thesis["thesis_id"],
            {"final_status": "invented", "assumption_results": []},
        )
    thesis = _freeze_test_thesis(repository, thesis)
    with pytest.raises(ValueError, match="invalid thesis status"):
        repository.save_thesis_check(
            thesis["thesis_id"],
            {"final_status": "invented", "assumption_results": []},
        )

    settings.values["learning"]["reflection_minimum_mature_samples"] = 1
    run, prediction = _authoritative_forward_fixture(
        settings, symbol="sh600001", realized_return_pct=1.0
    )
    with pytest.raises(ValueError, match="production or forward_shadow"):
        repository.save_reflection(
            {
                "run_id": run["run_id"],
                "source_type": "sample",
                "source_id": "invalid",
                "evidence_boundary": "demo",
                "horizon_days": 5,
                "due_at": datetime.now(UTC).isoformat(),
                "raw_return_pct": 0,
                "benchmark_return_pct": 0,
            }
        )
    with pytest.raises(ValueError, match="server authoritative"):
        repository.save_reflection(
            {
                "run_id": run["run_id"],
                "source_type": "forward_sample",
                "source_id": prediction["prediction_id"],
                "horizon_days": 5,
                "raw_return_pct": 99.0,
            }
        )
    reflection = record_outcome_reflection(
        settings,
        run_id=run["run_id"],
        source_type="forward_sample",
        source_id=prediction["prediction_id"],
        horizon_days=5,
        evidence_refs=[{"block_id": "a"}],
    )
    replay = record_outcome_reflection(
        settings,
        run_id=run["run_id"],
        source_type="forward_sample",
        source_id=prediction["prediction_id"],
        horizon_days=5,
        raw_return_pct=99.0,
    )
    assert replay["reflection_id"] == reflection["reflection_id"]
    assert replay["raw_return_pct"] == pytest.approx(1.0)
    with pytest.raises(ValueError, match="reflection not found"):
        repository.add_memory_candidates(
            "missing", symbol="sh600001", lessons=["x"]
        )
    second_run, second_prediction = _authoritative_forward_fixture(
        settings, symbol="sz000001", realized_return_pct=-1.0
    )
    second_reflection = record_outcome_reflection(
        settings,
        run_id=second_run["run_id"],
        source_type="forward_sample",
        source_id=second_prediction["prediction_id"],
        horizon_days=5,
    )
    assert second_reflection["memory_candidates"]
    memories = repository.memories("sh600001")
    cross = next(item for item in memories if item["symbol"] == "sz000001")
    assert cross["scope"] == "cross_symbol_low_weight"
    assert cross["weight"] <= 0.25


def test_acceptance_report_is_reproducible_and_fail_closed(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    report_dir = tmp_path / "data" / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "quality-gate-latest.json").write_text(
        json.dumps({"source_fingerprint": "source-v1"}), encoding="utf-8"
    )
    signature = checkpoint_signature(
        settings,
        workflow_structure="workflow-v1",
        model_routing={"provider": "mock"},
        prompt_version="prompt-v1",
        context_fingerprint="context-v1",
    )
    assert len(signature) == 64
    recorder = ExperimentRecorder(settings)
    failed_run = recorder.start(
        experiment_name="failed",
        experiment_type="research",
        run_type="fixture",
        evidence_boundary="test",
        idempotency_key="failed-run-idempotency",
    )
    assert recorder.fail(failed_run["run_id"], error_detail="fixture")["status"] == "failed"
    checkpoint_run = recorder.start(
        experiment_name="checkpoint",
        experiment_type="research",
        run_type="fixture",
        evidence_boundary="test",
        idempotency_key="checkpoint-run-idempotency",
    )
    assert recorder.checkpoint(
        checkpoint_run["run_id"],
        step_name="step",
        signature="signature",
        payload={"value": 1},
    )["resumed"] is False
    assert recorder.checkpoint(
        checkpoint_run["run_id"],
        step_name="step",
        signature="signature",
        payload={"value": 2},
    )["resumed"] is True

    path = settings.resolve(settings.get("system.database_path"))
    Round8Repository(path).record_provider_selections(
        "refresh-acceptance",
        {
            "calendar": {
                "selected_provider": "baostock",
                "reason": "priority",
                "related_failures": [],
                "attempts": [],
            }
        },
    )
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS background_jobs(idempotency_key TEXT);
            CREATE TABLE IF NOT EXISTS forward_ablation_samples(sample_fingerprint TEXT);
            CREATE TABLE IF NOT EXISTS forward_experiments(experiment_id TEXT);
            INSERT INTO background_jobs VALUES('duplicate-job');
            INSERT INTO background_jobs VALUES('duplicate-job');
            INSERT INTO forward_ablation_samples VALUES('duplicate-sample');
            INSERT INTO forward_ablation_samples VALUES('duplicate-sample');
            INSERT INTO forward_experiments VALUES('demo-fixture');
            """
        )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "start_allowed": False,
            "blockers": ["runtime_unavailable"],
            "data": {
                "calendar_day": {"trade_date": "2026-07-20", "is_open": True},
                "point_in_time_pool": {"total_members": 10},
                "source_states": {
                    "point_in_time_pool": {
                        "detail": {"required_field_coverage": {"industry": 0.95}}
                    }
                },
            },
        },
    )
    monkeypatch.setattr(
        "quantlab.runtime.readiness.formal_experiment_status",
        lambda *_args, **_kwargs: {
            "experiment": None,
            "formal_samples": 0,
            "shadow_trading_scorecard": {"accounts": []},
        },
    )
    monkeypatch.setattr(
        "quantlab.workflows.experiment_recorder._provider_refresh_acceptance_checks",
        lambda *_args, **_kwargs: {
            "provider_refresh_id": "refresh-acceptance",
            "provider_refresh_market_date": datetime.now(UTC).date().isoformat(),
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
    acceptance = next_trading_day_acceptance_report(
        settings, trade_date=datetime.now(UTC).date()
    )
    assert acceptance["status"] == "blocked"
    assert acceptance["checks"]["duplicate_jobs"] == 1
    assert acceptance["checks"]["duplicate_formal_samples"] == 0
    assert acceptance["checks"]["acceptance_schema_ready"] is False
    assert acceptance["checks"]["demo_pollution"] >= 1
    replay = next_trading_day_acceptance_report(
        settings, trade_date=datetime.now(UTC).date()
    )
    assert replay["report_id"] == acceptance["report_id"]
    monkeypatch.setattr(
        "quantlab.runtime.readiness.primary_start_readiness",
        lambda *_args, **_kwargs: {
            "start_allowed": False,
            "blockers": [],
            "data": {
                "calendar_day": {"trade_date": "2026-07-19", "is_open": False}
            },
        },
    )
    skipped = next_trading_day_acceptance_report(
        settings, trade_date=datetime.now(UTC).date() + timedelta(days=1)
    )
    assert skipped["status"] == "skipped_non_trading_day"


def _thesis(repository: Round8Repository, *, symbol: str = "sh600001") -> dict:
    thesis = repository.create_thesis(
        {
            "portfolio_id": "portfolio-1",
            "symbol": symbol,
            "initial_price": 10.0,
            "core_thesis": "The thesis remains valid only while frozen assumptions hold.",
            "supporting_evidence": ["context:technical"],
            "opposing_evidence": ["context:risk"],
            "red_lines": ["regulatory_event"],
            "invalidation_conditions": ["regulatory_event"],
            "user_decision": "adopted",
            "assumptions": [
                {"statement": "earnings hold", "verification_metric": "earnings"},
                {"statement": "risk is bounded", "verification_metric": "risk"},
                {"statement": "red line absent", "verification_metric": "events"},
            ],
        }
    )
    return _freeze_test_thesis(repository, thesis)


def test_thesis_red_line_overrides_price_and_missing_evidence_stays_review(tmp_path):
    settings = _settings(tmp_path)
    repository = Round8Repository(settings.resolve(settings.get("system.database_path")))
    thesis = _thesis(repository)
    assumption_id = thesis["assumptions"][0]["assumption_id"]
    pack = _context_pack(
        "sh600001",
        as_of=date.today(),
        current_price=11.5,
        red_line=True,
        cutoff_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(pack)
    checked = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        trigger_type="regulatory_event",
        evidence_refs=[
            {
                "assumption_id": assumption_id,
                "context_id": pack.context_id,
                "block_id": pack.block(EvidenceDomain.EVENT).block_id,
            }
        ],
    )
    assert checked["final_status"] in {"damaged", "broken"}
    assert checked["price_change_pct"] == pytest.approx(15.0)
    updated = repository.thesis(thesis["thesis_id"])
    assert updated is not None
    assert updated["assumptions"][0]["status"] == "contradicted"
    assert any(
        item["status"] == "needs_review" for item in updated["assumptions"][1:]
    )

    second = _thesis(repository, symbol="sz000001")
    unavailable = check_investment_thesis(
        settings,
        thesis_id=second["thesis_id"],
        context_id="context-2",
        context_fingerprint="fingerprint-2",
        trigger_type="scheduled_review",
        evidence_refs=[],
    )
    assert unavailable["proposed_status"] == "unchanged"
    assert len(unavailable["unavailable_reasons"]) == 3
    assert all(
        item["status"] == "needs_review"
        for item in unavailable["assumption_results"]
    )

    registry = ChatToolRegistry(settings, {"portfolio_id": "portfolio-1"})
    listed = registry.execute("query_investment_theses", {})
    assert {item["thesis_id"] for item in listed["theses"]} >= {
        thesis["thesis_id"],
        second["thesis_id"],
    }
    assert registry.execute(
        "query_investment_thesis", {"thesis_id": thesis["thesis_id"]}
    )["thesis_id"] == thesis["thesis_id"]
    memory = registry.execute("query_research_memory", {"symbol": "sh600001"})
    assert memory["symbol"] == "sh600001"
    with pytest.raises(ValueError, match="not found"):
        registry.execute("query_investment_thesis", {"thesis_id": "missing"})


def test_reflection_requires_mature_completed_formal_run(tmp_path):
    settings = _settings(tmp_path)
    recorder = ExperimentRecorder(settings)
    incomplete = recorder.start(
        experiment_name="forward-reflection-incomplete",
        experiment_type="forward",
        run_type="prediction",
        evidence_boundary="forward_shadow",
        idempotency_key="forward-reflection-incomplete-run",
    )
    with pytest.raises(ValueError, match="completed"):
        record_outcome_reflection(
            settings,
            run_id=incomplete["run_id"],
            source_type="forward_sample",
            source_id="missing",
            horizon_days=5,
        )
    recorder.complete(incomplete["run_id"], result_summary={"frozen": True})
    with pytest.raises(ValueError, match="unavailable|not found"):
        record_outcome_reflection(
            settings,
            run_id=incomplete["run_id"],
            source_type="forward_sample",
            source_id="missing",
            horizon_days=5,
        )
    run, prediction = _authoritative_forward_fixture(
        settings, symbol="sh600001", realized_return_pct=2.0
    )
    wrong_run = recorder.start(
        experiment_name="unlinked-forward-reflection",
        experiment_type="forward",
        run_type="settlement",
        evidence_boundary="forward_shadow",
        idempotency_key="unlinked-forward-reflection",
        context_fingerprint="different-context",
    )
    recorder.complete(wrong_run["run_id"], result_summary={})
    with pytest.raises(ValueError, match="not linked"):
        record_outcome_reflection(
            settings,
            run_id=wrong_run["run_id"],
            source_type="forward_sample",
            source_id=prediction["prediction_id"],
            horizon_days=5,
        )
    reflection = record_outcome_reflection(
        settings,
        run_id=run["run_id"],
        source_type="forward_sample",
        source_id=prediction["prediction_id"],
        horizon_days=5,
        raw_return_pct=99.0,
        benchmark_return_pct=99.0,
        due_at=datetime.now(UTC) + timedelta(days=365),
        supporting_evidence_results=[{"correct": True, "confidence": 0.7}],
        evidence_refs=[{"block_id": "technical:1"}],
    )
    assert reflection["raw_return_pct"] == pytest.approx(2.0)
    assert reflection["memory_candidates"]
    assert reflection["memory_candidates"][0]["challenge_eligible"] == 0
    replay = record_outcome_reflection(
        settings,
        run_id=run["run_id"],
        source_type="forward_sample",
        source_id=prediction["prediction_id"],
        horizon_days=5,
    )
    assert replay["reflection_id"] == reflection["reflection_id"]


def test_reflection_rejects_future_due_time_and_non_authoritative_settlement(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    run, prediction = _authoritative_forward_fixture(
        settings, symbol="sh600003", realized_return_pct=1.0
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE forward_ablation_predictions SET due_at=? WHERE prediction_id=?",
            (
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                prediction["prediction_id"],
            ),
        )
    with pytest.raises(ValueError, match="frozen due time"):
        record_outcome_reflection(
            settings,
            run_id=run["run_id"],
            source_type="forward_sample",
            source_id=prediction["prediction_id"],
            horizon_days=5,
        )

    second_run, second_prediction = _authoritative_forward_fixture(
        settings, symbol="sh600004", realized_return_pct=1.0
    )
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT payload FROM forward_ablation_outcomes WHERE prediction_id=?",
            (second_prediction["prediction_id"],),
        ).fetchone()
        payload = json.loads(row[0])
        payload["quote"]["authoritative"] = False
        payload["quote"]["evidence_stage"] = "test"
        db.execute(
            "UPDATE forward_ablation_outcomes SET payload=? WHERE prediction_id=?",
            (json.dumps(payload), second_prediction["prediction_id"]),
        )
    with pytest.raises(ValueError, match="non-authoritative"):
        record_outcome_reflection(
            settings,
            run_id=second_run["run_id"],
            source_type="forward_sample",
            source_id=second_prediction["prediction_id"],
            horizon_days=5,
        )


def test_authoritative_reflection_worker_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    _authoritative_forward_fixture(
        settings, symbol="sh600005", realized_return_pct=1.5
    )
    first = authoritative_reflection_settlement(settings)
    second = authoritative_reflection_settlement(settings)
    assert first["settled"] == 1
    assert second["settled"] == 0
    assert second["candidates"] == 0


def test_soak_counts_only_actual_selected_provider_changes(tmp_path):
    settings = _settings(tmp_path)
    repository = Round7Repository(settings.resolve(settings.get("system.database_path")))
    started = datetime.now(UTC) - timedelta(minutes=2)
    repository.save_soak_observation(
        {
            "processes": {},
            "provider_states": [
                {"provider": "provider-a", "status": "available"},
                {"provider": "provider-b", "status": "available"},
            ],
            "provider_selections": [
                {
                    "component": "current_spot",
                    "selected_provider": "provider-a",
                    "selection_reason": "priority",
                }
            ],
        },
        observed_at=started,
    )
    repository.save_soak_observation(
        {
            "processes": {},
            "provider_states": [
                {"provider": "provider-a", "status": "available"},
                {"provider": "provider-b", "status": "available"},
            ],
            "provider_selections": [
                {
                    "component": "current_spot",
                    "selected_provider": "provider-a",
                    "selection_reason": "priority",
                }
            ],
        },
        observed_at=started + timedelta(minutes=1),
    )
    before_switch = soak_report(settings)
    assert before_switch["provider_switches"] == 0

    repository.save_soak_observation(
        {
            "processes": {},
            "provider_selections": [
                {
                    "component": "current_spot",
                    "selected_provider": "provider-b",
                    "selection_reason": "fallback_after_failure",
                    "related_failures": [{"provider": "provider-a"}],
                }
            ],
        },
        observed_at=started + timedelta(minutes=2),
    )
    after_switch = soak_report(settings)
    assert after_switch["provider_switches"] == 1
    assert after_switch["provider_switch_events"][0]["from_provider"] == "provider-a"
    assert after_switch["provider_switch_events"][0]["to_provider"] == "provider-b"
