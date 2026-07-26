from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    AssetType,
    EvidenceBlock,
    EvidenceDomain,
    EvidenceQuality,
)
from quantlab.domain.data_governance import DataNamespace, DataTrustLevel
from quantlab.domain.strategy_evidence import (
    EvidenceStage,
    PointInTimePoolMember,
    PointInTimePoolSnapshot,
)
from quantlab.market import TradingCalendarService
from quantlab.persistence import EvidenceRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round6 import Round6Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.persistence.migrations import initialize_or_upgrade_database
from quantlab.persistence.simulator import UserPaperTradingRepository
from quantlab.persistence.strategy_evidence import StrategyEvidenceRepository
from quantlab.workflows.decision_lifecycle import (
    thesis_due_scan,
    thesis_event_check,
    thesis_price_invalidation_check,
)
from quantlab.workflows.decision_tasks import refresh_decision_tasks
from quantlab.workflows.experiment_recorder import _provider_refresh_acceptance_checks
from quantlab.workflows.investment_thesis import (
    ThesisAssumptionDraft,
    active_investment_theses,
    check_investment_thesis,
    edit_investment_thesis_draft,
    freeze_investment_thesis_revision,
)
from quantlab.workflows.investor_portfolio import record_recommendation_adoption


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path) -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
                "allow_mock_fallback": True,
                "maximum_task_cost_usd": 1.0,
            },
            "learning": {"reflection_minimum_mature_samples": 30},
            "strategies": {
                "forward_primary": {"candidate_count": 1},
                "a_share_v4": {"protocol_version": "round9-tail-test"},
                "etf_rotation": {"universe": ["sh510300"]},
            },
            "costs": {"stock": {"trade_lot": 100}, "etf": {"trade_lot": 100}},
            "risk": {"max_single_position": 0.15},
        },
        root=PROJECT_ROOT,
    )


def _pending_thesis(
    settings: Settings,
    *,
    symbol: str = "sh600001",
    next_check_at: str | None = None,
) -> dict:
    repository = Round8Repository(settings.resolve(settings.get("system.database_path")))
    return repository.create_thesis(
        {
            "portfolio_id": "portfolio-round9-tail",
            "symbol": symbol,
            "initial_price": 10.0,
            "core_thesis": "This placeholder thesis must not become active before user confirmation.",
            "user_decision": "adopted",
            "next_check_at": next_check_at,
            "red_lines": ["material regulatory event"],
            "invalidation_conditions": ["facts no longer support the thesis"],
            "assumptions": [
                {"statement": "earnings remain sound", "verification_metric": "earnings"},
                {"statement": "risk remains bounded", "verification_metric": "risk"},
                {"statement": "red lines remain absent", "verification_metric": "events"},
            ],
        }
    )


def _draft(
    core_logic: str,
    *,
    due_at: str | None,
    frequencies: tuple[str, ...] = ("weekly", "weekly", "weekly"),
) -> dict:
    return {
        "core_logic": core_logic,
        "assumptions": [
            {
                "statement": f"Frozen assumption {index} remains verifiable from current evidence.",
                "verification_metric": f"metric-{index}",
                "current_evidence": [],
                "supporting_evidence_refs": [f"support-{index}"],
                "opposing_evidence_refs": [f"oppose-{index}"],
                "check_frequency": frequency,
                "next_check_at": due_at if frequency not in {"event_driven", "manual"} else None,
                "red_lines": [f"red-line-{index}"],
                "invalidation_conditions": [f"invalid-{index}"],
            }
            for index, frequency in enumerate(frequencies, start=1)
        ],
        "valuation_anchor": "The observed price is an audit anchor and never a return guarantee.",
        "overall_red_lines": ["material adverse event"],
        "overall_invalidation_conditions": ["verified facts no longer support the thesis"],
        "data_provenance": {
            "boundary": "research_only",
            "test_only": True,
            "source": "isolated_round9_tail_fixture",
        },
        "needs_review": True,
    }


def _freeze(
    settings: Settings,
    thesis: dict,
    *,
    core_logic: str,
    due_at: str | None,
    frequencies: tuple[str, ...] = ("weekly", "weekly", "weekly"),
) -> tuple[dict, dict]:
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    revision = repository.create_thesis_revision(
        thesis["thesis_id"],
        payload={
            "schema_version": "round9-tail-test",
            **_draft(core_logic, due_at=due_at, frequencies=frequencies),
        },
        source="test_fixture",
        edited_by="test",
    )
    repository.freeze_thesis_revision(revision["revision_id"], thesis_id=thesis["thesis_id"])
    return Round8Repository(repository.path).thesis(thesis["thesis_id"]), revision


def _context_pack(
    thesis: dict,
    *,
    red_line: bool = False,
    observed_at: datetime | None = None,
) -> AnalysisContextPack:
    observed = observed_at or datetime.now(UTC) - timedelta(minutes=1)
    event_block = EvidenceBlock(
        block_id=f"event:{thesis['symbol']}:{uuid.uuid4()}",
        domain=EvidenceDomain.EVENT,
        title="authoritative thesis evaluations",
        source="server_observed_fixture",
        methodology="structured thesis evaluation",
        as_of=observed,
        available_at=observed,
        fetched_at=observed,
        quality=EvidenceQuality.AVAILABLE,
        payload={
            "thesis_evaluations": [
                {
                    "assumption_id": item["assumption_id"],
                    **(
                        {"red_line": True, "reason": "material adverse event"}
                        if red_line
                        else {"status": "supported"}
                    ),
                }
                for item in thesis["assumptions"]
            ]
        },
    )
    market_block = EvidenceBlock(
        block_id=f"market:{thesis['symbol']}:{uuid.uuid4()}",
        domain=EvidenceDomain.MARKET,
        title="authoritative market price",
        source="server_observed_fixture",
        methodology="stored close",
        as_of=observed,
        available_at=observed,
        fetched_at=observed,
        quality=EvidenceQuality.AVAILABLE,
        payload={"current_raw_price": 11.0},
    )
    return AnalysisContextPack(
        symbol=thesis["symbol"],
        asset_type=AssetType.STOCK,
        as_of=observed.date(),
        cutoff_at=observed,
        generated_at=observed,
        blocks=[market_block, event_block],
        deterministic_summary={"test_boundary": True},
    )


def _save_and_check(
    settings: Settings,
    thesis: dict,
    *,
    red_line: bool = False,
    observed_at: datetime | None = None,
) -> dict:
    pack = _context_pack(thesis, red_line=red_line, observed_at=observed_at)
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(pack)
    event = pack.block(EvidenceDomain.EVENT)
    return check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        trigger_type="round9_tail_test",
        evidence_refs=[
            {"assumption_id": item["assumption_id"], "block_id": event.block_id}
            for item in thesis["assumptions"]
        ],
        user_resolution="system_verified",
    )


def _seed_calendar(settings: Settings, start: date, *, days: int = 100) -> None:
    observed = datetime.now(UTC) - timedelta(minutes=2)
    records = [
        {
            "trade_date": (start + timedelta(days=offset)).isoformat(),
            "is_open": (start + timedelta(days=offset)).weekday() < 5,
        }
        for offset in range(days + 1)
    ]
    TradingCalendarService.from_settings(settings).ingest(
        records,
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        provider="server_calendar_fixture",
        source="isolated_test_calendar",
        endpoint="fixture://calendar",
        source_version="fixture-v1",
        available_at=observed,
        license_status="test_fixture",
        raw_fingerprint=hashlib.sha256(json.dumps(records).encode()).hexdigest(),
    )


def test_pending_thesis_is_excluded_until_freeze_and_checks_keep_old_revision(tmp_path):
    settings = _settings(tmp_path)
    today = date.today().isoformat()
    pending = _pending_thesis(settings, next_check_at=today)
    assert pending["status"] == "draft_pending_confirmation"
    assert active_investment_theses(settings) == []
    assert thesis_due_scan(settings, as_of=date.today())["tasks"] == []
    assert thesis_event_check(settings, as_of=date.today())["checked"] == 0
    assert thesis_price_invalidation_check(settings, as_of=date.today())["checked"] == 0
    assert check_investment_thesis(
        settings,
        thesis_id=pending["thesis_id"],
        context_id=None,
        context_fingerprint=None,
        evidence_refs=[],
    )["status"] == "waiting_for_user_confirmation"

    frozen, first_revision = _freeze(
        settings,
        pending,
        core_logic="The first user-confirmed thesis is now the only effective thesis.",
        due_at=today,
    )
    assert frozen["status"] == "active"
    assert frozen["core_thesis"] == "The first user-confirmed thesis is now the only effective thesis."
    assert frozen["current_frozen_revision"]["revision_id"] == first_revision["revision_id"]
    assert all(item["active_revision_id"] == first_revision["revision_id"] for item in frozen["assumptions"])

    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    draft = repository.create_thesis_revision(
        frozen["thesis_id"],
        payload={
            "schema_version": "round9-tail-test",
            **_draft(
                "A later draft must not affect the currently frozen thesis before confirmation.",
                due_at=today,
            ),
        },
        source="user_edit",
        edited_by="user",
    )
    assert Round8Repository(repository.path).thesis(frozen["thesis_id"])["core_thesis"] == frozen[
        "core_thesis"
    ]
    first_check = _save_and_check(settings, frozen)
    repository.freeze_thesis_revision(draft["revision_id"], thesis_id=frozen["thesis_id"])
    updated = Round8Repository(repository.path).thesis(frozen["thesis_id"])
    assert updated["core_thesis"].startswith("A later draft")
    assert first_check["frozen_revision_id"] == first_revision["revision_id"]
    assert first_check["frozen_revision_fingerprint"] == first_revision["fingerprint"]
    assert repository.thesis_revisions(frozen["thesis_id"])[0]["status"] == "superseded"


def test_successful_check_advances_trading_schedule_and_failures_do_not(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    pending = _pending_thesis(settings)
    thesis, _ = _freeze(
        settings,
        pending,
        core_logic="Weekly, event-driven and manual assumptions retain distinct schedules.",
        due_at=today.isoformat(),
        frequencies=("weekly", "event_driven", "manual"),
    )
    checked = _save_and_check(settings, thesis)
    calendar = TradingCalendarService.from_settings(settings)
    weekly_due = calendar.add_open_days(today, 5, formal=True).isoformat()
    assert checked["schedule_status"] == "advanced"
    assert checked["schedule_update_applied"] == 1
    refreshed = Round8Repository(calendar.repository.path).thesis(thesis["thesis_id"])
    by_frequency = {item["check_frequency"]: item for item in refreshed["assumptions"]}
    assert by_frequency["weekly"]["next_check_at"] == weekly_due
    assert by_frequency["event_driven"]["next_check_at"] is None
    assert by_frequency["manual"]["next_check_at"] is None
    assert refreshed["next_check_at"] == weekly_due

    failed_pending = _pending_thesis(settings, symbol="sh600002")
    failed, _ = _freeze(
        settings,
        failed_pending,
        core_logic="A missing ContextPack must never advance the frozen schedule.",
        due_at=today.isoformat(),
    )
    unavailable = check_investment_thesis(
        settings,
        thesis_id=failed["thesis_id"],
        context_id="missing-context",
        context_fingerprint="missing-fingerprint",
        evidence_refs=[],
        user_resolution="system_verified",
    )
    assert unavailable["schedule_update_applied"] == 0
    assert Round8Repository(calendar.repository.path).thesis(failed["thesis_id"])[
        "next_check_at"
    ] == today.isoformat()

    red_pending = _pending_thesis(settings, symbol="sh600003")
    red_thesis, _ = _freeze(
        settings,
        red_pending,
        core_logic="A red line must force a near-term review instead of a distant schedule.",
        due_at=today.isoformat(),
    )
    red_check = _save_and_check(settings, red_thesis, red_line=True)
    assert red_check["final_status"] in {"damaged", "broken"}
    assert red_check["schedule_status"] == "red_line_near_term"
    assert red_check["next_check_at"] == calendar.add_open_days(today, 1, formal=True).isoformat()


def test_due_scan_is_idempotent_and_successful_check_resolves_old_task(tmp_path):
    settings = _settings(tmp_path)
    today = date.today()
    _seed_calendar(settings, today - timedelta(days=1))
    pending = _pending_thesis(settings)
    thesis, _ = _freeze(
        settings,
        pending,
        core_logic="A due review has one identity until its frozen schedule advances.",
        due_at=today.isoformat(),
    )
    first = thesis_due_scan(settings, as_of=today)
    replay = thesis_due_scan(settings, as_of=today)
    assert len(first["tasks"]) == 1
    assert replay["tasks"][0]["task_id"] == first["tasks"][0]["task_id"]

    checked = _save_and_check(settings, thesis)
    assert checked["schedule_update_applied"] == 1
    tasks = Round9Repository(settings.resolve(settings.get("system.database_path"))).decision_tasks()
    due_task = next(item for item in tasks if item["task_type"] == "investment_thesis_due_review")
    assert due_task["status"] == "resolved"
    assert due_task["resolved_reason"] == "thesis_check_completed"
    tomorrow = today + timedelta(days=1)
    assert thesis_due_scan(settings, as_of=tomorrow)["tasks"] == []


def test_context_without_effective_evidence_does_not_advance_or_close_due_task(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="An existing ContextPack is not evidence unless it verifies an assumption.",
        due_at=today.isoformat(),
    )
    due_task = thesis_due_scan(settings, as_of=today)["tasks"][0]
    pack = _context_pack(thesis)
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(pack)

    empty = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        evidence_refs=[],
        user_resolution="system_verified",
    )
    assert empty["schedule_update_applied"] == 0
    assert empty["next_check_at"] == today.isoformat()
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    assert next(
        item for item in repository.decision_tasks() if item["task_id"] == due_task["task_id"]
    )["status"] == "open"

    market = pack.block(EvidenceDomain.MARKET)
    neutral = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        evidence_refs=[
            {"assumption_id": item["assumption_id"], "block_id": market.block_id}
            for item in thesis["assumptions"]
        ],
        user_resolution="system_verified",
    )
    assert neutral["schedule_update_applied"] == 0
    assert all(item["status"] == "needs_review" for item in neutral["assumption_results"])
    assert next(
        item for item in repository.decision_tasks() if item["task_id"] == due_task["task_id"]
    )["status"] == "open"


def test_partial_assumption_verification_advances_only_verified_assumption(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="Partial evidence must preserve every unresolved due condition.",
        due_at=today.isoformat(),
    )
    due_task = thesis_due_scan(settings, as_of=today)["tasks"][0]
    pack = _context_pack(thesis)
    EvidenceRepository(settings.resolve(settings.get("system.database_path"))).save_context(pack)
    event = pack.block(EvidenceDomain.EVENT)
    checked = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        evidence_refs=[
            {
                "assumption_id": thesis["assumptions"][0]["assumption_id"],
                "block_id": event.block_id,
            }
        ],
        user_resolution="system_verified",
    )
    assert checked["schedule_update_applied"] == 1
    refreshed = Round8Repository(settings.resolve(settings.get("system.database_path"))).thesis(
        thesis["thesis_id"]
    )
    assert refreshed["assumptions"][0]["next_check_at"] > today.isoformat()
    assert all(
        item["next_check_at"] == today.isoformat()
        for item in refreshed["assumptions"][1:]
    )
    assert refreshed["next_check_at"] == today.isoformat()
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    assert next(
        item for item in repository.decision_tasks() if item["task_id"] == due_task["task_id"]
    )["status"] == "open"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("每天", "daily"),
        ("每周", "weekly"),
        ("每月", "monthly"),
        ("每季度", "quarterly"),
        ("事件触发", "event_driven"),
        ("财报披露后", "event_driven"),
        ("手动复核", "manual"),
        ("事件触发并每月复核", "monthly"),
    ],
)
def test_chinese_check_frequency_is_normalized_only_at_input_boundary(raw, expected):
    assumption = ThesisAssumptionDraft.model_validate(
        {
            "statement": "This assumption remains measurable and falsifiable.",
            "verification_metric": "fixture metric",
            "check_frequency": raw,
        }
    )
    assert assumption.check_frequency == expected


def test_unknown_llm_check_frequency_is_rejected():
    with pytest.raises(ValidationError, match="check_frequency"):
        ThesisAssumptionDraft.model_validate(
            {
                "statement": "This assumption remains measurable and falsifiable.",
                "verification_metric": "fixture metric",
                "check_frequency": "whenever_the_model_feels_like_it",
            }
        )


def test_all_canonical_frequencies_use_expected_trading_session_semantics(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1), days=140)
    frequencies = ("daily", "weekly", "monthly", "quarterly", "event_driven", "manual")
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="Canonical frequencies have stable trading-session semantics.",
        due_at=today.isoformat(),
        frequencies=frequencies,
    )
    checked = _save_and_check(settings, thesis)
    assert checked["schedule_update_applied"] == 1
    calendar = TradingCalendarService.from_settings(settings)
    refreshed = Round8Repository(calendar.repository.path).thesis(thesis["thesis_id"])
    by_frequency = {item["check_frequency"]: item for item in refreshed["assumptions"]}
    for frequency, sessions in (("daily", 1), ("weekly", 5), ("monthly", 20), ("quarterly", 60)):
        assert by_frequency[frequency]["next_check_at"] == calendar.add_open_days(
            today, sessions, formal=True
        ).isoformat()
    assert by_frequency["event_driven"]["next_check_at"] is None
    assert by_frequency["manual"]["next_check_at"] is None


def test_schedule_uses_configured_market_timezone(monkeypatch, tmp_path):
    import quantlab.workflows.investment_thesis as thesis_workflow

    fixed = datetime(2026, 7, 19, 16, 30, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    settings = _settings(tmp_path)
    market_date = date(2026, 7, 20)
    _seed_calendar(settings, market_date, days=100)
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="The configured market timezone, not the UTC date, anchors scheduling.",
        due_at=market_date.isoformat(),
    )
    monkeypatch.setattr(thesis_workflow, "datetime", FixedDateTime)
    checked = _save_and_check(
        settings,
        thesis,
        observed_at=fixed - timedelta(minutes=1),
    )
    calendar = TradingCalendarService.from_settings(settings)
    assert checked["next_check_at"] == calendar.add_open_days(
        market_date, 5, formal=True
    ).isoformat()


def _seed_provider_acceptance(
    settings: Settings,
    *,
    trade_date: date,
    provider: str,
    selection_market_date: date | None = None,
    fallback: bool | None = None,
) -> tuple[Path, datetime, str]:
    path = settings.resolve(settings.get("system.database_path"))
    round9 = Round9Repository(path)
    manifests = Round5Repository(path)
    evidence = StrategyEvidenceRepository(path)
    observed = datetime.now(UTC) - timedelta(minutes=2)
    batch_manifests = {}
    for batch_type in (
        "trading_calendar",
        "security_master",
        "industry_membership",
        "point_in_time_pool",
    ):
        batch_manifests[batch_type] = manifests.create_manifest(
            batch_type=batch_type,
            namespace=DataNamespace.PRODUCTION,
            trust_level=DataTrustLevel.SERVER_OBSERVED,
            provider=provider,
            source=f"fixture://{batch_type}",
            endpoint=f"fixture://{batch_type}",
            source_version="fixture-v1",
            available_at=observed,
            license_status="test_fixture",
            payload={"batch_type": batch_type, "provider": provider, "trade_date": trade_date},
            raw_fingerprint=hashlib.sha256(f"{provider}:{batch_type}".encode()).hexdigest(),
            record_count=1,
            date_start=trade_date,
            date_end=trade_date,
        )
    member = PointInTimePoolMember(
        symbol="sh600001",
        name="fixture",
        asset_class="stock",
        category="a_share",
        eligible=True,
        source=provider,
        available_at=observed,
    )
    snapshot = PointInTimePoolSnapshot(
        snapshot_type="a_share",
        snapshot_date=trade_date,
        cutoff_at=observed + timedelta(seconds=30),
        protocol_version="round9-tail-test",
        source=provider,
        source_version="fixture-v1",
        stage=EvidenceStage.MEASURED,
        members=[member],
        created_at=observed + timedelta(seconds=31),
        namespace=DataNamespace.PRODUCTION,
        trust_level=DataTrustLevel.SERVER_OBSERVED,
        manifest_id=batch_manifests["point_in_time_pool"]["manifest_id"],
    )
    saved_snapshot = evidence.save_pool_snapshot(snapshot)
    refresh_id = f"refresh-{provider}-{uuid.uuid4()}"
    round9.link_pool_refresh(saved_snapshot["snapshot_id"], refresh_id)
    capabilities = {
        "trading_calendar": "trading_calendar",
        "security_master": "security_master",
        "industry_membership": "industry_membership",
        "point_in_time_universe": "trade_status",
        "market_spot": "market_spot",
        "point_in_time_pool": "point_in_time_pool",
    }
    fallback = provider.startswith("fallback") if fallback is None else fallback
    selections = {}
    for component, capability in capabilities.items():
        batch_type = (
            component
            if component in {"trading_calendar", "security_master", "industry_membership"}
            else "point_in_time_pool"
        )
        pool_linked = component in {
            "point_in_time_universe",
            "market_spot",
            "point_in_time_pool",
        }
        selections[component] = {
            "selected_provider": provider,
            "reason": (
                "fallback_selected_after_primary_failure"
                if fallback
                else (
                    "server_configured_file"
                    if provider == "server_configured_file"
                    else "highest_priority_available_capability_provider"
                )
            ),
            "related_failures": (
                [{"provider": "primary", "priority": 10, "status": "timeout"}]
                if fallback
                else []
            ),
            "attempts": (
                [
                    {"provider": "primary", "priority": 10, "status": "timeout"},
                    {"provider": provider, "priority": 20, "status": "available"},
                ]
                if fallback
                else (
                    []
                    if provider == "server_configured_file"
                    else [{"provider": provider, "priority": 10, "status": "available"}]
                )
            ),
            "market_date": (selection_market_date or trade_date).isoformat(),
            "status": "completed",
            "capability": capability,
            "source_version": "fixture-v1",
            "manifest_id": batch_manifests[batch_type]["manifest_id"],
            "pool_snapshot_id": saved_snapshot["snapshot_id"] if pool_linked else None,
            "pool_fingerprint": saved_snapshot["fingerprint"] if pool_linked else None,
        }
    Round8Repository(path).record_provider_selections(
        refresh_id,
        selections,
        observed_at=observed,
        market_date=selection_market_date or trade_date,
    )
    return path, datetime.now(UTC), refresh_id


@pytest.mark.parametrize(
    "provider",
    ["server_configured_file", "baostock", "fallback_baostock"],
)
def test_provider_refresh_complete_server_file_and_fallback_can_pass(tmp_path, provider):
    settings = _settings(tmp_path / provider)
    trade_date = date(2026, 7, 20)
    path, checked_at, _ = _seed_provider_acceptance(
        settings, trade_date=trade_date, provider=provider
    )
    report = _provider_refresh_acceptance_checks(
        path, trade_date=trade_date, checked_at=checked_at
    )
    assert report["provider_selection_passed"] is True
    assert report["pool_refresh_matches"] is True
    assert report["missing_provider_components"] == []
    assert report["unavailable_provider_components"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "direct_without_attempts",
        "direct_without_selected_success",
        "fallback_disguised_as_direct",
        "fallback_failures_deleted",
        "fallback_failure_attempt_deleted",
        "fallback_priority_reversed",
        "selected_success_not_final",
    ],
)
def test_provider_attempt_audit_rejects_direct_and_fallback_disguises(tmp_path, mutation):
    trade_date = date(2026, 7, 20)
    is_fallback = mutation.startswith("fallback") or mutation == "selected_success_not_final"
    provider = "fallback_baostock" if is_fallback else "baostock"
    settings = _settings(tmp_path / mutation)
    path, checked_at, refresh_id = _seed_provider_acceptance(
        settings,
        trade_date=trade_date,
        provider=provider,
        fallback=is_fallback,
    )
    with sqlite3.connect(path) as db:
        if mutation == "direct_without_attempts":
            db.execute(
                """UPDATE provider_refresh_selections SET attempts='[]'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "direct_without_selected_success":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET attempts='[{"provider":"primary","priority":10,"status":"available"}]'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "fallback_disguised_as_direct":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET selection_reason='selected_directly'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "fallback_failures_deleted":
            db.execute(
                """UPDATE provider_refresh_selections SET related_failures='[]'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "fallback_failure_attempt_deleted":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET attempts='[{"provider":"fallback_baostock","priority":20,"status":"available"}]'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "fallback_priority_reversed":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET attempts=?,related_failures=?
                   WHERE refresh_id=? AND component='security_master'""",
                (
                    json.dumps(
                        [
                            {"provider": "primary", "priority": 30, "status": "timeout"},
                            {
                                "provider": "fallback_baostock",
                                "priority": 20,
                                "status": "available",
                            },
                        ]
                    ),
                    json.dumps(
                        [{"provider": "primary", "priority": 30, "status": "timeout"}]
                    ),
                    refresh_id,
                ),
            )
        elif mutation == "selected_success_not_final":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET attempts=?
                   WHERE refresh_id=? AND component='security_master'""",
                (
                    json.dumps(
                        [
                            {"provider": "primary", "priority": 10, "status": "timeout"},
                            {
                                "provider": "fallback_baostock",
                                "priority": 20,
                                "status": "available",
                            },
                            {"provider": "primary", "priority": 10, "status": "failed"},
                        ]
                    ),
                    refresh_id,
                ),
            )
        else:
            raise AssertionError(f"unknown mutation: {mutation}")

    report = _provider_refresh_acceptance_checks(
        path, trade_date=trade_date, checked_at=checked_at
    )
    check = report["component_checks"]["security_master"]
    assert report["provider_selection_passed"] is False
    assert check["provider_attempt_audit_valid"] is False
    assert "provider_attempt_audit_valid" in check["failed_checks"]


def test_provider_refresh_rejects_old_empty_missing_and_pool_mismatch(tmp_path):
    trade_date = date(2026, 7, 20)

    old_settings = _settings(tmp_path / "old-refresh")
    path, checked_at, _ = _seed_provider_acceptance(
        old_settings,
        trade_date=trade_date,
        provider="server_configured_file",
        selection_market_date=trade_date - timedelta(days=1),
    )
    old = _provider_refresh_acceptance_checks(path, trade_date=trade_date, checked_at=checked_at)
    assert old["provider_selection_passed"] is False
    assert old["provider_refresh_market_date"] == (trade_date - timedelta(days=1)).isoformat()

    empty_settings = _settings(tmp_path / "empty-provider")
    path, checked_at, refresh_id = _seed_provider_acceptance(
        empty_settings, trade_date=trade_date, provider="server_configured_file"
    )
    with sqlite3.connect(path) as db:
        db.execute(
            """UPDATE provider_refresh_selections SET selected_provider=NULL
               WHERE refresh_id=? AND component='security_master'""",
            (refresh_id,),
        )
    empty = _provider_refresh_acceptance_checks(path, trade_date=trade_date, checked_at=checked_at)
    assert empty["provider_selection_passed"] is False
    assert "security_master" in empty["unavailable_provider_components"]

    missing_settings = _settings(tmp_path / "missing-market-spot")
    path, checked_at, refresh_id = _seed_provider_acceptance(
        missing_settings, trade_date=trade_date, provider="server_configured_file"
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "DELETE FROM provider_refresh_selections WHERE refresh_id=? AND component='market_spot'",
            (refresh_id,),
        )
    missing = _provider_refresh_acceptance_checks(path, trade_date=trade_date, checked_at=checked_at)
    assert missing["provider_selection_passed"] is False
    assert "market_spot" in missing["missing_provider_components"]

    mismatch_settings = _settings(tmp_path / "pool-mismatch")
    path, checked_at, _ = _seed_provider_acceptance(
        mismatch_settings, trade_date=trade_date, provider="server_configured_file"
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE pit_pool_snapshots SET refresh_id='different-refresh'")
    mismatch = _provider_refresh_acceptance_checks(
        path, trade_date=trade_date, checked_at=checked_at
    )
    assert mismatch["provider_selection_passed"] is False
    assert mismatch["pool_refresh_matches"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "provider_empty",
        "provider_whitespace",
        "unknown_success_status",
        "fallback_without_audit",
        "manifest_provider_mismatch",
        "manifest_null_dates",
        "manifest_future",
        "manifest_non_production",
        "manifest_trust_too_low",
        "source_version_mismatch",
        "selection_observed_future",
        "snapshot_future",
        "snapshot_non_production",
        "snapshot_trust_too_low",
        "snapshot_refresh_mismatch",
        "snapshot_manifest_mismatch",
        "snapshot_fingerprint_mismatch",
    ],
)
def test_provider_refresh_adversarial_mutations_fail_closed(tmp_path, mutation):
    trade_date = date(2026, 7, 20)
    provider = "fallback_baostock" if mutation == "fallback_without_audit" else "server_configured_file"
    settings = _settings(tmp_path / mutation)
    path, checked_at, refresh_id = _seed_provider_acceptance(
        settings, trade_date=trade_date, provider=provider
    )
    with sqlite3.connect(path) as db:
        manifest_id = db.execute(
            """SELECT manifest_id FROM provider_refresh_selections
               WHERE refresh_id=? AND component='security_master'""",
            (refresh_id,),
        ).fetchone()[0]
        if mutation == "provider_empty":
            db.execute(
                """UPDATE provider_refresh_selections SET selected_provider=''
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "provider_whitespace":
            db.execute(
                """UPDATE provider_refresh_selections SET selected_provider='   '
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "unknown_success_status":
            db.execute(
                """UPDATE provider_refresh_selections SET status='nonsense_success'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "fallback_without_audit":
            db.execute(
                """UPDATE provider_refresh_selections
                   SET attempts='[]',related_failures='[]'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "manifest_provider_mismatch":
            db.execute(
                "UPDATE trusted_data_manifests SET provider='different-provider' WHERE manifest_id=?",
                (manifest_id,),
            )
        elif mutation == "manifest_null_dates":
            db.execute(
                "UPDATE trusted_data_manifests SET date_start=NULL,date_end=NULL WHERE manifest_id=?",
                (manifest_id,),
            )
        elif mutation == "manifest_future":
            db.execute(
                "UPDATE trusted_data_manifests SET available_at=? WHERE manifest_id=?",
                ((checked_at + timedelta(days=1)).isoformat(), manifest_id),
            )
        elif mutation == "manifest_non_production":
            db.execute(
                "UPDATE trusted_data_manifests SET namespace='research' WHERE manifest_id=?",
                (manifest_id,),
            )
        elif mutation == "manifest_trust_too_low":
            db.execute(
                "UPDATE trusted_data_manifests SET trust_level='research_external' WHERE manifest_id=?",
                (manifest_id,),
            )
        elif mutation == "source_version_mismatch":
            db.execute(
                """UPDATE provider_refresh_selections SET source_version='different-version'
                   WHERE refresh_id=? AND component='security_master'""",
                (refresh_id,),
            )
        elif mutation == "selection_observed_future":
            db.execute(
                """UPDATE provider_refresh_selections SET observed_at=?
                   WHERE refresh_id=? AND component='security_master'""",
                ((checked_at + timedelta(days=1)).isoformat(), refresh_id),
            )
        elif mutation == "snapshot_future":
            future = (checked_at + timedelta(days=1)).isoformat()
            db.execute("UPDATE pit_pool_snapshots SET cutoff_at=?,created_at=?", (future, future))
        elif mutation == "snapshot_non_production":
            db.execute("UPDATE pit_pool_snapshots SET namespace='research'")
        elif mutation == "snapshot_trust_too_low":
            db.execute("UPDATE pit_pool_snapshots SET trust_level='research_external'")
        elif mutation == "snapshot_refresh_mismatch":
            db.execute("UPDATE pit_pool_snapshots SET refresh_id='different-refresh'")
        elif mutation == "snapshot_manifest_mismatch":
            db.execute("UPDATE pit_pool_snapshots SET manifest_id='different-manifest'")
        elif mutation == "snapshot_fingerprint_mismatch":
            db.execute(
                """UPDATE provider_refresh_selections SET pool_fingerprint='different-fingerprint'
                   WHERE refresh_id=? AND component='point_in_time_pool'""",
                (refresh_id,),
            )
        else:
            raise AssertionError(f"unknown mutation: {mutation}")
    report = _provider_refresh_acceptance_checks(
        path, trade_date=trade_date, checked_at=checked_at
    )
    assert report["provider_selection_passed"] is False
    assert report["explicit_provider_failure"] is True
    assert set(report["component_checks"]) == set(report["provider_components_expected"])
    assert report["unavailable_reasons"]


def _insert_order(path: Path, order_id: str, *, status: str = "pending") -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO user_paper_orders(
                 order_id,account_id,idempotency_key,symbol,asset_type,side,
                 requested_quantity,filled_quantity,status,requested_at,
                 eligible_trade_date,reference_price,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id,
                "paper-account",
                f"idempotency-{order_id}",
                "sh600001",
                "stock",
                "buy",
                100,
                0,
                status,
                now,
                date.today().isoformat(),
                10.0,
                now,
                now,
            ),
        )


def test_decision_task_reconciliation_resolves_and_preserves_user_history(tmp_path):
    settings = _settings(tmp_path)
    path = settings.resolve(settings.get("system.database_path"))
    UserPaperTradingRepository(path)
    data_states = Round6Repository(path)
    tasks = Round9Repository(path)

    _insert_order(path, "order-filled")
    first = refresh_decision_tasks(settings)
    order_task = next(item for item in first["tasks"] if item["source_id"] == "order-filled")
    replay = refresh_decision_tasks(settings)
    replay_task = next(item for item in replay["tasks"] if item["source_id"] == "order-filled")
    assert replay_task["task_id"] == order_task["task_id"]
    assert len(tasks.decision_task_events(order_task["task_id"])) == 1
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE user_paper_orders SET status='filled',filled_quantity=100 WHERE order_id='order-filled'"
        )
    refresh_decision_tasks(settings)
    resolved_order = next(
        item for item in tasks.decision_tasks() if item["task_id"] == order_task["task_id"]
    )
    assert resolved_order["status"] == "resolved"
    assert resolved_order["resolved_reason"] == "source_condition_cleared"
    assert [
        (item["previous_status"], item["new_status"], item["reason"])
        for item in tasks.decision_task_events(order_task["task_id"])
    ] == [
        (None, "open", "source_condition_detected"),
        ("open", "resolved", "source_condition_cleared"),
    ]

    data_states.update_data_source_state(
        "market_spot",
        status="unavailable",
        manifest_id=None,
        date_start=None,
        date_end=None,
        symbol_count=0,
        field_coverage=0,
        minimum_ready=False,
        attempted_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    data_first = refresh_decision_tasks(settings)
    data_task = next(item for item in data_first["tasks"] if item["source_id"] == "market_spot")
    data_states.update_data_source_state(
        "market_spot",
        status="unavailable",
        manifest_id=None,
        date_start=None,
        date_end=None,
        symbol_count=0,
        field_coverage=0,
        minimum_ready=False,
        attempted_at=datetime.now(UTC),
    )
    data_replay = refresh_decision_tasks(settings)
    assert next(item for item in data_replay["tasks"] if item["source_id"] == "market_spot")[
        "task_id"
    ] == data_task["task_id"]
    data_states.update_data_source_state(
        "market_spot",
        status="completed",
        manifest_id=None,
        date_start=date.today().isoformat(),
        date_end=date.today().isoformat(),
        symbol_count=1,
        field_coverage=1,
        minimum_ready=True,
    )
    refresh_decision_tasks(settings)
    assert next(item for item in tasks.decision_tasks() if item["task_id"] == data_task["task_id"])[
        "status"
    ] == "resolved"

    _insert_order(path, "order-dismissed")
    dismissed_task = next(
        item
        for item in refresh_decision_tasks(settings)["tasks"]
        if item["source_id"] == "order-dismissed"
    )
    tasks.update_task_status(
        dismissed_task["task_id"], status="dismissed", reason="user_dismissed", actor="user"
    )
    same_condition = next(
        item
        for item in refresh_decision_tasks(settings)["tasks"]
        if item["source_id"] == "order-dismissed"
    )
    assert same_condition["task_id"] == dismissed_task["task_id"]
    assert same_condition["status"] == "dismissed"
    with sqlite3.connect(path) as db:
        db.execute(
            """UPDATE user_paper_orders SET status='partially_filled',filled_quantity=10
               WHERE order_id='order-dismissed'"""
        )
    changed = next(
        item
        for item in refresh_decision_tasks(settings)["tasks"]
        if item["source_id"] == "order-dismissed" and item["status"] == "open"
    )
    assert changed["task_id"] != dismissed_task["task_id"]
    assert changed["condition_fingerprint"] != dismissed_task["condition_fingerprint"]


def test_resolved_due_task_reopens_but_same_dismissed_condition_stays_closed(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="A due condition remains active until evidence really clears it.",
        due_at=today.isoformat(),
    )
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    due_task = thesis_due_scan(settings, as_of=today)["tasks"][0]
    repository.update_task_status(
        due_task["task_id"],
        status="resolved",
        reason="incorrect_manual_resolution",
        actor="user",
    )
    reopened = thesis_due_scan(settings, as_of=today)["tasks"][0]
    assert reopened["task_id"] == due_task["task_id"]
    assert reopened["status"] == "open"
    reopen_event = repository.decision_task_events(due_task["task_id"])[-1]
    assert reopen_event["previous_status"] == "resolved"
    assert reopen_event["new_status"] == "open"
    assert reopen_event["reason"] == "source_condition_recurred"
    assert reopen_event["actor"] == "system"
    assert reopen_event["evidence_fingerprint"] == due_task["condition_fingerprint"]

    repository.update_task_status(
        due_task["task_id"], status="dismissed", reason="user_dismissed", actor="user"
    )
    replay = thesis_due_scan(settings, as_of=today)["tasks"][0]
    assert replay["task_id"] == due_task["task_id"]
    assert replay["status"] == "dismissed"


def test_closed_thesis_resolves_only_system_managed_thesis_tasks(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    thesis, _ = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="Closing a thesis clears its system reminders but never edits user tasks.",
        due_at=today.isoformat(),
    )
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    due_task = thesis_due_scan(settings, as_of=today)["tasks"][0]

    def task_payload(task_type, dedup_key, management_source="system_managed"):
        return {
            "category": "needs_review",
            "task_type": task_type,
            "severity": "warning",
            "title": task_type,
            "user_summary": "fixture",
            "source_type": "investment_thesis",
            "source_id": thesis["thesis_id"],
            "dedup_key": dedup_key,
            "condition_fingerprint": hashlib.sha256(dedup_key.encode()).hexdigest(),
            "management_source": management_source,
            "payload": {"fixture": True},
        }

    weakened = repository.upsert_decision_task(
        task_payload("thesis_weakened", f"weakened:{thesis['thesis_id']}")
    )
    red_line = repository.upsert_decision_task(
        task_payload("thesis_red_line", f"red-line:{thesis['thesis_id']}")
    )
    manual = repository.upsert_decision_task(
        task_payload(
            "thesis_weakened",
            f"manual:{thesis['thesis_id']}",
            management_source="user_created",
        )
    )
    pack = _context_pack(thesis)
    EvidenceRepository(repository.path).save_context(pack)
    event = pack.block(EvidenceDomain.EVENT)
    closed = check_investment_thesis(
        settings,
        thesis_id=thesis["thesis_id"],
        context_id=pack.context_id,
        context_fingerprint=pack.fingerprint,
        evidence_refs=[
            {"assumption_id": item["assumption_id"], "block_id": event.block_id}
            for item in thesis["assumptions"]
        ],
        user_resolution="closed",
    )
    assert closed["final_status"] == "closed"
    assert closed["schedule_update_applied"] == 0
    by_id = {item["task_id"]: item for item in repository.decision_tasks(limit=500)}
    assert by_id[due_task["task_id"]]["status"] == "resolved"
    assert by_id[weakened["task_id"]]["status"] == "resolved"
    assert by_id[red_line["task_id"]]["status"] == "resolved"
    assert by_id[manual["task_id"]]["status"] == "open"
    assert by_id[manual["task_id"]]["management_source"] == "user_created"


def test_round9_v4_reapplies_latest_frozen_payload_idempotently(tmp_path):
    settings = _settings(tmp_path)
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    _seed_calendar(settings, today - timedelta(days=1))
    thesis, revision = _freeze(
        settings,
        _pending_thesis(settings),
        core_logic="NEW FROZEN CONTENT",
        due_at=today.isoformat(),
    )
    historical_check = _save_and_check(settings, thesis)
    path = settings.resolve(settings.get("system.database_path"))
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE investment_theses SET core_thesis='STALE BASE CONTENT' WHERE thesis_id=?",
            (thesis["thesis_id"],),
        )
        db.execute(
            """UPDATE thesis_assumptions
               SET statement='STALE BASE CONTENT',active_revision_id=NULL
               WHERE thesis_id=?""",
            (thesis["thesis_id"],),
        )
        before_check = db.execute(
            """SELECT frozen_revision_id,frozen_revision_fingerprint,report_fingerprint
               FROM thesis_checks WHERE check_id=?""",
            (historical_check["check_id"],),
        ).fetchone()
        db.execute(
            "DELETE FROM quantlab_migration_registry WHERE component='round9' AND version=4"
        )

    upgraded = initialize_or_upgrade_database(path)
    assert "round9:4" in upgraded["pre_upgrade_backup"]["pending_migrations"]
    refreshed = Round8Repository(path).thesis(thesis["thesis_id"])
    assert refreshed["core_thesis"] == "NEW FROZEN CONTENT"
    assert refreshed["current_frozen_revision"]["revision_id"] == revision["revision_id"]
    assert all(
        item["statement"].startswith("Frozen assumption")
        and item["active_revision_id"] == revision["revision_id"]
        for item in refreshed["assumptions"]
    )
    with sqlite3.connect(path) as db:
        after_check = db.execute(
            """SELECT frozen_revision_id,frozen_revision_fingerprint,report_fingerprint
               FROM thesis_checks WHERE check_id=?""",
            (historical_check["check_id"],),
        ).fetchone()
        state_before_replay = db.execute(
            """SELECT core_thesis,current_frozen_revision_id,thesis_fingerprint
               FROM investment_theses WHERE thesis_id=?""",
            (thesis["thesis_id"],),
        ).fetchone()
    assert tuple(after_check) == tuple(before_check)

    replay = initialize_or_upgrade_database(path)
    assert replay["pre_upgrade_backup"] is None
    with sqlite3.connect(path) as db:
        state_after_replay = db.execute(
            """SELECT core_thesis,current_frozen_revision_id,thesis_fingerprint
               FROM investment_theses WHERE thesis_id=?""",
            (thesis["thesis_id"],),
        ).fetchone()
    assert tuple(state_after_replay) == tuple(state_before_replay)


def test_isolated_demo_lifecycle_reaches_auditable_decision_run(tmp_path):
    settings = _settings(tmp_path / "isolated-demo")
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round5Repository(path)
    observed = datetime.now(UTC) - timedelta(minutes=2)
    seed_pack = AnalysisContextPack(
        symbol="sh600010",
        asset_type=AssetType.STOCK,
        as_of=observed.date(),
        cutoff_at=observed,
        generated_at=observed,
        blocks=[
            EvidenceBlock(
                block_id="demo-market",
                domain=EvidenceDomain.MARKET,
                title="isolated demo market",
                source="isolated_fixture",
                methodology="stored fixture",
                as_of=observed,
                available_at=observed,
                fetched_at=observed,
                quality=EvidenceQuality.AVAILABLE,
                payload={"current_raw_price": 10.0},
            )
        ],
        deterministic_summary={"boundary": "research_only", "test_only": True},
    )
    EvidenceRepository(path).save_context(seed_pack)
    recommendation_id = str(uuid.uuid4())
    with repository.connect() as db:
        db.execute(
            """INSERT INTO investor_recommendations(
                 recommendation_id,portfolio_id,symbol,as_of,action,quantity_min,
                 quantity_max,actionable,context_id,context_fingerprint,payload,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                recommendation_id,
                "isolated-demo-portfolio",
                "sh600010",
                date.today().isoformat(),
                "buy",
                100,
                100,
                1,
                seed_pack.context_id,
                seed_pack.fingerprint,
                json.dumps(
                    {
                        "suggested_action": "buy",
                        "supporting_evidence": ["demo-market"],
                        "opposing_evidence": [],
                        "invalidation_conditions": ["material adverse event"],
                        "start_price": 10.0,
                        "asset_type": "stock",
                        "due_dates": {"5": date.today().isoformat(), "20": date.today().isoformat()},
                        "evidence_boundary": "research_only",
                        "test_only": True,
                    }
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
    adoption = record_recommendation_adoption(
        settings, recommendation_id=recommendation_id, decision="adopted"
    )
    pending = adoption["thesis"]
    assert pending["status"] == "draft_pending_confirmation"
    assert pending["current_frozen_revision"] is None
    assert thesis_due_scan(settings, as_of=date.today())["tasks"] == []

    edited = edit_investment_thesis_draft(
        settings,
        thesis_id=pending["thesis_id"],
        payload=_draft(
            "The isolated demo thesis becomes effective only after this explicit freeze.",
            due_at=date.today().isoformat(),
        ),
    )
    frozen = freeze_investment_thesis_revision(
        settings, thesis_id=pending["thesis_id"], revision_id=edited["revision_id"]
    )["thesis"]
    assert frozen["status"] == "active"
    assert frozen["current_frozen_revision"]["revision_id"] == edited["revision_id"]
    _seed_calendar(settings, date.today() - timedelta(days=1))
    due = thesis_due_scan(settings, as_of=date.today())
    assert len(due["tasks"]) == 1

    checked = _save_and_check(settings, frozen)
    assert checked["schedule_update_applied"] == 1
    due_task = next(
        item
        for item in Round9Repository(path).decision_tasks()
        if item["task_type"] == "investment_thesis_due_review"
    )
    assert due_task["status"] == "resolved"
    audit = Round9Repository(path).export_decision_run(frozen["run_id"])
    assert audit["decision_run"]["run_id"] == frozen["run_id"]
    assert audit["artifact_fingerprint"]
    assert frozen["current_frozen_revision"]["payload"]["data_provenance"] == {
        "boundary": "research_only",
        "source": "isolated_round9_tail_fixture",
        "test_only": True,
    }
    assert Path(settings.get("system.database_path")).parent.name == "isolated-demo"
    assert settings.get("system.test_mode") is True
