from __future__ import annotations

import re
import time
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from pydantic import BaseModel

from quantlab.config import Settings
from quantlab.domain import (
    AnalysisContextPack,
    ChatEvidenceAnswer,
    EvidenceDomain,
    MarketQuote,
    ResearchProvenance,
)
from quantlab.domain.data_governance import DataTrustLevel
from quantlab.execution import (
    NEXT_OPEN_SIMULATION,
    available_user_paper_simulation_modes,
)
from quantlab.market import InMemoryQuoteProvider, QuoteService
from quantlab.llm import await_with_provider_close, build_provider
from quantlab.llm.governance import GovernedLLMProvider, budget_from_settings
from quantlab.persistence import (
    ChatRepository,
    DecisionRepository,
    EvidenceRepository,
    NotificationRepository,
    TerminalRepository,
    Round8Repository,
)
from quantlab.persistence.chat import set_current_chat_job
from quantlab.reporting import research_persistence_context
from quantlab.security import safe_error_detail
from quantlab.workflows.research import analyze_symbol
from quantlab.workflows.simulator import (
    load_latest_trade_quote,
    run_pretrade_check,
    submit_user_paper_order,
    user_simulator_repository,
)
from quantlab.workflows.context import build_analysis_context_pack
from quantlab.workflows.research_identity import validate_research_record
from quantlab.workflows.stock_discovery import normalize_stock_symbol, search_stocks


@dataclass(frozen=True)
class ChatToolSpec:
    name: str
    permission: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    input_schema: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    cost_budget_usd: float = 0.0
    data_domains: tuple[str, ...] = ()


class ChatToolRegistry:
    """Explicit allow-list; model text can never select arbitrary functions or URLs."""

    def __init__(self, settings: Settings, conversation: dict[str, Any]):
        self.settings = settings
        self.conversation = conversation
        simulator = user_simulator_repository(settings)
        notifications = NotificationRepository(simulator.path)
        terminal = TerminalRepository(simulator.path)
        evidence = EvidenceRepository(simulator.path)
        decisions = DecisionRepository(simulator.path)
        lifecycle = Round8Repository(simulator.path)
        self.tools: dict[str, ChatToolSpec] = {
            "query_account": ChatToolSpec(
                "query_account",
                "read",
                lambda args: simulator.overview(self._account_id(args)),
                input_schema={"account_id": "bound_account|account_id"},
                data_domains=("account", "portfolio"),
            ),
            "query_positions": ChatToolSpec(
                "query_positions",
                "read",
                lambda args: {
                    "positions": simulator.positions(self._account_id(args))
                },
                input_schema={"account_id": "bound_account|account_id"},
                data_domains=("account", "position", "portfolio"),
            ),
            "query_orders_and_fills": ChatToolSpec(
                "query_orders_and_fills",
                "read",
                lambda args: {
                    "orders": simulator.orders(self._account_id(args), limit=100),
                    "fills": simulator.fills(self._account_id(args), limit=100),
                },
                input_schema={"account_id": "bound_account|account_id"},
                data_domains=("account", "order", "fill"),
            ),
            "query_performance": ChatToolSpec(
                "query_performance",
                "read",
                lambda args: simulator.performance(self._account_id(args)),
                input_schema={"account_id": "bound_account|account_id"},
                data_domains=("account", "performance", "benchmark"),
            ),
            "query_latest_quote": ChatToolSpec(
                "query_latest_quote",
                "read",
                lambda args: load_latest_trade_quote(
                    settings,
                    str(args["symbol"]),
                    asset_type=args.get("asset_type"),
                ).model_dump(mode="json"),
                input_schema={
                    "symbol": "market_symbol",
                    "asset_type": "stock|etf|convertible_bond|index|null",
                },
                data_domains=("market", "quote"),
            ),
            "query_research": ChatToolSpec(
                "query_research",
                "read",
                lambda args: _load_or_run_research(
                    settings,
                    str(args["symbol"]),
                    self._research_run_id(args),
                    False,
                    args.get("asset_type"),
                ),
                input_schema={
                    "symbol": "market_symbol",
                    "research_run_id": "string|null",
                    "asset_type": "string|null",
                },
                data_domains=("research", "reviewer", "strategy"),
            ),
            "run_or_reuse_research": ChatToolSpec(
                "run_or_reuse_research",
                "controlled_write",
                lambda args: _load_or_run_research(
                    settings,
                    str(args["symbol"]),
                    self._research_run_id(args),
                    True,
                    args.get("asset_type"),
                ),
                input_schema={
                    "symbol": "market_symbol",
                    "research_run_id": "string|null",
                    "asset_type": "string|null",
                },
                timeout_seconds=120,
                cost_budget_usd=float(
                    settings.get("llm.maximum_task_cost_usd", 1.0)
                ),
                data_domains=("research", "reviewer", "strategy", "llm"),
            ),
            "query_reviewer": ChatToolSpec(
                "query_reviewer",
                "read",
                lambda args: _reviewer_summary(
                    _load_or_run_research(
                        settings,
                        str(args["symbol"]),
                        self._research_run_id(args),
                        False,
                        args.get("asset_type"),
                    )
                ),
                input_schema={
                    "symbol": "market_symbol",
                    "research_run_id": "string|null",
                    "asset_type": "string|null",
                },
                data_domains=("research", "reviewer"),
            ),
            "query_constraints": ChatToolSpec(
                "query_constraints",
                "read",
                lambda _args: {
                    "maximum_total_exposure": settings.get(
                        "risk.max_total_exposure", 0.8
                    ),
                    "maximum_single_weight": settings.get(
                        "risk.max_single_position", 0.15
                    ),
                    "maximum_industry_weight": settings.get(
                        "risk.max_industry_exposure", 0.30
                    ),
                    "market_data_maximum_business_day_age": 1,
                    "hard_gates_cannot_be_relaxed_by_chat": True,
                },
                input_schema={"account_id": "bound_account|account_id|null"},
                data_domains=("risk", "portfolio", "execution"),
            ),
            "query_notifications": ChatToolSpec(
                "query_notifications",
                "read",
                lambda args: {
                    "notifications": notifications.list(
                        account_id=self._account_id(args),
                        unread_only=bool(args.get("unread_only", False)),
                        limit=50,
                    )
                },
                input_schema={
                    "account_id": "bound_account|account_id",
                    "unread_only": "boolean",
                },
                data_domains=("notification", "account"),
            ),
            "query_context_pack": ChatToolSpec(
                "query_context_pack",
                "read",
                lambda args: self._context_for_symbol(
                    evidence,
                    str(args["symbol"]),
                    as_of=args.get("as_of"),
                ),
                input_schema={"symbol": "market_symbol", "as_of": "date|null"},
                data_domains=("market", "technical", "capital_flow", "financial", "event", "macro", "portfolio", "strategy"),
            ),
            "query_market_flow": ChatToolSpec(
                "query_market_flow",
                "read",
                lambda args: {
                    "flows": evidence.flows("market", as_of=args.get("as_of"), limit=5)
                },
                input_schema={"as_of": "date|null"},
                data_domains=("capital_flow", "market"),
            ),
            "query_industry_flow": ChatToolSpec(
                "query_industry_flow",
                "read",
                lambda args: {
                    "flows": evidence.flows(
                        "industry",
                        scope_key=args.get("industry"),
                        as_of=args.get("as_of"),
                        limit=20,
                    )
                },
                input_schema={"industry": "string|null", "as_of": "date|null"},
                data_domains=("capital_flow", "industry"),
            ),
            "query_stock_flow": ChatToolSpec(
                "query_stock_flow",
                "read",
                lambda args: {
                    "flows": evidence.flows(
                        "stock",
                        scope_key=str(args["symbol"]),
                        as_of=args.get("as_of"),
                        limit=20,
                    )
                },
                input_schema={"symbol": "market_symbol", "as_of": "date|null"},
                data_domains=("capital_flow", "stock"),
            ),
            "query_macro_evidence": ChatToolSpec(
                "query_macro_evidence",
                "read",
                lambda args: _context_domain(
                    self._context_for_symbol(
                        evidence,
                        str(args["symbol"]),
                        as_of=args.get("as_of"),
                    ),
                    EvidenceDomain.MACRO.value,
                ),
                input_schema={"symbol": "market_symbol", "as_of": "date|null"},
                data_domains=("macro",),
            ),
            "query_events": ChatToolSpec(
                "query_events",
                "read",
                lambda args: _context_domain(
                    self._context_for_symbol(
                        evidence,
                        str(args["symbol"]),
                        as_of=args.get("as_of"),
                    ),
                    EvidenceDomain.EVENT.value,
                ),
                input_schema={"symbol": "market_symbol", "as_of": "date|null"},
                data_domains=("event", "news", "regulatory"),
            ),
            "compare_contexts": ChatToolSpec(
                "compare_contexts",
                "read",
                lambda args: {
                    "contexts": [
                        self._context_for_symbol(
                            evidence,
                            str(symbol),
                            as_of=args.get("as_of"),
                        )
                        for symbol in list(args["symbols"])[:5]
                    ]
                },
                input_schema={"symbols": "market_symbol[2..5]", "as_of": "date|null"},
                timeout_seconds=20,
                data_domains=("comparison", "market", "risk"),
            ),
            "query_decision_history": ChatToolSpec(
                "query_decision_history",
                "read",
                lambda args: {
                    "decisions": [
                        item
                        for item in decisions.recent(200)
                        if item.get("symbol") == str(args["symbol"])
                    ][:20]
                },
                input_schema={"symbol": "market_symbol"},
                data_domains=("research", "decision", "outcome"),
            ),
            "query_role_performance": ChatToolSpec(
                "query_role_performance",
                "read",
                lambda args: evidence.role_scorecard(
                    str(args["role"]),
                    int(settings.get("llm.role_minimum_matured_samples", 30)),
                ),
                input_schema={"role": "string"},
                data_domains=("llm_governance", "role_performance"),
            ),
            "query_investment_theses": ChatToolSpec(
                "query_investment_theses",
                "read",
                lambda args: {
                    "theses": lifecycle.theses(
                        portfolio_id=args.get("portfolio_id") or self.conversation.get("portfolio_id"),
                        statuses=tuple(args.get("statuses") or ()) or None,
                    )
                },
                input_schema={
                    "portfolio_id": "bound_portfolio|null",
                    "statuses": "string[]|null",
                },
                data_domains=("investment_thesis", "portfolio", "research"),
            ),
            "query_investment_thesis": ChatToolSpec(
                "query_investment_thesis",
                "read",
                lambda args: _require_thesis(lifecycle.thesis(str(args["thesis_id"]))),
                input_schema={"thesis_id": "uuid"},
                data_domains=("investment_thesis", "research", "evidence"),
            ),
            "query_research_memory": ChatToolSpec(
                "query_research_memory",
                "read",
                lambda args: {
                    "symbol": str(args["symbol"]),
                    "memories": lifecycle.memories(str(args["symbol"])),
                    "claim_boundary": (
                        "Only matured production/forward-shadow outcome lessons are returned; "
                        "they cannot automatically change strategy, weights or risk rules."
                    ),
                },
                input_schema={"symbol": "market_symbol"},
                data_domains=("research_memory", "outcome", "evidence"),
            ),
            "run_pretrade_check": ChatToolSpec(
                "run_pretrade_check",
                "controlled_write",
                lambda args: run_pretrade_check(
                    settings,
                    account_id=self._account_id(args),
                    symbol=str(args["symbol"]),
                    side=str(args["side"]),
                    quantity=int(args["quantity"]),
                    quote=(
                        MarketQuote.model_validate(args["_server_test_quote"])
                        if args.get("_server_test_quote")
                        else None
                    ),
                    asset_type=args.get("asset_type"),
                    research_run_id=self._research_run_id(args),
                    requested_at=datetime.now(UTC),
                    user_context={"source": "chat"},
                ),
                input_schema={
                    "account_id": "bound_account|account_id",
                    "symbol": "market_symbol",
                    "side": "buy|sell",
                    "quantity": "positive_integer",
                    "asset_type": "string|null",
                    "research_run_id": "string|null",
                },
                timeout_seconds=30,
                data_domains=("account", "market", "portfolio", "risk", "research"),
            ),
            "create_price_alert": ChatToolSpec(
                "create_price_alert",
                "controlled_write",
                lambda args: _create_alert(terminal, self._account_id(args), args),
                input_schema={
                    "account_id": "account_id",
                    "symbol": "market_symbol",
                    "condition_type": "price_above|price_below|position_weight_above",
                    "threshold": "number",
                },
                data_domains=("notification_rule", "market", "portfolio"),
            ),
            "create_flow_notification_rule": ChatToolSpec(
                "create_flow_notification_rule",
                "controlled_write",
                lambda args: notifications.create_rule(
                    rule_type=str(args["rule_type"]),
                    idempotency_key=str(args["idempotency_key"]),
                    account_id=self._account_id(args),
                    symbol=args.get("symbol"),
                    industry=args.get("industry"),
                    threshold=args.get("threshold"),
                    consecutive_periods=int(args.get("consecutive_periods", 2)),
                    cooldown_seconds=int(args.get("cooldown_seconds", 3_600)),
                    created_source="chat",
                ),
                input_schema={
                    "rule_type": "flow rule type",
                    "symbol": "market_symbol|null",
                    "industry": "string|null",
                    "threshold": "number|null",
                    "consecutive_periods": "integer",
                    "cooldown_seconds": "integer",
                },
                data_domains=("notification_rule", "capital_flow"),
            ),
            "mark_notification_read": ChatToolSpec(
                "mark_notification_read",
                "controlled_write",
                lambda args: {
                    "updated": notifications.mark_read(str(args["notification_id"]))
                },
                input_schema={"notification_id": "uuid"},
                data_domains=("notification",),
            ),
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools.get(name)
        if spec is None:
            raise ValueError("chat tool is not registered")
        requested_account = arguments.get("account_id")
        bound_account = self.conversation.get("account_id")
        if requested_account and bound_account and requested_account != bound_account:
            raise PermissionError("cross-account chat tool access is forbidden")
        return spec.handler(arguments)

    def permission(self, name: str) -> str:
        spec = self.tools.get(name)
        if spec is None:
            raise ValueError("chat tool is not registered")
        return spec.permission

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "permission": spec.permission,
                "input_schema": spec.input_schema,
                "read_only": spec.permission == "read",
                "confirmation_required": spec.permission == "controlled_write",
                "timeout_seconds": spec.timeout_seconds,
                "cost_budget_usd": spec.cost_budget_usd,
                "data_domains": list(spec.data_domains),
            }
            for spec in self.tools.values()
        ]

    def _account_id(self, arguments: dict[str, Any]) -> str:
        account_id = arguments.get("account_id") or self.conversation.get("account_id")
        if not account_id:
            raise ValueError("chat conversation is not bound to a simulator account")
        return str(account_id)

    def _research_run_id(self, arguments: dict[str, Any]) -> str | None:
        requested = arguments.get("research_run_id")
        bound = self.conversation.get("research_run_id")
        if requested and bound and str(requested) != str(bound):
            raise ValueError("conversation research run cannot be switched implicitly")
        value = requested or bound
        return str(value) if value else None

    def _context_for_symbol(
        self,
        evidence: EvidenceRepository,
        symbol: str,
        *,
        as_of: str | None,
    ) -> dict[str, Any]:
        resolved_symbol = normalize_stock_symbol(symbol)
        bound_symbol = (
            normalize_stock_symbol(str(self.conversation["symbol"]))
            if self.conversation.get("symbol")
            else None
        )
        if self.conversation.get("research_run_id") and resolved_symbol == bound_symbol:
            context = _require_context(
                _frozen_conversation_context(
                    self.settings,
                    self.conversation,
                    resolved_symbol,
                )
            )
            if as_of and str(context.get("as_of") or "")[:10] > str(as_of)[:10]:
                raise ValueError("bound research context is later than requested as_of")
            return context
        return _require_context(
            evidence.latest_context(
                resolved_symbol,
                as_of=as_of,
            )
        )


def _validated_research_binding(
    settings: Settings,
    *,
    symbol: str | None,
    research_run_id: str | None,
    asset_type: str | None = None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    resolved_symbol = normalize_stock_symbol(symbol) if symbol else None
    if not research_run_id:
        return resolved_symbol, None, None
    try:
        repository = DecisionRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        record = repository.get(str(research_run_id))
    except Exception as exc:
        raise RuntimeError(
            "research service unavailable; explicit run_id could not be validated"
        ) from exc
    identity = validate_research_record(
        record,
        run_id=str(research_run_id),
        symbol=resolved_symbol,
        asset_type=asset_type,
    )
    return resolved_symbol or str(identity["symbol"]), str(research_run_id), record


def _resolve_chat_request_identity(
    settings: Settings,
    conversation: dict[str, Any],
    *,
    requested_symbol: str | None,
    requested_run_id: str | None,
    content: str | None = None,
) -> tuple[str | None, str | None]:
    bound_symbol = (
        normalize_stock_symbol(str(conversation["symbol"]))
        if conversation.get("symbol")
        else None
    )
    explicit_symbol = normalize_stock_symbol(requested_symbol) if requested_symbol else None
    if explicit_symbol and bound_symbol and explicit_symbol != bound_symbol:
        raise ValueError("conversation symbol cannot be switched implicitly")

    bound_run_id = (
        str(conversation["research_run_id"])
        if conversation.get("research_run_id")
        else None
    )
    explicit_run_id = str(requested_run_id) if requested_run_id else None
    if explicit_run_id and bound_run_id and explicit_run_id != bound_run_id:
        raise ValueError("conversation research run cannot be switched implicitly")

    resolved_symbol = explicit_symbol or bound_symbol
    resolved_run_id = explicit_run_id or bound_run_id
    resolved_symbol, resolved_run_id, _record = _validated_research_binding(
        settings,
        symbol=resolved_symbol,
        research_run_id=resolved_run_id,
    )
    if content and _trade_intent(content):
        text_symbols = _symbols_from_text(content)
        if resolved_symbol and any(item != resolved_symbol for item in text_symbols):
            raise ValueError("trade symbol does not match the conversation symbol")
    return resolved_symbol, resolved_run_id


def create_chat_conversation(
    settings: Settings,
    *,
    title: str,
    account_id: str | None = None,
    symbol: str | None = None,
    research_run_id: str | None = None,
    page_scope: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if account_id:
        account = user_simulator_repository(settings).account(account_id)
        if account is None:
            raise ValueError("user paper account not found")
    resolved_symbol, resolved_run_id, _record = _validated_research_binding(
        settings,
        symbol=symbol,
        research_run_id=research_run_id,
    )
    return ChatRepository(
        settings.resolve(settings.get("system.database_path"))
    ).create_conversation(
        title=title,
        account_id=account_id,
        symbol=resolved_symbol,
        research_run_id=resolved_run_id,
        page_scope=page_scope,
        idempotency_key=idempotency_key,
    )


def handle_chat_message(
    settings: Settings,
    *,
    conversation_id: str,
    content: str,
    account_id: str | None = None,
    symbol: str | None = None,
    quantity: int | None = None,
    quote: MarketQuote | None = None,
    research_run_id: str | None = None,
    allow_research: bool = False,
    existing_user_message_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    set_current_chat_job(job_id)
    repository = ChatRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    conversation = repository.conversation(conversation_id)
    if conversation is None:
        raise ValueError("chat conversation not found")
    if account_id and conversation.get("account_id") and (
        account_id != conversation["account_id"]
    ):
        raise PermissionError("conversation account cannot be switched implicitly")
    if len(content) > 4_000:
        raise ValueError("chat message exceeds 4000 characters")
    bound_symbol, resolved_research_run_id = _resolve_chat_request_identity(
        settings,
        conversation,
        requested_symbol=symbol,
        requested_run_id=research_run_id,
        content=content,
    )
    effective_conversation = {
        **conversation,
        "symbol": bound_symbol,
        "research_run_id": resolved_research_run_id,
    }
    if existing_user_message_id:
        user_message = repository.message(existing_user_message_id)
        if user_message is None or user_message["conversation_id"] != conversation_id:
            raise ValueError("queued chat user message not found")
    else:
        user_message = repository.add_message(
            conversation_id=conversation_id,
            role="user",
            content=content,
            payload={
                "account_id": account_id,
                "symbol": bound_symbol,
                "quantity": quantity,
                "research_run_id": resolved_research_run_id,
            },
            input_tokens=max(1, len(content) // 4),
            job_id=job_id,
            idempotency_key=f"chat-job:{job_id}:user" if job_id else None,
        )
    if _is_forbidden_request(content):
        reply = (
            "该请求涉及密钥、系统提示、历史成交修改或绕过确认，QuantLab Chat不会执行。"
        )
        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=reply,
            payload={"status": "refused", "security_boundary": True},
            latency_ms=(time.perf_counter() - started) * 1000,
            status="rejected",
        )
        return {"message": assistant, "citations": [], "actions": []}

    registry = ChatToolRegistry(settings, effective_conversation)
    if quote is not None and not bool(settings.get("system.test_mode", False)):
        raise ValueError("Chat never accepts caller-supplied market quotes")
    if quote is not None:
        quote = quote.model_copy(
            update={
                "authoritative": False,
                "provider": "test_quote_provider",
                "evidence_stage": "test",
                "trust_level": DataTrustLevel.TEST,
                "license_status": "test_fixture",
                "endpoint": "chat/internal_test_quote",
            }
        )
    resolved_account = account_id or conversation.get("account_id")
    resolved_symbol = _resolve_symbol(settings, bound_symbol, content)
    trade_intent = _trade_intent(content)

    def _guidance_reply(reason: str) -> dict[str, Any]:
        """Fail-soft: answer with guidance instead of killing the background job."""

        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=reason,
            payload={"status": "guidance"},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return {"message": assistant, "citations": [], "actions": []}

    account_words = ("持仓", "账户", "现金", "盈亏", "净值", "委托", "成交", "订单", "通知")
    if not resolved_account and any(word in content for word in account_words):
        return _guidance_reply(
            "这个问题需要绑定模拟账户才能回答。请到「我的账户」创建一个模拟账户，"
            "或从账户相关页面打开 AI 助手再试；研究类问题不受影响。"
        )
    if trade_intent:
        if not resolved_account:
            return _guidance_reply(
                "创建订单草稿需要先绑定模拟账户。请到「我的账户」创建账户后，"
                "从账户或研究页面重新发起。"
            )
        if not resolved_symbol:
            return _guidance_reply(
                "我没有听出你要操作哪只标的。请带上明确的股票/ETF 代码再问，"
                "例如「买入 1000 股 sh510300」。"
            )
        simulator = user_simulator_repository(settings)
        if trade_intent == "sell" and ("清仓" in content or "全部卖" in content):
            position = next(
                (
                    item
                    for item in simulator.positions(resolved_account)
                    if item["symbol"] == resolved_symbol
                ),
                None,
            )
            quantity = int(position["sellable_quantity"]) if position else 0
        resolved_quantity = quantity or _quantity_from_text(content)
        if not resolved_quantity or resolved_quantity <= 0:
            raise ValueError("trade draft requires a positive share quantity")
        test_quote_service = (
            QuoteService(InMemoryQuoteProvider([quote]))
            if quote is not None
            else None
        )
        resolved_quote = load_latest_trade_quote(
            settings,
            resolved_symbol,
            quote_service=test_quote_service,
        )
        arguments = {
            "account_id": resolved_account,
            "symbol": resolved_symbol,
            "side": trade_intent,
            "quantity": resolved_quantity,
            "server_quote_fingerprint": resolved_quote.quote_fingerprint,
            **({"_server_test_quote": quote.model_dump(mode="json")} if quote else {}),
            "research_run_id": resolved_research_run_id,
        }
        check = _execute_tool(
            repository,
            registry,
            conversation_id,
            user_message["message_id"],
            "run_pretrade_check",
            arguments,
        )
        checked_quote = MarketQuote.model_validate(check["quote"])
        permitted_simulation_modes = available_user_paper_simulation_modes(
            checked_quote,
            allow_test_quote=bool(settings.get("system.test_mode", False)),
        )
        if not check["allowed_to_submit"]:
            failures = "; ".join(check["hard_failures"]) or "unknown_hard_risk_failure"
            assistant = repository.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=(
                    "交易前检查未通过硬风控，因此没有创建模拟交易确认草稿："
                    + failures
                ),
                payload={
                    "status": "blocked",
                    "pretrade_check": check,
                    "hard_failures": check["hard_failures"],
                },
                latency_ms=(time.perf_counter() - started) * 1000,
                data_as_of=resolved_quote.as_of.isoformat(),
                context_id=check.get("context_id"),
                context_version=check.get("context_version"),
                status="rejected",
            )
            return {"message": assistant, "citations": [], "actions": []}
        if not permitted_simulation_modes:
            reasons = ", ".join(checked_quote.actionability_reasons) or "quote_not_actionable"
            assistant = repository.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=(
                    "当前行情既不是可执行的实时行情，也不是可用于下一交易日模拟的"
                    "权威收盘参考价，因此没有创建确认草稿："
                    + reasons
                ),
                payload={
                    "status": "quote_not_submittable",
                    "pretrade_check": check,
                    "quote_actionability_reasons": checked_quote.actionability_reasons,
                },
                latency_ms=(time.perf_counter() - started) * 1000,
                data_as_of=resolved_quote.as_of.isoformat(),
                context_id=check.get("context_id"),
                context_version=check.get("context_version"),
                status="degraded",
                degraded_reason="quote_not_submittable",
            )
            return {"message": assistant, "citations": [], "actions": []}
        mode_summary = (
            "当前使用可执行实时行情；确认后会创建盘中模拟委托。"
            if permitted_simulation_modes == ("intraday_simulation",)
            else "当前仅有收盘参考价；确认后只会创建下一交易日待成交的模拟委托，不会立即成交。"
        )
        reply = (
            f"已完成{resolved_symbol}的交易前检查并创建草稿。"
            f"预计成交金额{check['estimated_gross_value']:.2f}元，"
            f"费用{check['estimated_transaction_fees']:.2f}元，"
            f"操作后现金{check['post_trade_cash']:.2f}元。"
            "草稿不会自动下单，必须通过独立确认接口。"
        )
        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=reply,
            payload={
                "status": "confirmation_required",
                "pretrade_check": check,
                "permitted_simulation_modes": list(permitted_simulation_modes),
            },
            latency_ms=(time.perf_counter() - started) * 1000,
            data_as_of=resolved_quote.as_of.isoformat(),
            context_id=check.get("context_id"),
            context_version=check.get("context_version"),
        )
        action = repository.create_action(
            conversation_id=conversation_id,
            message_id=assistant["message_id"],
            action_type="user_paper_order",
            account_id=resolved_account,
            symbol=resolved_symbol,
            research_run_id=check.get("research_run_id"),
            check_id=check["check_id"],
            draft_payload={
                "side": trade_intent,
                "quantity": resolved_quantity,
                "summary": mode_summary,
                "draft_check_id": check["check_id"],
                "draft_quote_fingerprint": resolved_quote.quote_fingerprint,
                "permitted_simulation_modes": list(permitted_simulation_modes),
                "requires_close_reference_acknowledgement": (
                    NEXT_OPEN_SIMULATION in permitted_simulation_modes
                ),
                "quote_submission_context": {
                    "quote_kind": checked_quote.quote_kind,
                    "session_status": checked_quote.session_status,
                    "actionable": checked_quote.actionable,
                    "authoritative": checked_quote.authoritative,
                },
                **(
                    {
                        "_server_test_quote": quote.model_dump(mode="json"),
                        "quote_boundary": "test/non_authoritative",
                    }
                    if quote is not None
                    else {}
                ),
            },
            idempotency_key=f"chat-draft:{conversation_id}:{user_message['message_id']}",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        NotificationRepository(
            settings.resolve(settings.get("system.database_path"))
        ).emit(
            event_type="chat_trade_draft",
            aggregate_type="chat_action",
            aggregate_id=action["action_id"],
            payload={
                "account_id": resolved_account,
                "symbol": resolved_symbol,
                "action_type": "confirm_chat_action",
                "action_payload": {"action_id": action["action_id"]},
            },
            dedup_key=f"chat_trade_draft:{action['action_id']}",
        )
        context_payload = (
            EvidenceRepository(
                settings.resolve(settings.get("system.database_path"))
            ).context(check["context_id"])
            if check.get("context_id")
            else None
        )
        context_citations = [
            {
                "data_type": f"context:{block['domain']}",
                "source": block["source"],
                "as_of": block["as_of"],
                "available_at": block["available_at"],
                "research_run_id": check.get("research_run_id"),
                "symbol": resolved_symbol,
                "data_quality": block["quality"],
                "degraded_status": "degraded" if block["degraded"] else None,
                "payload": {
                    "block_id": block["block_id"],
                    "fingerprint": block["fingerprint"],
                    "context_id": check.get("context_id"),
                    "context_version": check.get("context_version"),
                },
            }
            for block in (context_payload or {}).get("blocks", [])[:12]
        ]
        citations = repository.add_citations(
            assistant["message_id"],
            [
                {
                    "data_type": "market_quote",
                    "source": resolved_quote.source,
                    "as_of": resolved_quote.as_of.isoformat(),
                    "available_at": resolved_quote.available_at.isoformat()
                    if resolved_quote.available_at
                    else None,
                    "symbol": resolved_symbol,
                    "data_quality": resolved_quote.data_quality.value,
                    "degraded_status": ",".join(resolved_quote.degraded_from) or None,
                },
                {
                    "data_type": "pretrade_check",
                    "source": "quantlab_deterministic_rules",
                    "as_of": resolved_quote.as_of.isoformat(),
                    "research_run_id": check.get("research_run_id"),
                    "symbol": resolved_symbol,
                    "data_quality": "available",
                    "payload": {
                        "check_id": check["check_id"],
                        "hard_risk_passed": check["hard_risk_passed"],
                    },
                },
                *context_citations,
            ],
        )
        return {"message": assistant, "citations": citations, "actions": [action]}

    comparison_symbols = _symbols_from_text(content)
    if resolved_symbol and resolved_symbol not in comparison_symbols:
        comparison_symbols.insert(0, resolved_symbol)
    if _is_context_question(content) and not _is_alert_request(content) and comparison_symbols:
        packs: list[AnalysisContextPack] = []
        evidence_repository = EvidenceRepository(
            settings.resolve(settings.get("system.database_path"))
        )
        for item_symbol in comparison_symbols[:5]:
            if resolved_research_run_id and item_symbol == bound_symbol:
                try:
                    payload = _frozen_conversation_context(
                        settings,
                        effective_conversation,
                        item_symbol,
                    )
                except ValueError as exc:
                    repository.record_tool_call(
                        conversation_id=conversation_id,
                        message_id=user_message["message_id"],
                        tool_name="query_bound_research_context",
                        permission="read",
                        arguments={
                            "symbol": item_symbol,
                            "research_run_id": resolved_research_run_id,
                        },
                        result=None,
                        status="error",
                        error_detail=safe_error_detail(exc),
                    )
                    assistant = repository.add_message(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=(
                            "绑定研究报告的精确AnalysisContextPack不可用，已阻止切换到"
                            "同标的其他报告。"
                        ),
                        payload={
                            "status": "degraded",
                            "research_run_id": resolved_research_run_id,
                            "missing_data": ["bound_research_context_unavailable"],
                        },
                        latency_ms=(time.perf_counter() - started) * 1000,
                        status="degraded",
                        degraded_reason="bound_research_context_unavailable",
                    )
                    return {"message": assistant, "citations": [], "actions": []}
            else:
                payload = evidence_repository.latest_context(item_symbol)
            if payload is None and allow_research:
                payload = build_analysis_context_pack(
                    settings,
                    symbol=item_symbol,
                    account_id=resolved_account,
                    include_events=True,
                    save=True,
                )
            if payload is not None:
                packs.append(AnalysisContextPack.model_validate(payload))
            if payload is not None and any(word in content for word in ("上次", "变化", "之前")):
                for historical in evidence_repository.contexts(item_symbol, 2)[1:2]:
                    packs.append(AnalysisContextPack.model_validate(historical))
        if not packs:
            assistant = repository.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content="没有截止日内可用的AnalysisContextPack，无法确认当前市场事实。",
                payload={
                    "status": "degraded",
                    "facts": [],
                    "llm_judgments": [],
                    "missing_data": ["analysis_context_pack_unavailable"],
                },
                latency_ms=(time.perf_counter() - started) * 1000,
                status="degraded",
                degraded_reason="analysis_context_pack_unavailable",
            )
            return {"message": assistant, "citations": [], "actions": []}
        repository.record_tool_call(
            conversation_id=conversation_id,
            message_id=user_message["message_id"],
            tool_name="compare_contexts" if len(packs) > 1 else "query_context_pack",
            permission="read",
            arguments={
                "symbols": [pack.symbol for pack in packs],
                "question_type": "context_grounded_chat",
            },
            result={
                "context_ids": [pack.context_id for pack in packs],
                "context_versions": [pack.schema_version for pack in packs],
                "cutoffs": [pack.cutoff_at.isoformat() for pack in packs],
            },
            status="ok",
        )
        answer, governance = _answer_context_question(
            settings,
            packs=packs,
            question=content,
            task_id=f"chat:{conversation_id}:{user_message['message_id']}",
        )
        primary = packs[0]
        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer.answer,
            payload={
                "status": "context_grounded",
                "answer": answer.model_dump(mode="json"),
                "llm_governance": governance,
                "context_ids": [pack.context_id for pack in packs],
            },
            model=str(governance.get("model") or "context-committee"),
            provider=str(governance.get("provider") or "local"),
            input_tokens=int(governance.get("usage", {}).get("input_tokens", 0)),
            output_tokens=int(governance.get("usage", {}).get("output_tokens", 0)),
            latency_ms=(time.perf_counter() - started) * 1000,
            data_as_of=primary.as_of.isoformat(),
            context_id=primary.context_id,
            context_version=primary.schema_version,
            status="degraded" if answer.requires_user_review else "ok",
            degraded_reason="context_requires_review" if answer.requires_user_review else None,
        )
        valid_refs = set(answer.evidence_refs)
        selected_blocks = [
            (pack, block)
            for pack in packs
            for block in pack.blocks
            if block.block_id in valid_refs
        ]
        if not selected_blocks:
            selected_blocks = [
                (pack, block)
                for pack in packs
                for block in pack.blocks
                if block.quality.value != "unavailable"
            ][:12]
        citations = repository.add_citations(
            assistant["message_id"],
            [
                {
                    "data_type": f"context:{block.domain.value}",
                    "source": block.source,
                    "as_of": block.as_of.isoformat(),
                    "available_at": block.available_at.isoformat(),
                    "research_run_id": (
                        resolved_research_run_id
                        if pack.symbol == bound_symbol
                        else None
                    ),
                    "symbol": pack.symbol,
                    "data_quality": block.quality.value,
                    "degraded_status": "degraded" if block.degraded else None,
                    "payload": {
                        "block_id": block.block_id,
                        "context_id": pack.context_id,
                        "context_version": pack.schema_version,
                        "fingerprint": block.fingerprint,
                    },
                }
                for pack, block in selected_blocks
            ],
        )
        return {"message": assistant, "citations": citations, "actions": []}

    flow_alert = _flow_alert_intent(content)
    if flow_alert:
        if not resolved_account:
            raise ValueError("flow alert creation requires a conversation-bound account")
        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="已生成资金流通知规则草稿，确认后才会启用。",
            payload={"status": "confirmation_required", "action_type": "notification_rule"},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        action = repository.create_action(
            conversation_id=conversation_id,
            message_id=assistant["message_id"],
            action_type="notification_rule",
            account_id=resolved_account,
            symbol=resolved_symbol or "",
            research_run_id=None,
            check_id="",
            draft_payload={
                **flow_alert,
                "symbol": resolved_symbol,
                "idempotency_key": f"chat-flow-rule:{conversation_id}:{user_message['message_id']}",
            },
            idempotency_key=f"chat-flow-rule:{conversation_id}:{user_message['message_id']}",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        return {"message": assistant, "citations": [], "actions": [action]}

    alert_intent = _alert_intent(content)
    if alert_intent:
        if not resolved_account:
            raise ValueError("alert creation requires a conversation-bound simulator account")
        if not resolved_symbol:
            raise ValueError("alert creation requires an unambiguous security symbol")
        condition_type, threshold = alert_intent
        unit = "%" if condition_type == "position_weight_above" else "元"
        shown_threshold = threshold * 100 if unit == "%" else threshold
        reply = (
            f"已生成{resolved_symbol}预警草稿：{condition_type} "
            f"{shown_threshold:g}{unit}。确认后才会创建，且预警不会触发交易。"
        )
        assistant = repository.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=reply,
            payload={"status": "confirmation_required", "action_type": "notification_alert"},
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        action = repository.create_action(
            conversation_id=conversation_id,
            message_id=assistant["message_id"],
            action_type="notification_alert",
            account_id=resolved_account,
            symbol=resolved_symbol,
            research_run_id=None,
            check_id="",
            draft_payload={
                "condition_type": condition_type,
                "threshold": threshold,
            },
            idempotency_key=f"chat-alert:{conversation_id}:{user_message['message_id']}",
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        return {"message": assistant, "citations": [], "actions": [action]}
    elif any(word in content for word in ("持仓", "账户", "现金", "盈亏", "净值")):
        result = _execute_tool(
            repository,
            registry,
            conversation_id,
            user_message["message_id"],
            "query_account",
            {"account_id": resolved_account},
        )
        reply = (
            f"账户总资产{result['equity']:.2f}元，可用现金{result['cash']:.2f}元，"
            f"持仓市值{result['market_value']:.2f}元，当前持仓{len(result['positions'])}只。"
        )
        data_as_of = None
    elif any(word in content for word in ("委托", "成交", "订单")):
        result = _execute_tool(
            repository,
            registry,
            conversation_id,
            user_message["message_id"],
            "query_orders_and_fills",
            {"account_id": resolved_account},
        )
        reply = f"当前返回{len(result['orders'])}笔委托和{len(result['fills'])}笔成交。"
        data_as_of = None
    elif "通知" in content:
        result = _execute_tool(
            repository,
            registry,
            conversation_id,
            user_message["message_id"],
            "query_notifications",
            {"account_id": resolved_account, "unread_only": True},
        )
        reply = f"当前有{len(result['notifications'])}条未读站内通知。"
        data_as_of = None
    elif any(word in content for word in ("研究", "分析", "怎么看")):
        if not resolved_symbol:
            return _guidance_reply(
                "这个问题需要关联一只明确的标的。请先打开一份研究，"
                "或在问题里带上股票/ETF 代码。"
            )
        result = _execute_tool(
            repository,
            registry,
            conversation_id,
            user_message["message_id"],
            "run_or_reuse_research" if allow_research else "query_research",
            {
                "symbol": resolved_symbol,
                "research_run_id": resolved_research_run_id,
            },
        )
        decision = result.get("payload", {}).get("decision", {})
        reply = (
            f"已找到{resolved_symbol}的研究记录，当前动作"
            f"{decision.get('action', result.get('action', 'unknown'))}。"
        )
        data_as_of = result.get("as_of")
    else:
        result = {"tools": registry.catalog()}
        reply = _answer_general_question(
            settings,
            conversation=effective_conversation,
            content=content,
        )
        data_as_of = None

    assistant = repository.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=reply,
        payload={"result": result},
        latency_ms=(time.perf_counter() - started) * 1000,
        data_as_of=data_as_of,
    )
    return {"message": assistant, "citations": [], "actions": []}


def confirm_chat_action(
    settings: Settings,
    *,
    action_id: str,
    quantity: int | None,
    quote: MarketQuote | None = None,
    simulation_mode: str | None = None,
    close_reference_acknowledged: bool = False,
) -> dict[str, Any]:
    repository = ChatRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    action = repository.action(action_id)
    if action is None:
        raise ValueError("chat action not found")
    if action["status"] == "confirmed":
        return action
    if action["status"] != "confirmation_required":
        raise ValueError("chat action is not awaiting confirmation")
    if datetime.now(UTC) > datetime.fromisoformat(action["expires_at"]):
        return repository.update_action(action_id, status="expired")
    draft = action["draft_payload"]
    if action["action_type"] == "notification_alert":
        result = _create_alert(
            TerminalRepository(
                settings.resolve(settings.get("system.database_path"))
            ),
            action["account_id"],
            {
                "symbol": action["symbol"],
                "condition_type": draft["condition_type"],
                "threshold": draft["threshold"],
            },
        )
        return repository.update_action(
            action_id,
            status="confirmed",
            result=result,
        )
    if action["action_type"] == "notification_rule":
        result = NotificationRepository(
            settings.resolve(settings.get("system.database_path"))
        ).create_rule(
            rule_type=str(draft["rule_type"]),
            idempotency_key=str(draft["idempotency_key"]),
            account_id=action["account_id"],
            symbol=draft.get("symbol"),
            threshold=draft.get("threshold"),
            consecutive_periods=int(draft.get("consecutive_periods", 2)),
            cooldown_seconds=int(draft.get("cooldown_seconds", 86_400)),
            created_source="chat",
        )
        return repository.update_action(
            action_id,
            status="confirmed",
            result=result,
        )
    if action["action_type"] != "user_paper_order":
        raise ValueError("unsupported chat action type")
    if quantity is None:
        raise ValueError("order confirmation requires quantity")
    if quantity != int(draft["quantity"]):
        raise ValueError("confirmation quantity does not match the draft")
    permitted_simulation_modes = tuple(draft.get("permitted_simulation_modes") or ())
    if simulation_mode not in permitted_simulation_modes:
        raise ValueError(
            "chat action confirmation requires one of the draft's permitted simulation modes"
        )
    if (
        simulation_mode == NEXT_OPEN_SIMULATION
        and not close_reference_acknowledged
    ):
        raise ValueError(
            "next_open_simulation requires close_reference_acknowledged=true"
        )
    # A caller-provided quote is deliberately ignored. The draft's server-owned quote
    # (only present for explicit test-only accounts) or QuoteService is authoritative.
    server_test_quote = draft.get("_server_test_quote")
    refreshed_check = run_pretrade_check(
        settings,
        account_id=action["account_id"],
        symbol=action["symbol"],
        side=draft["side"],
        quantity=quantity,
        quote=MarketQuote.model_validate(server_test_quote) if server_test_quote else None,
        research_run_id=action.get("research_run_id"),
        requested_at=datetime.now(UTC),
        user_context={"source": "chat_confirmation", "action_id": action_id},
    )
    if not refreshed_check["allowed_to_submit"]:
        raise ValueError(
            "chat action confirmation failed refreshed hard risk checks: "
            + "; ".join(refreshed_check["hard_failures"])
        )
    order = submit_user_paper_order(
        settings,
        check_id=refreshed_check["check_id"],
        quantity=quantity,
        idempotency_key=f"chat-action-confirm:{action_id}",
        requested_at=datetime.now(UTC),
        user_confirmation={
            "confirmed": True,
            "check_id": refreshed_check["check_id"],
            "account_id": refreshed_check["account_id"],
            "symbol": refreshed_check["symbol"],
            "side": refreshed_check["side"],
            "source": "chat_action_confirm",
            "action_id": action_id,
            "quantity": quantity,
            "simulation_mode": simulation_mode,
            "close_reference_acknowledged": close_reference_acknowledged,
        },
    )
    return repository.update_action(
        action_id,
        status="confirmed",
        order_id=order["order_id"],
    )


def cancel_chat_action(settings: Settings, action_id: str) -> dict[str, Any]:
    repository = ChatRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    action = repository.action(action_id)
    if action is None:
        raise ValueError("chat action not found")
    if action["status"] == "confirmed":
        raise ValueError("confirmed chat action cannot be cancelled as a draft")
    return repository.update_action(action_id, status="cancelled")


def _execute_tool(
    repository: ChatRepository,
    registry: ChatToolRegistry,
    conversation_id: str,
    message_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = registry.execute(tool_name, arguments)
        repository.record_tool_call(
            conversation_id=conversation_id,
            message_id=message_id,
            tool_name=tool_name,
            permission=registry.permission(tool_name),
            arguments=arguments,
            result=result,
            status="ok",
        )
        return result
    except Exception as exc:
        repository.record_tool_call(
            conversation_id=conversation_id,
            message_id=message_id,
            tool_name=tool_name,
            permission=registry.permission(tool_name),
            arguments=arguments,
            result=None,
            status="error",
            error_detail=safe_error_detail(exc),
        )
        raise


def _load_or_run_research(
    settings: Settings,
    symbol: str,
    run_id: str | None,
    run_if_missing: bool,
    asset_type: str | None,
) -> dict[str, Any]:
    if run_id:
        _resolved_symbol, _resolved_run_id, record = _validated_research_binding(
            settings,
            symbol=symbol,
            research_run_id=run_id,
            asset_type=asset_type,
        )
        assert record is not None
        return record
    repository = DecisionRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    record = repository.latest_for_symbol(symbol)
    if record is not None:
        validate_research_record(
            record,
            run_id=str(record["run_id"]),
            symbol=symbol,
            asset_type=asset_type,
        )
        return record
    if not run_if_missing:
        raise ValueError("saved research report is unavailable")
    output = analyze_symbol(
        settings,
        symbol,
        asset_type=asset_type,
        include_events=False,
    )
    run = output["decision_run"]
    repository.save(
        run,
        research_persistence_context(output),
        provenance=ResearchProvenance(
            origin="user_interactive_research",
            requested_as_of=(output.get("price_history") or {}).get(
                "requested_cutoff_date"
            ),
            evidence_stage="research_only",
        ),
    )
    generated = repository.get(run.run_id)
    validate_research_record(
        generated,
        run_id=run.run_id,
        symbol=symbol,
        asset_type=asset_type,
    )
    assert generated is not None
    return generated


def _reviewer_summary(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload", {})
    reports = payload.get("reports") or payload.get("agent_reports") or {}
    reviewer = reports.get("reviewer") if isinstance(reports, dict) else None
    decision = payload.get("decision") or {}
    return {
        "research_run_id": record.get("run_id"),
        "reviewer": reviewer,
        "requires_human_review": bool(decision.get("requires_human_review")),
        "action": decision.get("action"),
    }


def _require_thesis(thesis: dict[str, Any] | None) -> dict[str, Any]:
    if thesis is None:
        raise ValueError("investment thesis not found")
    return thesis


def _resolve_symbol(settings: Settings, candidate: str | None, content: str) -> str | None:
    if candidate:
        return normalize_stock_symbol(candidate)
    code = re.search(r"(?<!\d)(?:sh|sz)?([036]\d{5})(?!\d)", content, re.I)
    if code:
        return normalize_stock_symbol(code.group(0))
    try:
        results = search_stocks(settings, content, 3).get("results", [])
        if len(results) == 1:
            return str(results[0]["symbol"])
        exact = [
            item
            for item in results
            if str(item.get("name") or "") and str(item.get("name")) in content
        ]
        return str(exact[0]["symbol"]) if len(exact) == 1 else None
    except Exception:
        return None


def _trade_intent(content: str) -> str | None:
    if any(word in content for word in ("为什么", "是否", "建议", "怎么看", "分析", "理由")) and not (
        any(word in content for word in ("帮我", "模拟买", "模拟卖"))
        or re.search(r"\d+\s*股", content)
    ):
        return None
    if any(word in content for word in ("买入", "加仓", "模拟买", "建仓")):
        return "buy"
    if any(word in content for word in ("卖出", "减仓", "清仓", "模拟卖")):
        return "sell"
    return None


def _quantity_from_text(content: str) -> int | None:
    match = re.search(r"(\d{1,9})\s*股", content)
    return int(match.group(1)) if match else None


def _symbols_from_text(content: str) -> list[str]:
    matches = re.findall(r"(?<!\w)(?:sh|sz)?[036]\d{5}(?!\d)", content, re.I)
    output: list[str] = []
    for match in matches:
        try:
            symbol = normalize_stock_symbol(match)
        except Exception:
            continue
        if symbol not in output:
            output.append(symbol)
    return output


def _is_context_question(content: str) -> bool:
    return any(
        word in content
        for word in (
            "为什么",
            "资金",
            "财务",
            "估值",
            "事件",
            "公告",
            "监管",
            "宏观",
            "证据",
            "缺口",
            "结论",
            "观点",
            "比较",
            "对比",
            "失效",
            "风险",
            "建议",
            "怎么看",
            "分析",
        )
    )


_GENERAL_CAPABILITY_REPLY = (
    "我可以查询模拟账户、持仓、委托、成交、盈亏、行情、研究、Reviewer、"
    "组合约束和通知；也可以运行交易前检查并创建需要二次确认的模拟订单草稿。"
)


class _ChatGeneralReply(BaseModel):
    reply: str = ""


def _answer_general_question(
    settings: Settings,
    *,
    conversation: dict[str, Any],
    content: str,
) -> str:
    """Answer non-keyword questions with the configured LLM.

    The deterministic keyword router owns account/order/alert flows.  Anything
    it does not recognise (greetings, "what should I do now", general market
    chat) used to fall through to a fixed capability card for every message,
    which reads as broken even when a real API key is configured.  Give those
    messages to the LLM instead, and degrade to the capability card when the
    provider is mock, unconfigured, or the call fails (fail-closed).
    """

    if str(settings.get("llm.provider", "mock")).strip().lower() == "mock":
        return _GENERAL_CAPABILITY_REPLY
    provider = build_provider(settings.section("llm"))
    system = (
        "你是 QuantLab 的 AI 投研助手，用简洁、直接的中文回答。"
        "你可以查询模拟账户、持仓、委托、成交、盈亏、行情与研究，"
        "也能运行交易前检查、创建需要用户二次确认的模拟订单草稿。"
        "规则：不承诺收益、不编造实时行情或不存在的数据；"
        "用户问买卖相关的问题时，提醒订单草稿需要本人二次确认；"
        "答不上来就直接说，并引导用户问与研究、持仓、风险相关的问题。"
        "回答控制在 120 字以内。"
    )
    context_bits: list[str] = []
    if conversation.get("page_scope"):
        context_bits.append(f"用户当前所在页面：{conversation['page_scope']}")
    if conversation.get("symbol"):
        context_bits.append(f"当前关联标的：{conversation['symbol']}")
    prompt = "\n".join([*context_bits, f"用户的问题：{content}"])

    async def call() -> _ChatGeneralReply:
        return await provider.structured(system, prompt, _ChatGeneralReply)

    try:
        answer = asyncio.run(await_with_provider_close(provider, call()))
    except Exception:
        return _GENERAL_CAPABILITY_REPLY
    reply = str(getattr(answer, "reply", "") or "").strip()
    return reply or _GENERAL_CAPABILITY_REPLY


def _answer_context_question(
    settings: Settings,
    *,
    packs: list[AnalysisContextPack],
    question: str,
    task_id: str,
) -> tuple[ChatEvidenceAnswer, dict[str, Any]]:
    repository = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    )
    context_fingerprint = "|".join(pack.fingerprint for pack in packs)
    base = build_provider(settings.section("llm"))
    provider = GovernedLLMProvider(
        base,
        repository,
        context_id=packs[0].context_id,
        context_fingerprint=context_fingerprint,
        task_id=task_id,
        budget=budget_from_settings(settings.section("llm")),
    )
    maximum_weight = float(settings.get("risk.max_single_position", 0.15))
    system = (
        "You are the chat member of a bounded investment research committee. Answer only from supplied "
        "AnalysisContextPacks. Cite block_id values. Clearly separate deterministic facts, quantitative "
        "results, LLM judgments and user assumptions. Never invent missing current facts or use evidence "
        "available after cutoff. Estimated capital flow is not confirmed institutional ownership. Any weight "
        "range must stay below the deterministic maximum, and the user retains final control."
    )
    prompt = json.dumps(
        {
            "question": question,
            "deterministic_maximum_weight": maximum_weight,
            "contexts": [
                pack.llm_payload(
                    max(8_000, int(settings.get("llm.context_maximum_bytes", 48_000)) // len(packs))
                )
                for pack in packs
            ],
        },
        ensure_ascii=False,
    )

    async def call():
        try:
            return await provider.structured(system, prompt, ChatEvidenceAnswer)
        except Exception as exc:
            if type(exc).__name__ == "LLMBudgetExceeded":
                NotificationRepository(repository.path).emit(
                    event_type="llm_budget_reached",
                    aggregate_type="chat_task",
                    aggregate_id=task_id,
                    payload={
                        "content": "Chat已达到本任务LLM预算，返回保守降级结果",
                        "data_as_of": packs[0].as_of.isoformat(),
                        "action_payload": {"task_id": task_id, "context_id": packs[0].context_id},
                    },
                    dedup_key=f"llm_budget:{task_id}",
                )
            return ChatEvidenceAnswer(
                answer="LLM综合判断不可用；以下仅保留ContextPack中的确定性事实，建议人工复核。",
                facts=_deterministic_context_facts(packs),
                missing_data=[f"llm_unavailable:{type(exc).__name__}"],
                suggested_action="review_required",
                requires_user_review=True,
            )

    answer = asyncio.run(await_with_provider_close(provider, call()))
    valid_blocks = {block.block_id: block for pack in packs for block in pack.blocks}
    invalid_refs = [ref for ref in answer.evidence_refs if ref not in valid_blocks]
    answer.evidence_refs = [ref for ref in answer.evidence_refs if ref in valid_blocks]
    if invalid_refs:
        answer.missing_data.append("invalid_evidence_references_removed")
        answer.requires_user_review = True
    if not answer.facts:
        answer.facts = _deterministic_context_facts(packs)
    answer.suggested_weight_max = min(answer.suggested_weight_max, maximum_weight)
    answer.suggested_weight_min = min(
        answer.suggested_weight_min,
        answer.suggested_weight_max,
    )
    referenced_domains = {
        valid_blocks[ref].domain for ref in answer.evidence_refs if ref in valid_blocks
    }
    if answer.suggested_action in {"buy", "add"} and referenced_domains <= {
        EvidenceDomain.CAPITAL_FLOW
    }:
        answer.suggested_action = "review_required"
        answer.suggested_weight_min = 0.0
        answer.suggested_weight_max = 0.0
        answer.missing_data.append("capital_flow_alone_cannot_generate_buy")
        answer.requires_user_review = True
    if any(pack.review_required for pack in packs):
        answer.requires_user_review = True
        if answer.suggested_action in {"buy", "add"}:
            answer.suggested_action = "review_required"
            answer.suggested_weight_min = 0.0
            answer.suggested_weight_max = 0.0
    return answer, provider.health_snapshot()


def _deterministic_context_facts(packs: list[AnalysisContextPack]) -> list[str]:
    facts: list[str] = []
    for pack in packs:
        market = pack.block(EvidenceDomain.MARKET)
        if market and market.quality.value != "unavailable":
            price = market.payload.get("current_raw_price")
            facts.append(
                f"{pack.symbol} raw_price={price} as_of={market.as_of.isoformat()} "
                f"source={market.source} quality={market.quality.value}"
            )
        facts.append(
            f"{pack.symbol} context_quality={pack.quality_score:.2f} "
            f"critical_gaps={len(pack.critical_gaps)}"
        )
    return facts[:20]


def _require_context(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        raise ValueError("analysis context pack is unavailable")
    return value


def _context_domain(context: dict[str, Any], domain: str) -> dict[str, Any]:
    blocks = [item for item in context.get("blocks", []) if item.get("domain") == domain]
    return {
        "context_id": context.get("context_id"),
        "context_version": context.get("schema_version"),
        "blocks": blocks,
        "status": "available" if blocks else "unavailable",
    }


def _frozen_conversation_context(
    settings: Settings,
    conversation: dict[str, Any],
    symbol: str,
) -> dict[str, Any] | None:
    run_id = conversation.get("research_run_id")
    if not run_id:
        return None
    record = DecisionRepository(
        settings.resolve(settings.get("system.database_path"))
    ).get(str(run_id))
    identity = validate_research_record(
        record,
        run_id=str(run_id),
        symbol=symbol,
    )
    assert record is not None
    decision = record.get("payload", {}).get("decision", {})
    context_id = decision.get("context_id")
    if not context_id:
        context_id = (
            record.get("payload", {})
            .get("research_context", {})
            .get("analysis_context_pack", {})
            .get("context_id")
        )
    if not context_id:
        raise ValueError("bound research context pack is unavailable")
    context = EvidenceRepository(
        settings.resolve(settings.get("system.database_path"))
    ).context(str(context_id))
    if context is None:
        raise ValueError("bound research context pack is unavailable")
    if str(context.get("symbol") or "") != symbol:
        raise ValueError("bound research context symbol does not match the research run")
    if identity.get("as_of") and str(context.get("as_of") or "")[:10] != identity[
        "as_of"
    ].isoformat():
        raise ValueError("bound research context as_of does not match the research run")
    if identity.get("asset_type") and str(context.get("asset_type") or "").lower() != identity[
        "asset_type"
    ]:
        raise ValueError("bound research context asset type does not match the research run")
    expected_fingerprint = identity.get("context_fingerprint")
    if expected_fingerprint and context.get("fingerprint") != expected_fingerprint:
        raise ValueError("bound research context fingerprint does not match the research run")
    return context


def _alert_intent(content: str) -> tuple[str, float] | None:
    if not any(word in content for word in ("提醒", "预警", "通知我")):
        return None
    if "仓位" in content:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", content)
        if not match:
            raise ValueError("position alert requires a percentage threshold")
        threshold = float(match.group(1)) / 100
        if not 0 < threshold <= 1:
            raise ValueError("position alert percentage must be between 0 and 100")
        return "position_weight_above", threshold
    match = re.search(
        r"(?:涨到|跌到|高于|低于|超过|跌破|上穿|下穿|价格达到)\s*"
        r"(\d+(?:\.\d+)?)",
        content,
    )
    if not match:
        raise ValueError("price alert requires a numeric threshold")
    threshold = float(match.group(1))
    if threshold <= 0:
        raise ValueError("price alert threshold must be positive")
    below = any(word in content for word in ("跌到", "低于", "跌破", "下穿"))
    return ("price_below" if below else "price_above"), threshold


def _is_alert_request(content: str) -> bool:
    return any(word in content for word in ("提醒", "预警", "通知我"))


def _flow_alert_intent(content: str) -> dict[str, Any] | None:
    if "资金" not in content or not _is_alert_request(content):
        return None
    if "背离" in content:
        return {
            "rule_type": "flow_price_divergence",
            "threshold": None,
            "consecutive_periods": 1,
            "cooldown_seconds": 86_400,
        }
    periods_match = re.search(r"连续\s*(\d{1,2})\s*(?:日|天|期)", content)
    periods = int(periods_match.group(1)) if periods_match else 2
    if "流入" in content:
        rule_type = "flow_positive_streak"
    elif "流出" in content:
        rule_type = "flow_negative_streak"
    elif any(word in content for word in ("放量", "成交额")):
        return {
            "rule_type": "market_turnover_ratio_above",
            "threshold": 1.2,
            "consecutive_periods": 1,
            "cooldown_seconds": 86_400,
        }
    else:
        return None
    threshold = 0.0
    threshold_match = re.search(r"(?:超过|大于)\s*(\d+(?:\.\d+)?)\s*(亿|万)?", content)
    if threshold_match:
        threshold = float(threshold_match.group(1))
        if threshold_match.group(2) == "亿":
            threshold *= 100_000_000
        elif threshold_match.group(2) == "万":
            threshold *= 10_000
    return {
        "rule_type": rule_type,
        "threshold": threshold,
        "consecutive_periods": max(2, min(periods, 20)),
        "cooldown_seconds": 86_400,
    }


def _create_alert(
    terminal: TerminalRepository,
    account_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    condition_type = str(arguments["condition_type"])
    if condition_type not in {"price_above", "price_below", "position_weight_above"}:
        raise ValueError("unsupported chat alert condition")
    threshold = float(arguments["threshold"])
    return {
        "alert_id": terminal.add_alert(
            str(arguments["symbol"]),
            condition_type,
            threshold,
            account_id=account_id,
        ),
        "condition_type": condition_type,
        "threshold": threshold,
    }


def _is_forbidden_request(content: str) -> bool:
    normalized = content.lower()
    forbidden = (
        "api key",
        "apikey",
        "系统prompt",
        "系统提示词",
        "读取token",
        "泄露token",
        "修改历史成交",
        "改写历史成交",
        "绕过确认",
        "自动确认订单",
        "直接改现金",
        "删除账户证据",
    )
    return any(item in normalized for item in forbidden)


__all__ = [
    "ChatToolRegistry",
    "cancel_chat_action",
    "confirm_chat_action",
    "create_chat_conversation",
    "handle_chat_message",
]
