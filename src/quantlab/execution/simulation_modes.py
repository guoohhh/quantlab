from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quantlab.domain.trading import DataQuality, MarketQuote


INTRADAY_SIMULATION = "intraday_simulation"
NEXT_OPEN_SIMULATION = "next_open_simulation"
USER_PAPER_SIMULATION_MODES = (
    INTRADAY_SIMULATION,
    NEXT_OPEN_SIMULATION,
)


def available_user_paper_simulation_modes(
    quote: MarketQuote,
    *,
    allow_test_quote: bool = False,
) -> tuple[str, ...]:
    """Return the only user-paper order modes supported by this quote."""
    if (
        quote.actionable
        and quote.session_status == "open"
        and quote.quote_kind in {"realtime", "delayed"}
    ):
        return (INTRADAY_SIMULATION,)
    if (
        allow_test_quote
        and quote.evidence_stage == "test"
        and quote.session_status == "open"
        and quote.quote_kind in {"realtime", "delayed"}
    ):
        return (INTRADAY_SIMULATION,)
    if (
        quote.authoritative
        and quote.session_status == "closed"
        and quote.quote_kind in {"current_close", "previous_close"}
        and quote.data_quality not in {DataQuality.STALE, DataQuality.MISSING}
    ):
        return (NEXT_OPEN_SIMULATION,)
    if (
        allow_test_quote
        and quote.evidence_stage == "test"
        and quote.session_status == "closed"
        and quote.quote_kind in {"current_close", "previous_close"}
        and quote.data_quality not in {DataQuality.STALE, DataQuality.MISSING}
    ):
        return (NEXT_OPEN_SIMULATION,)
    return ()


def validate_user_paper_simulation_mode(
    quote: MarketQuote,
    confirmation: Mapping[str, Any],
    *,
    allow_test_quote: bool = False,
) -> str:
    """Validate the explicit simulation contract against the authoritative quote."""
    mode = str(confirmation.get("simulation_mode") or "").strip()
    if mode not in USER_PAPER_SIMULATION_MODES:
        raise ValueError(
            "simulation_mode must be intraday_simulation or next_open_simulation"
        )

    if mode == INTRADAY_SIMULATION:
        if mode not in available_user_paper_simulation_modes(
            quote,
            allow_test_quote=allow_test_quote,
        ):
            raise ValueError(
                "intraday_simulation requires an actionable realtime or delayed quote"
            )
        return mode

    if mode not in available_user_paper_simulation_modes(
        quote,
        allow_test_quote=allow_test_quote,
    ):
        raise ValueError(
            "next_open_simulation requires an authoritative current_close or "
            "previous_close quote"
        )
    if confirmation.get("close_reference_acknowledged") is not True:
        raise ValueError(
            "next_open_simulation requires close_reference_acknowledged=true"
        )
    return mode


__all__ = [
    "INTRADAY_SIMULATION",
    "NEXT_OPEN_SIMULATION",
    "USER_PAPER_SIMULATION_MODES",
    "available_user_paper_simulation_modes",
    "validate_user_paper_simulation_mode",
]
