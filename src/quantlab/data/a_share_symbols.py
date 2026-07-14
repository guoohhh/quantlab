from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date


@dataclass(frozen=True)
class AShareSymbolAlias:
    historical_symbol: str
    canonical_symbol: str
    effective_date: date
    source: str
    note: str


# Security-code changes are rare but materially important for point-in-time replays: treating
# the two codes as unrelated securities truncates the old code at the change date and creates
# false delisting losses. Keep this registry small, explicit and reviewable instead of guessing
# aliases from similar names or prices.
_ALIASES = (
    AShareSymbolAlias(
        historical_symbol="sz300114",
        canonical_symbol="sz302132",
        effective_date=date(2025, 2, 18),
        source="SZSE security master and BaoStock point-in-time universe",
        note="中航电测 changed security code to 中航成飞 after the major asset restructuring",
    ),
)

ALIAS_REGISTRY_VERSION = "a-share-symbol-aliases-v1"
_BY_HISTORICAL = {item.historical_symbol: item for item in _ALIASES}


def canonical_a_share_symbol(symbol: str) -> str:
    normalized = str(symbol).strip().lower()
    alias = _BY_HISTORICAL.get(normalized)
    return alias.canonical_symbol if alias else normalized


def a_share_symbol_aliases() -> list[dict[str, str]]:
    return [
        {
            **asdict(item),
            "effective_date": item.effective_date.isoformat(),
        }
        for item in _ALIASES
    ]
