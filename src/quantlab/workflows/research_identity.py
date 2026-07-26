from __future__ import annotations

from datetime import date
from typing import Any


_POLLUTION_FLAG_KEYS = {
    "demo",
    "is_demo",
    "is_test",
    "test_only",
}
_POLLUTION_MARKER_KEYS = {
    "data_namespace",
    "dataset_stage",
    "evidence_namespace",
    "evidence_stage",
    "mode",
    "namespace",
    "run_stage",
    "scenario_type",
    "source_stage",
}
_POLLUTION_MARKERS = {
    "demo",
    "fixture",
    "historical_demo",
    "historical_replay",
    "replay",
    "synthetic",
    "test",
    "testing",
}


def validate_research_record(
    record: dict[str, Any] | None,
    *,
    run_id: str,
    symbol: str | None,
    market_as_of: date | None = None,
    asset_type: str | None = None,
) -> dict[str, Any]:
    """Validate one explicitly selected research identity without fallback.

    ``research_only`` is the normal evidence boundary for an AI research report and
    is deliberately allowed. Explicit demo/test/replay namespaces are not allowed
    to enter Chat or the user paper-trading audit chain.
    """

    if record is None:
        raise ValueError(f"research run not found: {run_id}")
    stored_run_id = str(record.get("run_id") or "")
    if stored_run_id != str(run_id):
        raise ValueError("research run identity does not match requested run_id")

    stored_symbol = str(record.get("symbol") or "")
    if not stored_symbol:
        raise ValueError("research run symbol is unavailable")
    if symbol is not None and stored_symbol != str(symbol):
        raise ValueError("research run symbol does not match requested symbol")

    research_as_of = _optional_date(record.get("as_of"))
    effective_as_of = _optional_date(record.get("effective_as_of"))
    if effective_as_of is None or effective_as_of != research_as_of:
        raise ValueError("research run effective_as_of identity is unavailable or mismatched")
    requested_as_of = _optional_date(record.get("requested_as_of"))
    if requested_as_of is None:
        raise ValueError("research run requested_as_of identity is unavailable")
    _validate_internal_identity(
        record,
        symbol=stored_symbol,
        research_as_of=research_as_of,
    )
    if market_as_of is not None:
        if research_as_of is None:
            raise ValueError("research run as_of is unavailable for market-time validation")
        if research_as_of > market_as_of:
            raise ValueError("research run is later than the market quote")

    stored_asset_type = research_asset_type(record)
    requested_asset_type = str(asset_type or "").strip().lower()
    if (
        requested_asset_type
        and requested_asset_type != "auto"
        and stored_asset_type
        and stored_asset_type != requested_asset_type
    ):
        raise ValueError("research run asset type does not match requested asset type")

    if _pollution_marker(record):
        raise ValueError("research run is demo or test data and cannot be linked")
    origin = str(record.get("origin") or "legacy_unclassified")
    if origin in {"legacy_unclassified", "demo_research", "test_research"}:
        raise ValueError("research run origin is unqualified or unavailable")

    context_id, context_fingerprint = research_context_identity(record)
    if record.get("context_id") and str(record["context_id"]) != str(context_id):
        raise ValueError("research run context_id persistence identity does not match")
    if record.get("context_fingerprint") and str(record["context_fingerprint"]) != str(
        context_fingerprint
    ):
        raise ValueError("research run context fingerprint persistence identity does not match")
    return {
        "run_id": stored_run_id,
        "symbol": stored_symbol,
        "as_of": research_as_of,
        "requested_as_of": requested_as_of,
        "effective_as_of": effective_as_of,
        "origin": origin,
        "evidence_stage": record.get("evidence_stage") or "unavailable",
        "settlement_eligible": bool(record.get("settlement_eligible")),
        "training_eligible": bool(record.get("training_eligible")),
        "asset_type": stored_asset_type,
        "context_id": context_id,
        "context_fingerprint": context_fingerprint,
    }


def research_asset_type(record: dict[str, Any]) -> str | None:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    research_context = (
        payload.get("research_context")
        if isinstance(payload.get("research_context"), dict)
        else {}
    )
    analysis_context = (
        research_context.get("analysis_context_pack")
        if isinstance(research_context.get("analysis_context_pack"), dict)
        else {}
    )
    data = research_context.get("data") if isinstance(research_context.get("data"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    for value in (
        analysis_context.get("asset_type"),
        data.get("asset_type"),
        research_context.get("asset_type"),
        decision.get("asset_type"),
        payload.get("asset_type"),
    ):
        if value is not None and str(value).strip():
            return str(value).strip().lower()
    return None


def research_context_identity(record: dict[str, Any]) -> tuple[str | None, str | None]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    research_context = (
        payload.get("research_context")
        if isinstance(payload.get("research_context"), dict)
        else {}
    )
    analysis_context = (
        research_context.get("analysis_context_pack")
        if isinstance(research_context.get("analysis_context_pack"), dict)
        else {}
    )
    context_id = decision.get("context_id") or analysis_context.get("context_id")
    context_fingerprint = decision.get("context_fingerprint") or analysis_context.get(
        "fingerprint"
    )
    return (
        str(context_id) if context_id else None,
        str(context_fingerprint) if context_fingerprint else None,
    )


def _validate_internal_identity(
    record: dict[str, Any],
    *,
    symbol: str,
    research_as_of: date | None,
) -> None:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    research_context = (
        payload.get("research_context")
        if isinstance(payload.get("research_context"), dict)
        else {}
    )
    data = research_context.get("data") if isinstance(research_context.get("data"), dict) else {}
    analysis_context = (
        research_context.get("analysis_context_pack")
        if isinstance(research_context.get("analysis_context_pack"), dict)
        else {}
    )
    stored_identity = (
        payload.get("research_identity")
        if isinstance(payload.get("research_identity"), dict)
        else {}
    )

    for candidate in (decision.get("symbol"), analysis_context.get("symbol")):
        if candidate is not None and str(candidate) != symbol:
            raise ValueError("research record internal symbol identity does not match")
    if stored_identity:
        if str(stored_identity.get("run_id") or "") != str(record.get("run_id") or ""):
            raise ValueError("research record internal run_id identity does not match")
        if str(stored_identity.get("symbol") or "") != symbol:
            raise ValueError("research record internal symbol identity does not match")
        for key in ("requested_as_of", "effective_as_of", "origin", "evidence_stage"):
            stored = stored_identity.get(key)
            persisted = record.get(key)
            if stored is not None and str(stored) != str(persisted):
                raise ValueError(f"research record internal {key} identity does not match")

    if research_as_of is not None:
        for candidate in (
            decision.get("as_of"),
            data.get("effective_as_of"),
            analysis_context.get("as_of"),
        ):
            candidate_date = _optional_date(candidate)
            if candidate_date is not None and candidate_date != research_as_of:
                raise ValueError("research record internal as_of identity does not match")

    asset_types = {
        str(candidate).strip().lower()
        for candidate in (
            payload.get("asset_type"),
            decision.get("asset_type"),
            research_context.get("asset_type"),
            data.get("asset_type"),
            analysis_context.get("asset_type"),
        )
        if candidate is not None and str(candidate).strip()
    }
    if len(asset_types) > 1:
        raise ValueError("research record internal asset type identity does not match")

    decision_context_id = decision.get("context_id")
    pack_context_id = analysis_context.get("context_id")
    if (
        decision_context_id
        and pack_context_id
        and str(decision_context_id) != str(pack_context_id)
    ):
        raise ValueError("research record internal context_id identity does not match")
    decision_fingerprint = decision.get("context_fingerprint")
    pack_fingerprint = analysis_context.get("fingerprint")
    if (
        decision_fingerprint
        and pack_fingerprint
        and str(decision_fingerprint) != str(pack_fingerprint)
    ):
        raise ValueError("research record internal context fingerprint does not match")


def _pollution_marker(value: Any) -> bool:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower()
            if key in _POLLUTION_FLAG_KEYS and _truthy_marker(item):
                return True
            if key in _POLLUTION_MARKER_KEYS and _is_pollution_marker(item):
                return True
            if _pollution_marker(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_pollution_marker(item) for item in value)
    return False


def _is_pollution_marker(value: Any) -> bool:
    marker = str(value or "").strip().lower().replace("-", "_")
    if marker == "research_only":
        return False
    return marker in _POLLUTION_MARKERS or any(
        marker.startswith(f"{prefix}_")
        for prefix in ("demo", "fixture", "historical_demo", "historical_replay", "synthetic", "test")
    )


def _truthy_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _optional_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if value is None or not str(value).strip():
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError("research run as_of is invalid") from exc


__all__ = [
    "research_asset_type",
    "research_context_identity",
    "validate_research_record",
]
