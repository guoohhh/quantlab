from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from dashboard.ui_foundation import (
    bind_product_research_context,
    cache_research_report,
    cached_research_report,
    mark_research_failed,
    mark_research_loading,
    product_context,
    research_request_state,
    restore_previous_research_report,
)
from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.domain import ResearchProvenance
from quantlab.domain.models import AssetType
from quantlab.domain.trading import DataQuality, MarketQuote
from quantlab.learning import LearningRepository
from quantlab.learning.trainer import train_registered_model
from quantlab.llm import MockLLMProvider
from quantlab.persistence import DecisionRepository
from quantlab.persistence.migrations import (
    ensure_database_initialized,
    initialize_or_upgrade_database,
)
from quantlab.persistence.round5 import Round5Repository
from quantlab.reporting import build_stored_audit_package
from quantlab.workflows.chat import create_chat_conversation
from quantlab.workflows.research_identity import validate_research_record
from quantlab.workflows.simulator import create_user_paper_account, run_pretrade_check


def _business_day() -> date:
    value = date.today()
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def _run(symbol: str, as_of: date):
    return asyncio.run(
        MultiAgentDecisionSystem(MockLLMProvider()).run(
            ResearchContext(symbol=symbol, as_of=as_of, price=10.0)
        )
    )


def _context(requested: date, effective: date) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "price_history": {"requested_cutoff_date": requested.isoformat()},
        "data": {
            "requested_as_of": requested.isoformat(),
            "effective_as_of": effective.isoformat(),
            "source": "test_fixture",
        },
        "analysis_context_pack": {
            "symbol": "sh600519",
            "as_of": effective.isoformat(),
        },
        "evidence_stage": "research_only",
    }


def _quote(symbol: str, as_of: date) -> MarketQuote:
    observed = datetime.combine(as_of, time(2, 0), tzinfo=UTC)
    return MarketQuote(
        symbol=symbol,
        name="trust closure fixture",
        asset_type=AssetType.STOCK,
        raw_price=10.0,
        as_of=as_of,
        available_at=observed,
        source="server_test_quote",
        data_quality=DataQuality.AVAILABLE,
        authoritative=False,
        industry="manufacturing",
        trade_lot=100,
        t_plus_one=True,
        session_status="open",
        risk_metadata={
            "risk_check_complete": True,
            "financial_check_complete": True,
            "financial_quality_score": 0.8,
            "listing_days": 1000,
        },
    )


def _registered_forward(path, symbol: str, trade_date: date) -> str:
    initialize_or_upgrade_database(path)
    repository = Round5Repository(path)
    experiment = repository.create_experiment(
        protocol_version=f"test-{trade_date.isoformat()}",
        cohort_id=f"cohort-{trade_date.isoformat()}",
        frozen_payload={
            "asset_scope": ["stock"],
            "daily_sampling_rule": "pre_registered_test_fixture",
            "candidate_count": 1,
            "horizons": [5, 20],
            "model_version": "fixture-v1",
            "prompt_version": "fixture-v1",
            "strategy_version": "fixture-v1",
            "governance_version": "fixture-v1",
            "initial_capital": 100000.0,
            "cost_rules": {},
            "matching_rules": {},
            "missing_data_rule": "fail_closed",
            "minimum_trust_level": "server_observed",
            "upgrade_conditions": [],
            "stop_conditions": [],
        },
    )
    registration = repository.begin_registration(
        experiment,
        trade_date,
        pool_snapshot_id=None,
        pool_fingerprint="fixture-pool",
        manifest_id=None,
    )
    for ordinal, horizon in enumerate((5, 20), start=1):
        repository.record_registration_sample(
            registration,
            symbol=symbol,
            horizon_days=horizon,
            ordinal=ordinal,
            status="registered",
            sample_key=f"registered:{symbol}:{trade_date}:{horizon}",
            context_fingerprint="fixture-context",
        )
    completed = repository.finish_registration(
        registration["registration_id"],
        status="completed",
        expected=2,
        registered=2,
        failed=0,
        skipped=0,
    )
    return str(completed["registration_id"])


def test_past_interactive_research_is_never_settlement_or_training_eligible(tmp_path):
    requested = _business_day() - timedelta(days=30)
    run = _run("sh600519", requested)
    repository = DecisionRepository(tmp_path / "quantlab.db")
    repository.save(
        run,
        _context(requested, requested),
        provenance=ResearchProvenance(
            origin="user_interactive_research", requested_as_of=requested
        ),
    )

    record = repository.get(run.run_id)
    sample = LearningRepository(repository.path).get_sample(f"live:{run.run_id}:5")
    trained = train_registered_model(
        LearningRepository(repository.path), 5, "stock", minimum_samples=1
    )

    assert record["origin"] == "historical_research"
    assert record["settlement_eligible"] == 0
    assert record["training_eligible"] == 0
    assert repository.pending_forecasts() == []
    assert sample["training_eligible"] == 0
    assert trained["status"] == "insufficient_samples"


def test_interactive_research_remains_linkable_to_chat_and_pretrade(settings):
    settings = settings.with_overrides(
        {
            "risk": {
                "max_single_position": 0.30,
                "max_total_exposure": 0.90,
                "max_industry_exposure": 0.40,
            },
            "costs": {
                "stock": {
                    "commission_rate": 0.00025,
                    "minimum_commission": 5.0,
                    "stamp_duty_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                    "slippage_bps": 10.0,
                    "stop_slippage_bps": 25.0,
                    "trade_lot": 100,
                }
            },
        }
    )
    trade_date = _business_day()
    run = _run("sh600519", trade_date)
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    repository.save(
        run,
        _context(trade_date, trade_date),
        provenance=ResearchProvenance(requested_as_of=trade_date),
    )
    account = create_user_paper_account(
        settings, name="interactive research", idempotency_key="interactive-research-account"
    )
    conversation = create_chat_conversation(
        settings,
        title="interactive research",
        account_id=account["account_id"],
        symbol="sh600519",
        research_run_id=run.run_id,
        idempotency_key="interactive-research-chat",
    )
    check = run_pretrade_check(
        settings,
        account_id=account["account_id"],
        symbol="sh600519",
        side="buy",
        quantity=100,
        quote=_quote("sh600519", trade_date),
        research_run_id=run.run_id,
        requested_at=datetime.combine(trade_date, time(2, 0), tzinfo=UTC),
    )

    assert conversation["research_run_id"] == run.run_id
    assert check["research_run_id"] == run.run_id
    assert check["research_link_status"] == "linked"
    assert repository.get(run.run_id)["settlement_eligible"] == 0


def test_registered_forward_research_is_admitted_and_can_settle(tmp_path):
    trade_date = _business_day()
    path = tmp_path / "quantlab.db"
    registration_id = _registered_forward(path, "sh600519", trade_date)
    run = _run("sh600519", trade_date)
    repository = DecisionRepository(path)
    repository.save(
        run,
        _context(trade_date, trade_date),
        provenance=ResearchProvenance(
            origin="registered_forward_research",
            requested_as_of=trade_date,
            registration_id=registration_id,
        ),
    )

    assert len(repository.pending_forecasts()) == 2
    outcome = repository.record_forecast_outcome(
        run.run_id, 5, 2.0, (trade_date + timedelta(days=7)).isoformat()
    )
    sample = LearningRepository(path).get_sample(f"live:{run.run_id}:5")
    assert outcome.outcome == "up"
    assert sample["training_eligible"] == 1
    assert sample["outcome"] == "up"


@pytest.mark.parametrize(
    "origin",
    [
        "user_interactive_research",
        "historical_research",
        "strategy_shadow_research",
        "demo_research",
        "test_research",
        "legacy_unclassified",
    ],
)
def test_unqualified_origins_never_enter_settlement(origin, tmp_path):
    trade_date = _business_day()
    run = _run("sh600519", trade_date)
    repository = DecisionRepository(tmp_path / f"{origin}.db")
    repository.save(
        run,
        _context(trade_date, trade_date),
        provenance=ResearchProvenance(
            origin=origin, requested_as_of=trade_date, evidence_stage="research_only"
        ),
    )
    assert repository.pending_forecasts() == []
    with pytest.raises(ValueError, match="not eligible"):
        repository.record_forecast_outcome(
            run.run_id, 5, 1.0, (trade_date + timedelta(days=7)).isoformat()
        )


def test_legacy_pending_and_live_samples_are_quarantined_without_guessing(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE decision_runs(run_id TEXT PRIMARY KEY,symbol TEXT NOT NULL,
              as_of TEXT NOT NULL,action TEXT NOT NULL,confidence REAL NOT NULL,
              payload TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE forecast_predictions(run_id TEXT NOT NULL,symbol TEXT NOT NULL,
              as_of TEXT NOT NULL,horizon_days INTEGER NOT NULL,model TEXT NOT NULL,
              up_probability REAL NOT NULL,flat_probability REAL NOT NULL,
              down_probability REAL NOT NULL,confidence REAL NOT NULL,
              PRIMARY KEY(run_id,horizon_days));
            CREATE TABLE learning_samples(sample_key TEXT PRIMARY KEY,run_id TEXT,source TEXT NOT NULL,
              asset_scope TEXT NOT NULL DEFAULT 'unknown',symbol TEXT NOT NULL,as_of TEXT NOT NULL,
              horizon_days INTEGER NOT NULL,features_json TEXT NOT NULL,expected_return_pct REAL,
              outcome TEXT,realized_return_pct REAL,evaluated_at TEXT,context_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            """
        )
        db.execute(
            "INSERT INTO decision_runs(run_id,symbol,as_of,action,confidence,payload) VALUES(?,?,?,?,?,?)",
            ("legacy-run", "sh600519", "2026-01-02", "hold", 0.5, "{}"),
        )
        db.execute(
            "INSERT INTO forecast_predictions VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy-run", "sh600519", "2026-01-02", 5, "legacy", 0.4, 0.3, 0.3, 0.5),
        )
        db.execute(
            "INSERT INTO learning_samples(sample_key,run_id,source,asset_scope,symbol,as_of,horizon_days,features_json,context_json) VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy-live", "legacy-run", "live_decision", "stock", "sh600519", "2026-01-02", 5, "{}", "{}"),
        )

    result = initialize_or_upgrade_database(path)
    record = DecisionRepository(path).get("legacy-run")
    sample = LearningRepository(path).get_sample("legacy-live")

    assert result["pre_upgrade_backup"] is not None
    assert record["origin"] == "legacy_unclassified"
    assert record["requested_as_of"] is None
    assert record["quarantine_reason"] == "legacy_origin_unproven"
    assert sample["training_eligible"] == 0
    assert DecisionRepository(path).pending_forecasts() == []


def test_requested_and_effective_identity_survives_repository_restart(tmp_path):
    requested = _business_day()
    effective = requested - timedelta(days=1)
    while effective.weekday() >= 5:
        effective -= timedelta(days=1)
    run = _run("sh600519", effective)
    path = tmp_path / "identity.db"
    DecisionRepository(path).save(
        run,
        _context(requested, effective),
        provenance=ResearchProvenance(requested_as_of=requested),
    )

    record = DecisionRepository(path).get(run.run_id)
    package = build_stored_audit_package(record)
    assert record["requested_as_of"] == requested.isoformat()
    assert record["effective_as_of"] == effective.isoformat()
    assert package["research_identity"]["requested_as_of"] == requested.isoformat()


def test_failed_generation_state_never_replays_cache_without_explicit_restore():
    state: dict = {}
    requested = _business_day()
    report = {
        "run_id": "successful-run",
        "symbol": "sh600519",
        "as_of": requested.isoformat(),
        "data": {"effective_as_of": requested.isoformat()},
        "research_identity": {
            "run_id": "successful-run",
            "symbol": "sh600519",
            "requested_as_of": requested.isoformat(),
            "effective_as_of": requested.isoformat(),
            "origin": "user_interactive_research",
            "evidence_stage": "research_only",
        },
    }
    identity = cache_research_report(
        state, report, symbol="sh600519", requested_as_of=requested
    )
    bind_product_research_context(state, identity)
    mark_research_loading(state, symbol="sh600519", requested_as_of=requested)
    mark_research_failed(state, symbol="sh600519", requested_as_of=requested)

    assert research_request_state(
        state, symbol="sh600519", requested_as_of=requested
    ) == "failed"
    assert cached_research_report(
        state, symbol="sh600519", requested_as_of=requested
    ) is None
    assert product_context(state).research_run_id is None

    restored = restore_previous_research_report(
        state, symbol="sh600519", requested_as_of=requested
    )
    assert restored["run_id"] == "successful-run"
    assert research_request_state(
        state, symbol="sh600519", requested_as_of=requested
    ) == "success"


def test_unified_entrypoint_backs_up_old_database_before_upgrade(tmp_path):
    path = tmp_path / "entrypoint-old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user_data(value TEXT)")
        db.execute("INSERT INTO user_data(value) VALUES('preserve-me')")
    result = ensure_database_initialized(path)
    backup = result["pre_upgrade_backup"]
    assert backup is not None
    assert backup["integrity_check"] == "ok"
    assert backup["foreign_key_violations"] == []
    assert hashlib.sha256(Path(backup["database"]).read_bytes()).hexdigest() == backup[
        "sha256"
    ]


def test_migration_failure_never_publishes_partial_database(tmp_path, monkeypatch):
    path = tmp_path / "migration-failure.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user_data(value TEXT)")
        db.execute("INSERT INTO user_data(value) VALUES('unchanged')")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    def fail(_path):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(
        "quantlab.persistence.decision_learning.apply_decision_learning_schema", fail
    )
    with pytest.raises(RuntimeError, match="injected migration failure"):
        initialize_or_upgrade_database(path)

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM user_data").fetchone()[0] == "unchanged"


def test_matching_persisted_identity_validates_context_and_origin(tmp_path):
    trade_date = _business_day()
    run = _run("sh600519", trade_date)
    repository = DecisionRepository(tmp_path / "identity-validate.db")
    repository.save(
        run,
        _context(trade_date, trade_date),
        provenance=ResearchProvenance(requested_as_of=trade_date),
    )
    identity = validate_research_record(
        repository.get(run.run_id), run_id=run.run_id, symbol="sh600519"
    )
    assert identity["requested_as_of"] == trade_date
    assert identity["effective_as_of"] == trade_date
    assert identity["origin"] in {
        "user_interactive_research",
        "historical_research",
    }
    assert identity["training_eligible"] is False
