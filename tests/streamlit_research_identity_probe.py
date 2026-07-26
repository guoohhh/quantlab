from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import streamlit as st

from dashboard import product_ui
from dashboard.ui_foundation import (
    PRODUCT_PAGE_KEY,
    PRODUCT_PAGE_TARGET_KEY,
    cache_research_report,
    current_product_page,
    set_product_page,
)
from quantlab.config import Settings


probe = Path(os.environ["QUANTLAB_RESEARCH_PROBE_FILE"])


def calls() -> list[str]:
    return probe.read_text(encoding="utf-8").splitlines() if probe.exists() else []


def record_call(event: str) -> None:
    probe.parent.mkdir(parents=True, exist_ok=True)
    with probe.open("a", encoding="utf-8") as output:
        output.write(f"{event}\n")


def _date_text(value: date | str | None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _seed_lightweight_indexes() -> None:
    if st.session_state.get("_research_probe_seeded"):
        return
    records = st.session_state.setdefault("_research_probe_records", {})
    for number in range(1, 10):
        run_id = f"seed-{number}"
        records[run_id] = {
            "run_id": run_id,
            "symbol": f"sh600{number:03d}",
            "requested_as_of": "2026-07-19",
            "effective_as_of": "2026-07-17",
            "as_of": "2026-07-17",
            "action": "buy" if number % 2 else "review",
            "confidence": 0.5,
            "evidence_stage": "research_only",
            "created_at": f"2026-07-19T09:{number:02d}:00+00:00",
        }
    st.session_state["_research_probe_seeded"] = True


class ProbeDecisionRepository:
    def __init__(self, _path: object):
        self.records = st.session_state.setdefault("_research_probe_records", {})

    def research_page(
        self,
        *,
        page: int,
        page_size: int,
        query: str | None = None,
        action: str | None = None,
        evidence_stage: str | None = None,
    ) -> dict[str, object]:
        record_call(
            f"index|page={page}|query={query or ''}|action={action or ''}|stage={evidence_stage or ''}"
        )
        items = list(self.records.values())
        if query:
            normalized = query.lower()
            items = [
                item
                for item in items
                if normalized in str(item["symbol"]).lower()
                or normalized in str(item["run_id"]).lower()
            ]
        if action:
            items = [item for item in items if item["action"] == action]
        if evidence_stage:
            items = [item for item in items if item["evidence_stage"] == evidence_stage]
        items.sort(key=lambda item: str(item["run_id"]), reverse=True)
        start = (page - 1) * page_size
        return {
            "items": [dict(item) for item in items[start : start + page_size]],
            "total": len(items),
            "page": page,
            "page_size": page_size,
        }

    def save(self, decision_run: object, *, research_context: object, provenance: object) -> None:
        del research_context
        decision = decision_run.decision
        effective = _date_text(decision.as_of)
        self.records[decision_run.run_id] = {
            "run_id": decision_run.run_id,
            "symbol": decision.symbol,
            "requested_as_of": _date_text(provenance.requested_as_of),
            "effective_as_of": effective,
            "as_of": effective,
            "action": decision.action,
            "confidence": decision.confidence,
            "evidence_stage": provenance.evidence_stage,
            "created_at": "2026-07-19T10:00:00+00:00",
        }

    def get(self, run_id: str) -> dict[str, object] | None:
        record_call(f"detail-get|{run_id}")
        item = self.records.get(run_id)
        return dict(item) if item else None


class ProbeRoundtableRepository:
    def __init__(self, _path: object):
        pass

    def sessions_for_source(self, _run_id: str, *, limit: int) -> list[dict[str, object]]:
        assert limit == 20
        return []

    def get(self, _session_id: str) -> None:
        return None


def fake_analyze(_settings: Settings, symbol: str, requested_as_of: date) -> dict[str, object]:
    analysis_count = sum(item.startswith("analysis|") for item in calls()) + 1
    record_call(f"analysis|{symbol}|{requested_as_of.isoformat()}")
    effective = requested_as_of - timedelta(days=2)
    decision = SimpleNamespace(
        symbol=symbol,
        as_of=effective,
        action="watch",
        confidence=0.6,
    )
    return {"decision_run": SimpleNamespace(run_id=f"run-{analysis_count}", decision=decision)}


def fake_stored_audit_package(record: dict[str, object]) -> dict[str, object]:
    effective = str(record["effective_as_of"])
    return {
        "run_id": record["run_id"],
        "symbol": record["symbol"],
        "as_of": effective,
        "data": {
            "effective_as_of": effective,
            "source": "probe",
            "bars": 120,
            "degraded_sources": [],
        },
        "decision": {
            "action": record["action"],
            "confidence": record["confidence"],
            "target_weight": 0.1,
            "evidence": {"supporting": [], "opposing": []},
            "invalidation_conditions": [],
        },
        "analysis_context_pack": {
            "quality_score": 0.8,
            "fingerprint": f"probe-{record['run_id']}",
        },
    }


def fake_validate_research_record(
    record: dict[str, object] | None,
    *,
    run_id: str,
    symbol: str,
) -> dict[str, object]:
    assert record is not None
    assert record["run_id"] == run_id
    assert record["symbol"] == symbol
    return {
        "run_id": run_id,
        "symbol": symbol,
        "requested_as_of": date.fromisoformat(str(record["requested_as_of"])),
        "effective_as_of": date.fromisoformat(str(record["effective_as_of"])),
    }


def _seed_cached_detail_route() -> None:
    record = st.session_state["_research_probe_records"]["seed-9"]
    report = fake_stored_audit_package(record)
    identity = cache_research_report(
        st.session_state,
        report,
        symbol=str(record["symbol"]),
        requested_as_of=str(record["requested_as_of"]),
    )
    set_product_page(
        st.session_state,
        "研究详情",
        symbol=identity.symbol,
        research_run_id=identity.run_id,
        research_requested_as_of=identity.requested_as_of,
        research_effective_as_of=identity.effective_as_of,
    )


def probe_navigation() -> str:
    target = st.session_state.pop(PRODUCT_PAGE_TARGET_KEY, None)
    if target:
        st.session_state[PRODUCT_PAGE_KEY] = target
    return current_product_page(st.session_state)


def probe_header(page: str) -> None:
    st.caption(f"route:{page}")


originals = {
    "DecisionRepository": product_ui.DecisionRepository,
    "RoundtableRepository": product_ui.RoundtableRepository,
    "analyze_symbol": product_ui.analyze_symbol,
    "apply_product_theme": product_ui.apply_product_theme,
    "build_stored_audit_package": product_ui.build_stored_audit_package,
    "record_product_usage": product_ui.record_product_usage,
    "render_product_navigation": product_ui.render_product_navigation,
    "_notification_attention_count": product_ui._notification_attention_count,
    "_render_global_ai_assistant": product_ui._render_global_ai_assistant,
    "_render_workspace_header": product_ui._render_workspace_header,
    "research_persistence_context": product_ui.research_persistence_context,
    "validate_research_record": product_ui.validate_research_record,
}
try:
    _seed_lightweight_indexes()
    if os.environ.get("QUANTLAB_RESEARCH_PROBE_START_ROUTE") == "cached_detail":
        if not st.session_state.get("_research_probe_detail_initialized"):
            _seed_cached_detail_route()
            st.session_state["_research_probe_detail_initialized"] = True
    else:
        st.session_state.setdefault(PRODUCT_PAGE_KEY, "研究台")
    # AppTest retains the prior element tree across a route rerun. Keep the hub
    # widget keys available while a detail or roundtable route is under test.
    st.session_state.setdefault("product_research_index_query", "")
    st.session_state.setdefault("product_research_index_action", "")
    st.session_state.setdefault("product_research_index_stage", "")
    st.session_state.setdefault("product_research_index_page", 1)
    st.session_state.setdefault("product_research_symbol_new", "sh510300")
    st.session_state.setdefault("product_research_date_new", date(2026, 7, 23))
    product_ui.DecisionRepository = ProbeDecisionRepository
    product_ui.RoundtableRepository = ProbeRoundtableRepository
    product_ui.analyze_symbol = fake_analyze
    product_ui.apply_product_theme = Mock()
    product_ui.build_stored_audit_package = fake_stored_audit_package
    product_ui.record_product_usage = Mock()
    product_ui.render_product_navigation = probe_navigation
    product_ui._notification_attention_count = lambda _settings: 0
    product_ui._render_global_ai_assistant = Mock()
    product_ui._render_workspace_header = probe_header
    product_ui.research_persistence_context = lambda _output: {}
    product_ui.validate_research_record = fake_validate_research_record
    product_ui.render_product_app(
        Settings(
            values={"system": {"database_path": "research-probe.db", "test_mode": True}},
            root=probe.parent,
        )
    )
    # The hub widgets are absent on detail routes. Persist their values after
    # those routes render so AppTest can issue an otherwise ordinary rerun.
    if current_product_page(st.session_state) != "研究台":
        st.session_state.setdefault("product_research_index_query", "")
        st.session_state.setdefault("product_research_index_action", "")
        st.session_state.setdefault("product_research_index_stage", "")
        st.session_state.setdefault("product_research_index_page", 1)
        st.session_state.setdefault("product_research_symbol_new", "sh510300")
        st.session_state.setdefault("product_research_date_new", date(2026, 7, 23))
finally:
    for name, value in originals.items():
        setattr(product_ui, name, value)
