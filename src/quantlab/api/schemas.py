from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


MarketSymbol = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^(sh|sz)\d{6}$"),
]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
    account_id: str | None = Field(default=None, max_length=64)
    save: bool = True
    background: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


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


class TradeQuoteRequest(StrictRequest):
    symbol: MarketSymbol
    name: str = Field(default="", max_length=100)
    asset_type: Literal["stock", "etf", "convertible_bond", "index"]
    raw_price: float = Field(gt=0)
    as_of: date
    available_at: datetime | None = None
    source: str = Field(min_length=1, max_length=100)
    data_quality: Literal["available", "degraded", "stale", "missing"] = "available"
    degraded_from: list[str] = Field(default_factory=list, max_length=20)
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    is_st: bool = False
    industry: str | None = Field(default=None, max_length=100)
    trade_lot: int = Field(default=100, gt=0, le=100_000)
    t_plus_one: bool = True
    session_status: Literal["open", "closed", "unknown"] = "unknown"
    risk_metadata: dict = Field(default_factory=dict)


class UserPaperAccountRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=100)
    initial_capital: float = Field(default=100_000.0, gt=0, le=1_000_000_000)
    benchmark_symbol: MarketSymbol = "sh000300"
    idempotency_key: str = Field(min_length=8, max_length=128)


class UserPaperNewSeasonRequest(StrictRequest):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    initial_capital: float | None = Field(default=None, gt=0, le=1_000_000_000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class UserPreTradeRequest(StrictRequest):
    account_id: str = Field(min_length=1, max_length=64)
    symbol: MarketSymbol
    side: Literal["buy", "sell"]
    quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    amount: float | None = Field(default=None, gt=0, le=1_000_000_000)
    research_run_id: str | None = Field(default=None, max_length=100)
    user_context: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def quantity_or_amount(self) -> "UserPreTradeRequest":
        if (self.quantity is None) == (self.amount is None):
            raise ValueError("provide exactly one of quantity or amount")
        return self


class UserOrderConfirmation(StrictRequest):
    confirmed: Literal[True]
    check_id: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    symbol: MarketSymbol
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, le=100_000_000)
    source: str = Field(min_length=1, max_length=64)
    simulation_mode: Literal["intraday_simulation", "next_open_simulation"]
    close_reference_acknowledged: bool = False

    @model_validator(mode="after")
    def requires_close_reference_acknowledgement(self) -> "UserOrderConfirmation":
        if (
            self.simulation_mode == "next_open_simulation"
            and not self.close_reference_acknowledged
        ):
            raise ValueError(
                "next_open_simulation requires close_reference_acknowledged=true"
            )
        return self


class UserOrderConfirmRequest(StrictRequest):
    check_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0, le=100_000_000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    user_confirmation: UserOrderConfirmation

    @model_validator(mode="after")
    def confirmation_matches_request(self) -> "UserOrderConfirmRequest":
        if self.user_confirmation.check_id != self.check_id:
            raise ValueError("confirmation check_id does not match request")
        if self.user_confirmation.quantity != self.quantity:
            raise ValueError("confirmation quantity does not match request")
        return self


class UserOrderSettlementRequest(StrictRequest):
    fill_quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    fill_key: str = Field(min_length=8, max_length=128)


class UserOrderCancelRequest(StrictRequest):
    reason: str = Field(default="cancelled_by_user", min_length=1, max_length=300)


class UserAccountMarkRequest(StrictRequest):
    pass


class UserTradeReviewRequest(StrictRequest):
    order_id: str | None = Field(default=None, max_length=64)
    symbol: MarketSymbol | None = None
    review_type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class ChatConversationRequest(StrictRequest):
    title: str = Field(default="QuantLab会话", min_length=1, max_length=100)
    account_id: str | None = Field(default=None, max_length=64)
    symbol: MarketSymbol | None = None
    research_run_id: str | None = Field(default=None, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ChatMessageRequest(StrictRequest):
    content: str = Field(min_length=1, max_length=4_000)
    account_id: str | None = Field(default=None, max_length=64)
    symbol: MarketSymbol | None = None
    quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    research_run_id: str | None = Field(default=None, max_length=100)
    allow_research: bool = False
    background: bool | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class ChatActionConfirmRequest(StrictRequest):
    quantity: int | None = Field(default=None, gt=0, le=100_000_000)
    simulation_mode: Literal["intraday_simulation", "next_open_simulation"] | None = None
    close_reference_acknowledged: bool = False


class NotificationPreferenceItem(StrictRequest):
    notification_type: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    minimum_severity: Literal["info", "warning", "critical"] = "info"
    cooldown_seconds: int = Field(default=300, ge=0, le=604_800)


class NotificationPreferencesRequest(StrictRequest):
    preferences: list[NotificationPreferenceItem] = Field(max_length=200)


class ContextPackBuildRequest(StrictRequest):
    symbol: MarketSymbol
    as_of: date | None = None
    asset_type: Literal["stock", "etf", "convertible_bond", "index"] | None = None
    account_id: str | None = Field(default=None, max_length=64)
    include_events: bool = True
    save: bool = True


class CapitalFlowCalculateRequest(StrictRequest):
    scope: Literal["market", "industry", "stock"]
    as_of: date
    source: str = Field(min_length=1, max_length=100)
    methodology: str = Field(min_length=1, max_length=1_000)
    symbol: MarketSymbol | None = None
    records: list[dict] = Field(min_length=1, max_length=20_000)
    supplemental: dict = Field(default_factory=dict)
    save: bool = True
    account_id: str | None = Field(default=None, max_length=64)


class ContextCommitteeRequest(StrictRequest):
    context_id: str = Field(min_length=1, max_length=64)
    deterministic_max_weight: float = Field(ge=0, le=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RoleObservationRequest(StrictRequest):
    role: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=100)
    symbol: MarketSymbol
    as_of: date
    horizon_days: int = Field(gt=0, le=252)
    probabilities: dict[str, float]
    realized_direction: Literal["up", "flat", "down"]
    realized_return_pct: float
    market_regime: str | None = Field(default=None, max_length=64)
    drawdown_reduction: float | None = None
    fact_errors: int = Field(default=0, ge=0)
    quant_incremental_return_pct: float | None = None
    cost_usd: float = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    payload: dict = Field(default_factory=dict)


class RoleChallengeDecisionRequest(StrictRequest):
    passed: bool
    decision: Literal["promote", "reject"]
    reason: str = Field(min_length=1, max_length=1_000)
    applicable_regimes: list[str] = Field(
        default_factory=lambda: ["all"], min_length=1, max_length=20
    )


class NotificationRuleRequest(StrictRequest):
    rule_type: Literal[
        "market_turnover_ratio_above",
        "flow_positive_streak",
        "flow_negative_streak",
        "flow_price_divergence",
        "flow_data_unavailable",
    ]
    idempotency_key: str = Field(min_length=8, max_length=128)
    account_id: str | None = Field(default=None, max_length=64)
    symbol: MarketSymbol | None = None
    industry: str | None = Field(default=None, max_length=100)
    threshold: float | None = None
    consecutive_periods: int = Field(default=2, ge=1, le=60)
    cooldown_seconds: int = Field(default=3_600, ge=0, le=2_592_000)
    enabled: bool = True
    created_source: str = Field(default="api", min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class BackgroundJobRequest(StrictRequest):
    job_type: Literal[
        "research",
        "historical_replay",
        "capital_flow_refresh",
        "training",
        "simulator_settlement",
        "daily_cycle",
        "notification_dispatch",
        "premarket_digest",
        "account_daily_report",
        "forward_settlement_scan",
        "mark_to_market",
        "a_share_v4_research",
        "convertible_bond_research",
        "etf_pit_replay",
        "retention_cleanup",
        "database_backup",
        "thesis_due_scan",
        "thesis_event_check",
        "thesis_price_invalidation_check",
        "authoritative_reflection_settlement",
        "controlled_memory_refresh",
        "decision_task_refresh",
    ]
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=128)
    priority: int = Field(default=100, ge=0, le=1_000)
    timeout_seconds: int = Field(default=900, ge=1, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=20)
    cost_budget_usd: float = Field(default=0.0, ge=0, le=10_000)
    dependency_job_ids: list[str] = Field(default_factory=list, max_length=50)


class JobCancelRequest(StrictRequest):
    reason: str = Field(default="cancelled_by_user", min_length=1, max_length=500)


class WorkerRunRequest(StrictRequest):
    worker_id: str = Field(default="api-worker", min_length=1, max_length=100)
    maximum_jobs: int = Field(default=1, ge=1, le=100)


class ScheduleRunRequest(StrictRequest):
    run_date: date | None = None
    backfill: bool = False


class TradingCalendarItem(StrictRequest):
    trade_date: date
    is_open: bool
    source: str = Field(min_length=1, max_length=100)
    available_at: datetime
    quality: Literal["available", "degraded"] = "available"


class TradingCalendarBatchRequest(StrictRequest):
    items: list[TradingCalendarItem] = Field(min_length=1, max_length=10_000)


class PointInTimeSecurityRequest(StrictRequest):
    symbol: str = Field(min_length=8, max_length=20)
    name: str = Field(default="", max_length=100)
    security_type: Literal["etf", "stock", "convertible_bond"]
    exchange: str = Field(min_length=1, max_length=20)
    listing_date: date
    delisting_date: date | None = None
    asset_class: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    status: str = Field(default="listed", min_length=1, max_length=50)
    source: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=100)
    available_at: datetime
    payload: dict = Field(default_factory=dict)


class PointInTimeTradeStatusRequest(StrictRequest):
    symbol: str = Field(min_length=8, max_length=20)
    trade_date: date
    trade_status: bool
    suspended: bool = False
    is_st: bool = False
    amount: float | None = Field(default=None, ge=0)
    fund_size: float | None = Field(default=None, ge=0)
    turnover_rate: float | None = Field(default=None, ge=0)
    premium_discount_pct: float | None = None
    remaining_balance: float | None = Field(default=None, ge=0)
    redeem_status: str | None = Field(default=None, max_length=100)
    rating: str | None = Field(default=None, max_length=50)
    overseas_market_date: date | None = None
    source: str = Field(min_length=1, max_length=100)
    methodology: str = Field(min_length=1, max_length=500)
    available_at: datetime
    payload: dict = Field(default_factory=dict)


class EtfPointInTimePoolRequest(StrictRequest):
    snapshot_date: date
    cutoff_at: datetime
    source_version: str = Field(min_length=1, max_length=100)
    master_records: list[PointInTimeSecurityRequest] = Field(min_length=1, max_length=10_000)
    trade_statuses: list[PointInTimeTradeStatusRequest] = Field(
        min_length=1, max_length=100_000
    )
    minimum_amount: float = Field(default=0.0, ge=0)
    minimum_fund_size: float = Field(default=0.0, ge=0)
    save: bool = True


class PointInTimeMasterBatchRequest(StrictRequest):
    master_version: str = Field(min_length=1, max_length=100)
    records: list[PointInTimeSecurityRequest] = Field(min_length=1, max_length=100_000)


class PointInTimeStatusBatchRequest(StrictRequest):
    security_type: Literal["etf", "stock", "convertible_bond"]
    records: list[PointInTimeTradeStatusRequest] = Field(min_length=1, max_length=200_000)


class AShareV4PoolRequest(StrictRequest):
    snapshot_date: date
    cutoff_at: datetime
    records: list[dict] = Field(min_length=1, max_length=10_000)
    correlations: dict[str, float] = Field(default_factory=dict)
    risk_on: bool
    source: str = Field(min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=100)
    save: bool = True


class ConvertibleBondPoolRequest(StrictRequest):
    snapshot_date: date
    cutoff_at: datetime
    source_version: str = Field(min_length=1, max_length=100)
    master_records: list[PointInTimeSecurityRequest] = Field(min_length=1, max_length=10_000)
    trade_statuses: list[PointInTimeTradeStatusRequest] = Field(
        min_length=1, max_length=100_000
    )
    minimum_remaining_balance: float = Field(default=100_000_000.0, ge=0)
    minimum_amount: float = Field(default=10_000_000.0, ge=0)
    save: bool = True


class ForwardCohortRequest(StrictRequest):
    pass


class ForwardSampleRequest(StrictRequest):
    cohort_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=20)
    account_id: str | None = Field(default=None, max_length=64)
    horizon_days: Literal[5, 20]


class ForwardSettlementRequest(StrictRequest):
    cohort_id: str = Field(min_length=1, max_length=64)
    sample_key: str = Field(min_length=1, max_length=200)
    horizon_days: Literal[5, 20]


class InvestorPortfolioRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=100)
    cash: float = Field(default=0.0, ge=0)


class InvestorCsvPreviewRequest(StrictRequest):
    import_type: Literal["positions", "trades"]
    csv_content: str = Field(min_length=1, max_length=5_000_000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class InvestorImportConfirmRequest(StrictRequest):
    confirm: bool


class InvestorTradeRequest(StrictRequest):
    symbol: str = Field(min_length=1, max_length=20)
    asset_type: Literal["stock", "etf", "convertible_bond"] = "stock"
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    transaction_cost: float = Field(default=0.0, ge=0)
    trade_date: date
    idempotency_key: str = Field(min_length=8, max_length=128)


class InvestorRecommendationRequest(StrictRequest):
    symbol: str = Field(min_length=1, max_length=20)
    side_hint: Literal["buy", "sell", "hold", "review"] | None = None


class InvestorAdoptionRequest(StrictRequest):
    decision: Literal["adopted", "partially_adopted", "rejected", "user_override"]
    trade_side: Literal["buy", "sell"] | None = None
    actual_quantity: int | None = Field(default=None, gt=0)
    actual_price: float | None = Field(default=None, gt=0)
    actual_trade_date: date | None = None
    transaction_cost: float = Field(default=0.0, ge=0)
    note: str | None = Field(default=None, max_length=2_000)


class InvestmentThesisCheckRequest(StrictRequest):
    context_id: str = Field(min_length=1, max_length=128)
    context_fingerprint: str = Field(min_length=64, max_length=128)
    evidence_refs: list[dict] = Field(default_factory=list, max_length=200)
    user_resolution: Literal["confirmed", "ignored", "closed"] = "confirmed"


class InvestmentThesisDraftEditRequest(StrictRequest):
    core_logic: str = Field(min_length=10, max_length=800)
    assumptions: list[dict] = Field(min_length=3, max_length=7)
    valuation_anchor: str = Field(min_length=1, max_length=500)
    overall_red_lines: list[str] = Field(default_factory=list, max_length=12)
    overall_invalidation_conditions: list[str] = Field(default_factory=list, max_length=12)
    needs_review: bool = True


class DecisionTaskStatusRequest(StrictRequest):
    status: Literal["open", "acknowledged", "resolved", "dismissed"]
    reason: str = Field(default="user_status_update", min_length=1, max_length=200)


class ExperimentRunRequest(StrictRequest):
    experiment_name: str = Field(min_length=1, max_length=200)
    experiment_type: str = Field(min_length=1, max_length=100)
    run_type: str = Field(min_length=1, max_length=100)
    evidence_boundary: Literal["production", "forward_shadow", "user", "demo", "test", "research_only"]
    idempotency_key: str = Field(min_length=8, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=100)
    dataset_fingerprint: str | None = Field(default=None, max_length=128)
    universe_fingerprint: str | None = Field(default=None, max_length=128)
    context_fingerprint: str | None = Field(default=None, max_length=128)
    quote_fingerprint: str | None = Field(default=None, max_length=128)
    model_routing: dict = Field(default_factory=dict)
    parameters: dict = Field(default_factory=dict)
    cost_budget: dict = Field(default_factory=dict)


class ExperimentArtifactRequest(StrictRequest):
    artifact_type: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    payload: dict = Field(default_factory=dict)
    uri: str | None = Field(default=None, max_length=500)


class NextTradingDayAcceptanceRequest(StrictRequest):
    trade_date: date


class SmoothedRebalanceRequest(StrictRequest):
    nav: float = Field(gt=0)
    available_cash: float = Field(ge=0)
    current_quantities: dict[str, int]
    desired_weights: dict[str, float]
    prices: dict[str, float]
    sellable_quantities: dict[str, int] | None = None
    evidence_degraded: bool = False
    policy: dict = Field(default_factory=dict)


class NotificationChannelRequest(StrictRequest):
    channel: Literal["in_app", "email", "feishu", "desktop"]
    enabled: bool
    account_id: str | None = Field(default=None, max_length=64)
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    daily_maximum: int = Field(default=50, ge=1, le=10_000)
    config: dict = Field(default_factory=dict)


class NotificationEmailTestRequest(StrictRequest):
    account_id: str | None = Field(default=None, max_length=64)


class StrategyEvidenceRunRequest(StrictRequest):
    episodes: list[dict] = Field(min_length=1, max_length=5_000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    source: str = Field(default="point_in_time_dataset", min_length=1, max_length=100)
    source_version: str = Field(min_length=1, max_length=100)
    save: bool = True
    options: dict = Field(default_factory=dict)


class BackupRequest(StrictRequest):
    label: str = Field(default="manual", min_length=1, max_length=100)


class RestoreRequest(StrictRequest):
    backup_path: str = Field(min_length=1, max_length=1_000)
    expected_sha256: str = Field(min_length=64, max_length=64)
    confirm: bool
