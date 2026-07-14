from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


MarketSymbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^(sh|sz)\d{6}$"),
]


class WatchlistRequest(BaseModel):
    symbol: MarketSymbol
    name: str = Field(default="", max_length=100)
    group_name: str = Field(default="default", max_length=50)
    notes: str = Field(default="", max_length=2_000)


class SignalRequest(BaseModel):
    symbol: MarketSymbol
    strategy: str = Field(min_length=1, max_length=64)
    score: float = Field(ge=-1, le=1)
    action: str = Field(min_length=1, max_length=32)
    as_of: date
    payload: dict = Field(default_factory=dict)


class AlertRequest(BaseModel):
    symbol: MarketSymbol
    condition_type: Literal["price_above", "price_below", "score_above", "score_below"]
    threshold: float


class CapitalRequest(BaseModel):
    capital: float = Field(gt=0)


class RiskProfileRequest(BaseModel):
    profile: Literal["conservative", "balanced", "aggressive"]


class ManualTradeRequest(BaseModel):
    symbol: MarketSymbol
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(default=0, ge=0)
    trade_date: date
    notes: str = Field(default="", max_length=2_000)


class PortfolioPlanRequest(BaseModel):
    as_of: date | None = None
    reversal_limit: int = Field(default=10, ge=0, le=50)
    check_stock_risks: bool = True
    save: bool = True


class CandidateTournamentRequest(BaseModel):
    as_of: date | None = None
    candidate_limit: int = Field(default=3, ge=2, le=6)
    shortlist_size: int = Field(default=2, ge=1, le=6)
    max_correlation: float = Field(default=0.8, ge=0, le=1)
    save: bool = True


class CandidateTournamentSettlementRequest(BaseModel):
    as_of: date | None = None
    limit: int = Field(default=20, ge=1, le=200)


class StockScreenRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=20)
    as_of: date | None = None
    top_n: int = Field(default=10, ge=1, le=20)
    max_correlation: float = Field(default=0.85, ge=0, le=1)
    save: bool = True


class StockRecommendationRequest(BaseModel):
    as_of: date | None = None
    styles: list[str] = Field(default_factory=list, max_length=5)
    candidate_limit: int = Field(default=30, ge=5, le=100)
    top_n: int = Field(default=10, ge=1, le=20)
    max_correlation: float = Field(default=0.85, ge=0, le=1)
    save: bool = True


class StockResearchBatchRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=5)
    as_of: date | None = None
    include_events: bool = True
    save: bool = True


class StockRankingReplayRequest(BaseModel):
    symbols: list[str] = Field(min_length=2, max_length=20)
    start: date
    end: date
    horizon_days: Literal[5, 20] = 20
    episodes: int = Field(default=12, ge=1, le=60)
    top_k: int = Field(default=3, ge=1, le=5)
    max_correlation: float = Field(default=0.85, ge=0, le=1)
    save: bool = True
    record_learning_samples: bool = True


class StockPaperCycleRequest(BaseModel):
    symbols: list[str] = Field(min_length=2, max_length=20)
    as_of: date | None = None
    top_n: int = Field(default=3, ge=1, le=5)
    max_correlation: float = Field(default=0.85, ge=0, le=1)
    run_research: bool = False
    research_limit: int = Field(default=2, ge=0, le=5)


class StockMarketReplayRequest(BaseModel):
    start: date
    end: date
    horizon_days: Literal[5, 20] = 5
    episodes: int = Field(default=12, ge=1, le=60)
    sample_size: int = Field(default=60, ge=12, le=200)
    top_k: int = Field(default=3, ge=1, le=5)
    max_correlation: float = Field(default=0.85, ge=0, le=1)
    save: bool = True
    record_learning_samples: bool = True


class ResearchRequest(BaseModel):
    symbol: MarketSymbol
    as_of: date | None = None
    asset_type: Literal["stock", "etf"] | None = None
    include_events: bool = True
    save: bool = True


class PaperCycleRequest(BaseModel):
    as_of: date | None = None
    run_research: bool = False
    research_limit: int = Field(default=1, ge=0, le=2)


class DailyCycleRequest(BaseModel):
    as_of: date | None = None
    run_research: bool = False


class HistoricalReplayRequest(BaseModel):
    start: date
    end: date
    horizon_days: Literal[5, 20] = 20
    episodes: int = Field(default=3, ge=1, le=60)
    save: bool = True
    confirm_large_run: bool = False


class WalkForwardRequest(BaseModel):
    start: date
    end: date
    train_days: int | None = Field(default=None, ge=120)
    test_days: int | None = Field(default=None, ge=20)
    save: bool = True


class EtfVariantResearchRequest(BaseModel):
    start: date
    end: date
    strategy_variant: Literal["legacy", "adaptive_v1", "adaptive_v2", "adaptive_v3"] = "adaptive_v2"


class LLMReplayRequest(BaseModel):
    suite: Literal["smoke", "committee"] = "smoke"
    runs: int = Field(default=1, ge=1, le=5)
    save: bool = True


class RoundtableRequest(BaseModel):
    source_run_id: str = Field(min_length=8, max_length=64)
    participants: list[str] = Field(min_length=2, max_length=8)
    topic: str = Field(min_length=1, max_length=1_000)
    rounds: int = Field(default=2, ge=1, le=3)
    save: bool = True


class MarketEventRequest(BaseModel):
    symbol: MarketSymbol
    event_date: date
    event_type: Literal["news", "earnings", "regulatory", "corporate_action", "macro", "other"]
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=500)
    sentiment: float = Field(default=0, ge=-1, le=1)
    impact_score: float = Field(default=0.5, ge=0, le=1)
    payload: dict = Field(default_factory=dict)
