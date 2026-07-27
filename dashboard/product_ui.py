from __future__ import annotations

import uuid
from datetime import date, datetime
from html import escape
from time import monotonic
from typing import Any
import math
import re
import pandas as pd
import streamlit as st

from dashboard.local_settings import (
    LLM_PROVIDER_OPTIONS,
    llm_provider_key_configured,
    masked_secret_status,
    remove_local_llm_key,
    save_llm_product_preferences,
)
from dashboard.ui_foundation import (
    PRODUCT_PAGES,
    PRODUCT_ATTENTION_COUNT_KEY,
    apply_product_theme,
    bind_product_research_context,
    cache_research_report,
    cached_research_report,
    clear_product_research_context,
    consume_product_context,
    context_matches_research,
    mark_research_failed,
    mark_research_loading,
    product_context,
    render_page_state,
    render_product_navigation,
    research_identity,
    set_product_page,
    update_product_selection,
)
from quantlab.config import Settings
from quantlab.domain import MarketQuote, ResearchProvenance
from quantlab.execution import (
    INTRADAY_SIMULATION,
    NEXT_OPEN_SIMULATION,
    available_user_paper_simulation_modes,
)
from quantlab.persistence import (
    ChatRepository,
    DecisionRepository,
    EvidenceRepository,
    NotificationRepository,
    RoundtableRepository,
)
from quantlab.persistence.jobs import JobRepository
from quantlab.persistence.round5 import Round5Repository
from quantlab.persistence.round8 import Round8Repository
from quantlab.persistence.round9 import Round9Repository
from quantlab.persistence.notifications import (
    EVENT_PRESENTATION,
    MANDATORY_NOTIFICATION_TYPES,
)
from quantlab.runtime.notification_delivery import NotificationDeliveryWorker
from quantlab.runtime.readiness import formal_experiment_status, primary_start_readiness
from quantlab.runtime.soak import soak_report
from quantlab.reporting import (
    build_stored_audit_package,
    research_persistence_context,
)
from quantlab.workflows import (
    analyze_symbol,
    build_market_radar,
    build_product_home,
    cancel_chat_action,
    cancel_user_paper_order,
    confirm_chat_action,
    create_chat_conversation,
    create_user_paper_account,
    mark_user_paper_account,
    recommend_stocks,
    record_product_usage,
    roundtable_participant_catalog,
    run_pretrade_check,
    search_stocks,
    settle_user_paper_order,
    submit_user_paper_order,
    user_simulator_repository,
)
from quantlab.workflows.chat_jobs import submit_chat_job
from quantlab.workflows.roundtable_jobs import submit_roundtable_job
from quantlab.workflows.investor_portfolio import (
    build_investor_recommendation,
    confirm_investor_import,
    create_investor_portfolio,
    investor_recommendation_detail,
    investor_recommendation_effects,
    mark_investor_portfolios,
    preview_investor_csv,
    record_recommendation_adoption,
)
from quantlab.workflows.research_identity import validate_research_record
from quantlab.workflows.product_demo import (
    confirm_historical_research_demo,
    live_demo_status,
    prepare_historical_research_demo,
    reset_historical_research_demo,
    run_historical_research_demo,
)
from quantlab.workflows.decision_tasks import refresh_decision_tasks


PRIMARY_ENTRYPOINTS = list(PRODUCT_PAGES)

PAGE_DESCRIPTIONS = {
    "今日": ("DAILY DECISION DESK", "先看变化与待处理事项，再决定是否需要行动。"),
    "市场与发现": ("MARKET CONTEXT", "从市场状态、资金与候选线索进入可验证的标的研究。"),
    "研究台": (
        "EVIDENCE TRACE",
        "冻结证据、阅读多空分歧、继续追问，并把完整研究身份带到交易前检查。",
    ),
    "组合与交易": (
        "USER SIMULATION",
        "查看持仓与盈亏，运行后端交易前检查，并由你最终确认模拟订单。",
    ),
    "决策复盘": ("DECISION MEMORY", "把订单、研究、论文与结果重新连起来，检查当时的判断是否兑现。"),
    "专业空间": ("PROFESSIONAL LAYER", "管理只读外部账本、任务、通知、运行状态和正式证据隔离。"),
    "帮助中心": ("PRODUCT HANDBOOK", "快速开始、报告阅读、模拟交易规则、常见问题与能力边界。"),
    "研究详情": ("FROZEN RESEARCH", "单独阅读一份冻结报告，查看证据、反证与下一步。"),
    "专家圆桌": ("EXPERT ROUNDTABLE", "围绕同一份冻结研究展开可恢复、可追溯的多视角讨论。"),
    "设置": ("PERSONAL SETTINGS", "管理本机的 AI 服务偏好和通知接收信息。"),
}

LLM_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "openai": "OpenAI Responses",
    "openai_compatible": "OpenAI 兼容接口",
}

LLM_PROVIDER_HELP = {
    "deepseek": "使用 DeepSeek 的 Chat Completions 兼容接口；请求地址留空时使用官方地址。",
    "openai": "使用 OpenAI Responses API；可填写支持 Responses API 的自定义请求地址。",
    "openai_compatible": "用于兼容 Chat Completions 的服务；必须填写模型、请求地址和对应 API Key。",
}

BLOCKER_LABELS = {
    "formal_signal_date_must_equal_server_market_date": "信号日期不是当前上海市场日期",
    "current_quality_gate_has_not_passed": "当前代码尚未通过最新质量门禁",
    "trusted_production_calendar_does_not_cover_signal_date": "正式交易日历尚未覆盖今天",
    "trusted_production_calendar_does_not_cover_20_session_horizon": "交易日历尚未覆盖未来20个交易日",
    "trusted_production_point_in_time_pool_for_signal_date_unavailable": "今天的正式股票池尚未生成",
    "point_in_time_pool_has_fewer_than_frozen_candidate_count": "可研究候选数量不足",
    "trusted_production_industry_membership_not_ready": "行业归属数据尚未准备好",
    "point_in_time_pool_field_coverage_below_minimum": "股票池关键字段覆盖不足",
    "trading_calendar_coverage_below_minimum": "交易日历覆盖不足",
    "trusted_production_security_master_not_ready": "A股证券主数据尚未准备好",
    "formal_llm_provider_is_not_explicitly_configured": "正式LLM服务尚未配置",
    "worker_process_is_not_healthy": "后台任务服务未正常运行",
    "scheduler_process_is_not_healthy": "每日调度服务未正常运行",
}

STATUS_LABELS = {
    "completed": "正常",
    "available": "正常",
    "partial": "部分可用",
    "degraded": "降级",
    "unavailable": "不可用",
    "failed": "失败",
    "queued": "排队中",
    "running": "正在运行",
    "skipped_non_trading_day": "休市日无需生成",
    "pending": "待处理",
    "submitted": "已提交",
    "partially_filled": "部分成交",
    "filled": "已成交",
    "cancelled": "已撤单",
    "rejected": "已拒绝",
    "expired": "已过期",
}
SEVERITY_LABELS = {
    "info": "更新",
    "warning": "请复核",
    "critical": "重要",
}
DATA_TRUST_LEVEL_LABELS = {
    "test": "测试数据",
    "user_imported": "用户导入",
    "research_external": "研究参考",
    "server_observed": "服务器核验",
    "trusted_licensed": "已授权数据",
    "exchange_or_broker_confirmed": "交易所或券商确认",
}
ADOPTION_DECISION_LABELS = {
    "adopted": "采纳",
    "partially_adopted": "部分采纳",
    "rejected": "拒绝",
    "user_override": "逆建议交易",
}
RESEARCH_ACTION_LABELS = {
    "buy": "倾向买入",
    "add": "倾向加仓",
    "hold": "继续持有",
    "watch": "持续观察",
    "observe": "持续观察",
    "avoid": "暂不参与",
    "sell": "倾向卖出",
    "reduce": "倾向减仓",
    "review": "等待复核",
    "blocked": "暂不操作",
    "unavailable": "研究暂不可用",
}
RESEARCH_STAGE_LABELS = {
    "research_only": "探索研究，不计正式成绩",
    "forward_shadow": "前瞻影子观察",
    "production": "正式生产研究",
    "historical_replay": "历史回放，仅供研究",
    "point_in_time_training": "点时训练样本",
    "candidate_tournament": "候选比较研究",
    "test": "测试数据，不可用于决策",
    "unavailable": "证据阶段待确认",
}
THESIS_STATUS_LABELS = {
    "active": "论文生效",
    "strengthened": "判断增强",
    "unchanged": "判断未变",
    "weakened": "判断减弱，需复核",
    "damaged": "出现损伤，优先复核",
    "broken": "论文失效",
    "draft_pending_confirmation": "论文草稿待确认",
    "needs_review": "需要复核",
}
RESEARCH_JOB_LABELS = {
    "research": "研究任务",
    "chat_request": "研究追问",
    "context_pack": "研究证据整理",
    "context_committee": "研究委员会",
    "analysis": "分析任务",
}
QUOTE_KIND_LABELS = {
    "realtime": "实时行情",
    "delayed": "延迟行情",
    "current_close": "最近收盘价",
    "previous_close": "上一交易日收盘价",
    "unavailable": "行情不可用",
}
PRETRADE_REASON_LABELS = {
    "daily_bar_is_not_a_realtime_execution_quote": "当前价格来自日线收盘，不是实时成交报价",
    "quote_kind_current_close": "当前使用最近收盘价作为参考",
    "quote_kind_current_close_is_not_intraday_actionable": "最近收盘价不能代表盘中即时成交",
    "stored_last_mark_is_not_current_execution_data": "仅有上次盯市价格，不能作为当前成交依据",
    "suspended_or_missing": "标的停牌或没有可用行情",
    "market_data_missing": "缺少可用行情数据",
    "market_data_stale": "行情已经过期",
    "session_status_unknown": "无法确认当前交易时段",
    "limit_up": "涨停状态下不能模拟买入",
    "limit_down": "跌停状态下不能模拟卖出",
    "invalid_trade_lot": "买入数量需要符合整手规则",
    "t_plus_one_or_insufficient_position": "可卖数量不足或受 T+1 限制",
    "insufficient_cash": "可用现金不足",
    "maximum_total_exposure_exceeded": "操作后总仓位超过上限",
    "maximum_single_weight_exceeded": "操作后单一标的仓位超过上限",
    "maximum_industry_weight_exceeded": "操作后行业仓位超过上限",
}

NOTIFICATION_ACTION_LABELS = {
    "confirm_chat_action": "打开确认草稿",
    "query_job": "查看任务状态",
    "view_capital_flow": "查看资金线索",
    "view_context_pack": "查看研究上下文",
    "view_investment_thesis": "查看投资论文",
    "view_llm_audit": "查看运行说明",
    "view_order": "打开模拟订单",
    "view_position": "打开模拟持仓",
    "view_positions": "打开模拟持仓",
    "view_research": "打开研究报告",
    "view_role_scorecard": "查看运行说明",
    "view_structured_summary": "查看摘要",
}
NOTIFICATION_TYPE_LABELS = {
    event_type: title for event_type, (_, title, _) in EVENT_PRESENTATION.items()
}
NOTIFICATION_USER_COPY = {
    "background_job_completed": (
        "后台更新已完成",
        "一项后台处理已按计划完成；相关页面会在下次打开时读取最新状态。",
    ),
    "background_job_failed": (
        "后台更新未完成",
        "一项后台处理没有完成。系统不会使用不完整的数据或结果；可稍后重试，或到「专业空间 > 系统状态」查看详情。",
    ),
    "data_source_degraded": (
        "部分数据暂不可用",
        "部分数据源正在降级运行。页面会保留数据缺口，不会用猜测补齐。",
    ),
    "forward_registration_failed": (
        "正式样本未登记",
        "今天的正式实验没有创建新样本。系统保持空白，不会用恢复运行、历史记录或模拟数据补写。",
    ),
    "provider_fallback": (
        "AI 服务已切换备用通道",
        "本次 AI 服务使用备用通道完成；请以研究报告中的数据状态和证据为准。",
    ),
    "ai_view_changed": (
        "AI 研究结论发生变化",
        "系统检测到研究结论变化。请打开对应报告，重新查看支持证据、反对证据和失效条件。",
    ),
    "roundtable_request_completed": (
        "专家圆桌已完成",
        "一场专家圆桌已保存。可回到对应研究报告，查看逐轮发言与仍未解决的分歧。",
    ),
}
RESEARCH_CONDITION_LABELS = {
    "Price closes below MA_20 (55.20 adjusted) or 20-day return turns negative": (
        "价格收盘跌破 20 日均线（55.20，复权），或近 20 日收益转负"
    ),
    "Actual institutional capital flow data shows net inflows": "后续机构资金数据显示净流入",
    "Company reports dramatically improved profitability or ROE": "公司披露显著改善的盈利能力或净资产收益率",
    "Positive earnings surprise or operational update reverses negative sentiment": (
        "业绩超预期或经营进展扭转当前负面情绪"
    ),
    "If normalized earnings or book value increase significantly, fair value estimate may rise.": (
        "若经标准化后的盈利或账面价值显著提升，合理价值估计需要上调复核。"
    ),
    "If actual institutional flow data contradicts estimated proxy, capital flow view invalidated.": (
        "若后续可核验的机构资金数据与当前估算方向相反，资金流判断需要重新评估。"
    ),
}
PRODUCT_FEEDBACK_KEY = "product_feedback"
HISTORICAL_DEMO_OPEN_KEY = "historical_demo_workspace_open"
HISTORICAL_DEMO_PREPARED_KEY = "historical_demo_prepared"
HISTORICAL_DEMO_RESULT_KEY = "historical_demo_result"
_UI_READINESS_CACHE_KEY = "product_ui_readiness_cache"
_UI_READINESS_CACHE_SECONDS = 12.0


def render_product_app(settings: Settings) -> None:
    if "product_loaded_event" not in st.session_state:
        record_product_usage(settings, event_type="product_opened", entrypoint="今日")
        st.session_state["product_loaded_event"] = True
    apply_product_theme()
    st.session_state[PRODUCT_ATTENTION_COUNT_KEY] = _notification_attention_count(settings)
    page = render_product_navigation()
    _render_workspace_header(page)
    _render_product_feedback()
    renderers = {
        "今日": render_home,
        "市场与发现": render_market_and_discovery,
        "研究台": render_ai_research,
        "组合与交易": render_simulator,
        "决策复盘": render_review,
        "专业空间": render_mine,
        "帮助中心": render_help_center,
        "研究详情": render_research_detail,
        "专家圆桌": render_roundtable,
        "设置": render_settings,
    }
    renderers[page](settings)
    _render_global_ai_assistant(settings, page=page)


def _render_workspace_header(page: str) -> None:
    eyebrow, description = PAGE_DESCRIPTIONS[page]
    title = page
    if page == "今日" and st.session_state.get(HISTORICAL_DEMO_OPEN_KEY):
        title = "完整决策示例"
        eyebrow = "ISOLATED DECISION WALKTHROUGH"
        description = "用冻结历史证据走完研究、交易前检查、用户确认、模拟成交与复盘。"
    st.markdown(
        f"""
        <section class="ql-workspace-head">
          <div>
            <p class="ql-eyebrow">{eyebrow}</p>
            <h1>{title}</h1>
            <p>{description}</p>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _go_to(
    page: str,
    *,
    symbol: str | None = None,
    research_run_id: str | None = None,
    research_requested_as_of: str | None = None,
    research_effective_as_of: str | None = None,
    account_id: str | None = None,
    order_id: str | None = None,
) -> None:
    set_product_page(
        st.session_state,
        page,
        symbol=symbol,
        research_run_id=research_run_id,
        research_requested_as_of=research_requested_as_of,
        research_effective_as_of=research_effective_as_of,
        account_id=account_id,
        order_id=order_id,
    )
    st.rerun()


def _queue_product_feedback(message: str) -> None:
    st.session_state[PRODUCT_FEEDBACK_KEY] = message


def _render_product_feedback() -> None:
    message = st.session_state.pop(PRODUCT_FEEDBACK_KEY, None)
    if message:
        st.toast(str(message))


def _research_loading_indicator() -> Any:
    """Show progress feedback before a synchronous research run yields a result."""

    placeholder = st.empty()
    placeholder.markdown(
        """
        <section class="ql-research-loading" role="status" aria-live="polite">
          <span class="ql-research-loading-mark" aria-hidden="true"><i></i><b></b><em></em></span>
          <div><strong>正在建立研究上下文</strong>
          <p>冻结数据、整理支持与反对证据，并准备可继续追问的研究报告。</p></div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    return placeholder


def _notification_display(item: dict[str, Any]) -> tuple[str, str]:
    notification_type = str(item.get("notification_type") or "")
    override = NOTIFICATION_USER_COPY.get(notification_type)
    if override:
        return override
    raw_title = str(item.get("title") or "QuantLab 通知")
    raw_content = str(item.get("content") or "系统状态已更新。")
    raw_detail = f"{raw_title} {raw_content}".lower()
    if notification_type.endswith("_failed") or any(
        token in raw_detail for token in ("traceback", "exception", "error:", " failed:")
    ):
        return (
            "系统更新未完成",
            "一次系统处理没有完成。系统不会使用不完整的数据或结果；可到「专业空间 > 系统状态」查看详情。",
        )
    return (
        raw_title,
        raw_content,
    )


def _notification_attention_count(settings: Settings) -> int:
    try:
        repository = NotificationRepository(settings.resolve(settings.get("system.database_path")))
        return repository.unread_attention_count()
    except Exception:
        return 0


def _ui_readiness_snapshot(
    settings: Settings,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return a short-lived readiness snapshot for product-page display.

    The detailed readiness check validates source freshness, PIT evidence, and
    quality-gate provenance.  It is useful on a status page, but repeating it
    on every Streamlit widget rerun made ordinary navigation feel sluggish.
    This cache is confined to one browser session, expires quickly, and is
    invalidated whenever the active database changes.  It is never used by
    scheduler, worker, experiment registration, or order admission.
    """

    path = settings.resolve(settings.get("system.database_path"))
    try:
        stat = path.stat()
        database_signature: tuple[Any, ...] = (
            str(path.resolve()).casefold(),
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError:
        database_signature = (str(path).casefold(), None, None)
    settings_signature = (
        str(settings.get("strategies.forward_primary.minimum_trust_level", "")),
        int(settings.get("strategies.forward_primary.candidate_count", 3) or 3),
        str(settings.get("llm.provider", "")),
    )
    signature = (database_signature, settings_signature)
    now = monotonic()
    cached = st.session_state.get(_UI_READINESS_CACHE_KEY)
    if (
        not force
        and isinstance(cached, dict)
        and cached.get("signature") == signature
        and now - float(cached.get("captured_at") or 0) < _UI_READINESS_CACHE_SECONDS
        and isinstance(cached.get("value"), dict)
    ):
        return cached["value"]

    readiness = primary_start_readiness(settings, require_runtime=False)
    st.session_state[_UI_READINESS_CACHE_KEY] = {
        "signature": signature,
        "captured_at": now,
        "value": readiness,
    }
    return readiness


def _notification_target(settings: Settings, item: dict[str, Any]) -> dict[str, Any]:
    """Resolve a notification into a safe user-facing destination.

    The notification payload is only navigation context.  It never confirms an
    order, re-runs research, or bypasses research identity validation.
    """

    payload = item.get("action_payload")
    payload = payload if isinstance(payload, dict) else {}
    action_type = str(item.get("action_type") or "")
    symbol = str(payload.get("symbol") or item.get("symbol") or "").strip() or None
    account_id = str(payload.get("account_id") or item.get("account_id") or "").strip() or None
    order_id = str(payload.get("order_id") or item.get("order_id") or "").strip() or None
    label = NOTIFICATION_ACTION_LABELS.get(action_type, "查看通知详情")

    if action_type in {"view_order", "view_position", "view_positions"}:
        return {
            "page": "组合与交易",
            "context": {
                key: value
                for key, value in {
                    "symbol": symbol,
                    "account_id": account_id,
                    "order_id": order_id,
                }.items()
                if value is not None
            },
            "label": label,
            "message": "已打开关联的模拟账户；任何订单仍需你单独确认。",
        }

    if action_type == "view_research":
        run_id = str(payload.get("run_id") or item.get("research_run_id") or "").strip()
        if run_id:
            try:
                record = DecisionRepository(
                    settings.resolve(settings.get("system.database_path"))
                ).get(run_id)
                identity = validate_research_record(
                    record,
                    run_id=run_id,
                    symbol=symbol,
                )
                return {
                    "page": "研究台",
                    "context": {
                        "symbol": identity["symbol"],
                        "research_run_id": identity["run_id"],
                        "research_requested_as_of": identity["requested_as_of"].isoformat(),
                        "research_effective_as_of": identity["effective_as_of"].isoformat(),
                    },
                    "label": label,
                    "message": "已打开关联研究报告，并保留原始研究身份。",
                }
            except Exception:
                pass
        return {
            "page": "研究台",
            "context": {"symbol": symbol} if symbol else {},
            "label": label,
            "message": "关联报告无法验证，已只带入标的，不会加载或复用旧报告。",
        }

    if action_type in {"confirm_chat_action", "view_context_pack"}:
        return {
            "page": "研究台",
            "context": {"symbol": symbol} if symbol else {},
            "label": label,
            "message": "已带入相关标的；AI 对话草稿和研究结论仍需你逐项确认。",
        }

    if action_type == "view_capital_flow":
        return {
            "page": "市场与发现",
            "context": {"symbol": symbol} if symbol else {},
            "label": label,
            "message": "已带入资金提醒的标的；资金流只是一条研究线索。",
        }

    if action_type == "view_investment_thesis":
        return {
            "page": "决策复盘",
            "context": {"symbol": symbol} if symbol else {},
            "label": label,
            "message": "已打开决策复盘；请结合论文失效条件复核。",
        }

    return {
        "page": "专业空间",
        "context": {},
        "label": label,
        "message": "已打开通知详情和关联状态。",
    }


def _open_notification(settings: Settings, item: dict[str, Any]) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    try:
        NotificationRepository(path).mark_read(str(item["notification_id"]))
    except Exception as exc:
        st.error(_friendly_error(exc, "通知状态暂时无法更新；没有跳转到关联内容。"))
        return

    target = _notification_target(settings, item)
    if target["page"] == "专业空间":
        st.session_state["product_mine_view_target"] = "提醒与任务"
        st.session_state["product_mine_attention_view_target"] = "通知中心"
        notification_title, notification_content = _notification_display(item)
        st.session_state["product_notification_target"] = {
            "title": notification_title,
            "content": notification_content,
            "action_type": item.get("action_type") or "unavailable",
            "data_as_of": item.get("data_as_of"),
            "created_at": item.get("created_at"),
        }
    _queue_product_feedback(target["message"])
    _go_to(target["page"], **target["context"])


def _mark_visible_notifications_read(
    notifications: Any,
    items: list[dict[str, Any]],
) -> int:
    """Mark only rows the active notification filters actually returned."""

    return sum(
        int(not item["read"] and notifications.mark_read(item["notification_id"])) for item in items
    )


def _date_value(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        return fallback


def _pretrade_request_matches(
    check: dict[str, Any],
    *,
    account_id: str,
    symbol: str,
    side: str,
    quantity: int,
    research_run_id: str | None,
    research_link_status: str,
) -> bool:
    return bool(
        check.get("account_id") == account_id
        and check.get("symbol") == symbol
        and check.get("side") == side
        and int(check.get("requested_quantity") or 0) == quantity
        and (check.get("research_run_id") or None) == research_run_id
        and (check.get("research_link_status") or "unlinked") == research_link_status
    )


def _chat_conversations_for_context(
    conversations: list[dict[str, Any]],
    *,
    symbol: str | None,
    research_run_id: str | None,
) -> list[dict[str, Any]]:
    if research_run_id:
        return [
            item
            for item in conversations
            if item.get("symbol") == symbol and item.get("research_run_id") == research_run_id
        ]
    if symbol:
        return [
            item
            for item in conversations
            if item.get("symbol") == symbol and not item.get("research_run_id")
        ]
    return conversations


def _money(value: Any) -> str:
    try:
        return f"¥{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _percent(value: Any, *, already_percent: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:.2f}%" if already_percent else f"{number:.2%}"


def _time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)[:19]


def _render_metric_grid(
    items: list[tuple[str, Any]],
    *,
    per_row: int = 3,
) -> None:
    """Render financial metrics without compressing long values into narrow columns."""

    if per_row < 1:
        raise ValueError("metric grid per_row must be positive")
    for start in range(0, len(items), per_row):
        row = items[start : start + per_row]
        columns = st.columns(len(row))
        for column, (label, value) in zip(columns, row, strict=True):
            column.metric(label, value)


def _review_outcome_summary(
    overview: dict[str, Any],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a compact, mutually exclusive view of the user's decision outcomes."""

    open_statuses = {"pending", "submitted", "accepted", "partially_filled"}
    filled_orders = sum(item.get("status") == "filled" for item in orders)
    open_orders = sum(item.get("status") in open_statuses for item in orders)
    stopped_orders = max(0, len(orders) - filled_orders - open_orders)
    if reviews and fills:
        headline = "已形成可回看的决策闭环"
    elif fills:
        headline = "成交已记录，等待补充复盘"
    elif orders:
        headline = "委托已记录，等待成交结果"
    else:
        headline = "等待第一笔可回溯决策"
    total_orders = len(orders)
    return {
        "headline": headline,
        "orders": total_orders,
        "fills": len(fills),
        "positions": len(overview.get("positions") or []),
        "reviews": len(reviews),
        "filled_orders": filled_orders,
        "open_orders": open_orders,
        "stopped_orders": stopped_orders,
        "filled_percent": (filled_orders / total_orders * 100.0) if total_orders else 0.0,
        "open_percent": (open_orders / total_orders * 100.0) if total_orders else 0.0,
        "stopped_percent": (stopped_orders / total_orders * 100.0) if total_orders else 0.0,
    }


def _render_review_outcome(summary: dict[str, Any]) -> None:
    st.markdown(
        f"""
        <section class="ql-review-outcome" aria-label="决策结果概览">
          <div class="ql-review-outcome-head">
            <span>OUTCOME TRACE</span>
            <strong>{escape(str(summary['headline']))}</strong>
            <p>这里仅汇总当前用户模拟账本；历史演示、正式影子与外部组合不会混入。</p>
          </div>
          <div class="ql-review-flow">
            <div><b>{int(summary['orders'])}</b><span>已确认委托</span></div>
            <i aria-hidden="true">→</i>
            <div><b>{int(summary['fills'])}</b><span>成交记录</span></div>
            <i aria-hidden="true">→</i>
            <div><b>{int(summary['positions'])}</b><span>当前持仓</span></div>
            <i aria-hidden="true">→</i>
            <div><b>{int(summary['reviews'])}</b><span>复盘记录</span></div>
          </div>
          <div class="ql-review-distribution" aria-label="订单结果分布">
            <div class="ql-review-bar">
              <i style="width:{float(summary['filled_percent']):.2f}%"></i>
              <b style="width:{float(summary['open_percent']):.2f}%"></b>
              <em style="width:{float(summary['stopped_percent']):.2f}%"></em>
            </div>
            <p><span>已成交 {int(summary['filled_orders'])}</span><span>处理中 {int(summary['open_orders'])}</span><span>未成交结束 {int(summary['stopped_orders'])}</span></p>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _friendly_error(exc: Exception, fallback: str) -> str:
    message = str(exc).lower()
    known = (
        ("quote changed", "行情已变化，请重新运行交易前检查。"),
        ("market quote", "当前没有可用于交易的服务器行情。"),
        ("insufficient", "现金或可卖持仓不足。"),
        ("not eligible", "当前委托尚未到可成交时间。"),
        ("not found", "没有找到对应记录，页面可能已经刷新。"),
        ("outcome settlement", "该建议已有到期结果，不能再修改采纳记录。"),
        ("later trade", "该标的之后已有其他成交，为保护账本不能直接修正旧记录。"),
        ("product symbol is required", "请先输入股票或 ETF 代码，再创建或切换研究。"),
        ("conversation symbol cannot be switched", "当前 AI 对话已绑定其他标的，请新建会话后再继续。"),
        ("conversation research run cannot be switched", "当前 AI 对话已绑定另一份研究报告，请从对应报告进入。"),
        ("intraday_simulation requires", "当前没有可用于盘中模拟的实时行情。"),
        ("next_open_simulation requires", "请确认收盘价仅用于参考，并以下一交易日行情结算。"),
        ("simulation_mode must be", "请选择盘中模拟或下一交易日模拟方式。"),
    )
    return next((text for key, text in known if key in message), fallback)


def _research_action_label(value: Any) -> str:
    normalized = str(value or "review").strip().lower()
    return RESEARCH_ACTION_LABELS.get(normalized, "等待系统复核")


def _research_stage_label(value: Any) -> str:
    normalized = str(value or "unavailable").strip().lower()
    return RESEARCH_STAGE_LABELS.get(normalized, "证据阶段待确认")


def _quote_kind_label(value: Any) -> str:
    normalized = str(value or "unavailable").strip().lower()
    return QUOTE_KIND_LABELS.get(normalized, "行情状态待确认")


def _pretrade_reason_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return PRETRADE_REASON_LABELS.get(normalized, "需要人工复核的交易条件")


def _thesis_status_label(value: Any) -> str:
    normalized = str(value or "needs_review").strip().lower()
    return THESIS_STATUS_LABELS.get(normalized, "需要人工复核")


def _research_job_label(value: Any) -> str:
    normalized = str(value or "research").strip().lower()
    return RESEARCH_JOB_LABELS.get(normalized, "后台研究任务")


def _status_label(value: Any, *, fallback: str = "待确认") -> str:
    normalized = str(value or "").strip().lower()
    return STATUS_LABELS.get(normalized, fallback)


def _research_link_label(run_id: Any) -> str:
    return "已关联研究" if str(run_id or "").strip() else "未关联研究"


def _research_condition_label(value: Any) -> str:
    """Keep frozen evidence intact while presenting a dependable Chinese reading layer."""

    text = str(value or "").strip()
    if not text:
        return ""
    mapped = RESEARCH_CONDITION_LABELS.get(text)
    if mapped:
        return mapped
    normalized = re.sub(r"\s+", " ", text).strip()
    moving_average = re.fullmatch(
        r"If price sustains above (\d+)-day MA \(([^)]+)\), bearish technical view invalidated\.",
        normalized,
        flags=re.IGNORECASE,
    )
    if moving_average:
        return (
            f"若价格持续站上 {moving_average.group(1)} 日均线（{moving_average.group(2)}），"
            "当前偏弱的技术判断需要重新评估。"
        )
    if re.search(r"normalized earnings or book value", normalized, flags=re.IGNORECASE):
        return "若经标准化后的盈利或账面价值显著提升，合理价值估计需要上调复核。"
    if re.search(r"institutional flow data.*estimated proxy", normalized, flags=re.IGNORECASE):
        return "若后续可核验的机构资金数据与当前估算方向相反，资金流判断需要重新评估。"
    if re.search(r"price closes below.*ma[_ ]?20", normalized, flags=re.IGNORECASE):
        return "若价格收盘跌破 20 日均线，或近 20 日走势转弱，当前判断需要重新复核。"
    # Frozen reports may contain historic English strings. Do not claim a
    # translation we cannot support, and do not surface it in the normal user
    # flow; the immutable original remains available in the audit disclosure.
    if re.search(r"[A-Za-z]", normalized) and not re.search(r"[\u4e00-\u9fff]", normalized):
        return "这项失效条件尚未形成可靠的中文释义；请在下方“审计原文”中核对。"
    return normalized


def _research_user_copy(value: Any) -> str:
    """Apply the same user-facing safeguard to all visible report bullet text."""

    return _research_condition_label(value)


def _research_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _render_research_items(
    title: str,
    value: Any,
    *,
    empty_copy: str,
    formatter: Any | None = None,
) -> None:
    st.subheader(title)
    items = _research_items(value)
    if not items:
        st.caption(empty_copy)
        return
    for item in items[:6]:
        st.write(f"• {formatter(item) if formatter else _research_user_copy(item)}")


def _data_trust_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return DATA_TRUST_LEVEL_LABELS.get(normalized, "可信等级待确认")


def _severity_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return SEVERITY_LABELS.get(normalized, "需要复核")


def _adoption_decision_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return ADOPTION_DECISION_LABELS.get(normalized, "待确认")


def _render_recent_research_cards(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Render saved research as concise, human-readable continuation cards."""

    for item in items[:8]:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        with st.container(border=True):
            summary, conclusion = st.columns([1.35, 0.85])
            summary.markdown(f"**{item.get('symbol') or '未标注标的'}**")
            summary.caption(
                f"数据截至 {item.get('effective_as_of') or item.get('as_of') or '待确认'} · "
                f"{_research_stage_label(item.get('evidence_stage'))}"
            )
            summary.caption(f"最近更新 {_time(item.get('created_at'))}")
            conclusion.caption("当前结论")
            conclusion.markdown(f"**{_research_action_label(item.get('action'))}**")
            conclusion.caption(f"置信度 {_percent(item.get('confidence'))}")
            open_col, roundtable_col = st.columns(2)
            if open_col.button("打开研究", key=f"research_workbench_open_{run_id}"):
                return item
            if roundtable_col.button(
                "圆桌复核",
                icon=":material/groups:",
                key=f"research_workbench_roundtable_{run_id}",
            ):
                _open_research_roundtable(item)
    return None


def _validated_roundtable_source(
    path: Any,
    identity: Any,
) -> dict[str, Any]:
    """Require the exact displayed report before a roundtable can consume it."""

    record = DecisionRepository(path).get(identity.run_id)
    validated = validate_research_record(
        record,
        run_id=identity.run_id,
        symbol=identity.symbol,
    )
    if (
        validated["symbol"] != identity.symbol
        or validated["requested_as_of"].isoformat() != identity.requested_as_of
        or validated["effective_as_of"].isoformat() != identity.effective_as_of
    ):
        raise ValueError("current report identity does not match the persisted research record")
    return validated


def _render_readiness_summary(readiness: dict[str, Any]) -> None:
    coverage = readiness.get("data", {}).get("coverage", {})
    rows = []
    labels = {
        "trading_calendar": "交易日历",
        "security_master": "证券主数据",
        "industry_membership": "行业归属",
        "point_in_time_pool": "当日股票池",
    }
    for key, item in coverage.items():
        rows.append(
            {
                "数据": labels.get(key, key),
                "状态": _status_label(item.get("status")),
                "记录数": int(item.get("record_count") or 0),
                "可用候选": int(item.get("eligible_members") or 0),
                "关键字段覆盖": _percent(item.get("field_coverage")),
                "最后成功": _time(item.get("last_success_at")),
            }
        )
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    if readiness.get("blockers"):
        for blocker in readiness["blockers"]:
            st.caption(f"• {BLOCKER_LABELS.get(blocker, '系统仍在等待必要条件')} ")
    elif readiness.get("start_allowed"):
        st.success("生产数据与运行条件已准备；正式实验仍只会由真实交易日调度器启动。")


def _render_formal_experiment_summary(settings: Settings) -> None:
    status = formal_experiment_status(settings)
    experiment = status.get("experiment")
    columns = st.columns(4)
    columns[0].metric("正式实验", "已启动" if experiment else "尚未启动")
    columns[1].metric("正式样本", int(status.get("formal_samples") or 0))
    trading = status.get("shadow_trading_scorecard") or {}
    accounts = trading.get("accounts") or []
    columns[2].metric("影子账户", len(accounts))
    columns[3].metric(
        "证据状态", "等待真实时间" if not status.get("formal_samples") else "持续采集"
    )
    st.caption("历史演示、人工研究和产品使用数据不会进入这里。5/20日样本必须自然到期。")


def _render_evidence_context(context: dict[str, Any]) -> None:
    blocks = context.get("blocks") or context.get("evidence_blocks") or []
    if isinstance(blocks, dict):
        blocks = list(blocks.values())
    rows = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        rows.append(
            {
                "证据块": block.get("block_type")
                or block.get("name")
                or block.get("domain")
                or "证据",
                "来源": block.get("source") or "未注明",
                "截至": block.get("as_of") or "—",
                "可用时间": _time(block.get("available_at")),
                "质量": block.get("quality") or block.get("data_quality") or "—",
                "降级": "是" if block.get("degraded") else "否",
            }
        )
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("报告已保存上下文指纹；当前摘要没有可展开的证据块。")


def _go_latest_roundtable(settings: Settings) -> None:
    """跳到最新一份研究的专家圆桌；没有研究档案时退回研究台。"""

    try:
        path = settings.resolve(settings.get("system.database_path"))
        index = DecisionRepository(path).research_page(page=1, page_size=1)
        items = list(index.get("items") or [])
    except Exception:
        items = []
    if items:
        _open_research_roundtable(items[0])
    else:
        _go_to("研究台")


def _render_home_hero(settings: Settings) -> None:
    """首屏英雄区：把简介承诺（AI 关进笼子、代码说了算）前置为可见的第一屏。

    纯呈现层：阈值只读自 settings 的 [risk] 配置；主 CTA 复用既有历史 demo
    入口；圆桌 CTA 复用 _open_research_roundtable 直跳最新研究的圆桌页。
    """
    st.markdown(
        """
        <section class="ql-hero">
          <div class="ql-hero-seal" aria-hidden="true"><i>代码<br>说了算</i></div>
          <span>QUANTLAB · 把 AI 关进笼子</span>
          <h1>研究由 AI 圆桌完成，<em>红线由代码强制执行。</em></h1>
          <p>技术面、动量、价值否决、风险否决、宏观五个角色组成多 Agent 圆桌，负责研究、比较与反证，形成操作草稿；而仓位、集中度、回撤、ST、T+1、涨跌停和交易成本这些红线，全部由确定性代码引擎强制拦截。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    cta_run, cta_council = st.columns([1, 1])
    with cta_run:
        if st.button(
            "▶ 一键跑通完整决策链路",
            key="hero_open_historical_demo",
            type="primary",
            width="stretch",
            help="用冻结历史数据与独立模拟账本，三分钟走完证据 → 圆桌 → 风控 → 成交 → 复盘。不连券商、不进正式实验。",
        ):
            st.session_state[HISTORICAL_DEMO_OPEN_KEY] = True
            st.rerun()
    with cta_council:
        if st.button(
            "发起一场圆桌讨论",
            key="hero_go_research",
            icon=":material/groups:",
            width="stretch",
            help="带着最新一份研究档案，直接进入专家圆桌看五个角色交锋；还没有档案时先去研究台生成。",
        ):
            _go_latest_roundtable(settings)

    # 圆桌（首屏 C 位）——真实的圆桌舞台预览，点击直达专家圆桌
    catalog = {item["key"]: item for item in roundtable_participant_catalog()}
    stage_participants = ["technical", "bull", "risk", "bear", "macro"]
    st.markdown(
        '<div class="ql-section-title ql-home-stage-title"><span>MULTI-AGENT COUNCIL · 圆桌研究</span>'
        "<strong>五个角色围一张桌子：研究、比较、反证</strong></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        _roundtable_stage_html(
            participants=stage_participants,
            catalog=catalog,
            turns=[],
            status="queued",
        ),
        unsafe_allow_html=True,
    )
    stage_cta, stage_hint = st.columns([1, 3])
    if stage_cta.button(
        "进入专家圆桌",
        key="hero_enter_roundtable",
        icon=":material/meeting_room:",
        width="stretch",
        help="带着最新一份研究档案进入圆桌页，选择专家、发起讨论、看逐轮发言。",
    ):
        _go_latest_roundtable(settings)
    stage_hint.caption("圆桌围绕冻结研究展开：逐轮发言、交叉质疑、主持人总结，全部留痕可回放。")

    # 红线笼子——阈值读真实 [risk] 配置，市场规则为引擎内联逻辑
    risk = settings.get("risk") or {}

    def _pct(key: str, fallback: float) -> str:
        try:
            value = float(risk.get(key, fallback))
        except (TypeError, ValueError):
            value = fallback
        return f"{value * 100:.0f}%"

    exposure = _pct("max_total_exposure", 0.80)
    single = _pct("max_single_position", 0.15)
    industry = _pct("max_industry_exposure", 0.30)
    drawdown = _pct("max_portfolio_drawdown", 0.15)
    guards = [
        ("仓位上限", exposure, "总权益暴露不得超过", False),
        ("单票集中度", single, "任一标的持仓上限", False),
        ("行业集中度", industry, "单一行业暴露上限", False),
        ("回撤约束", drawdown, "组合最大回撤触发", False),
        ("ST 否决", "拦截", "ST/风险警示标的一律否决", True),
        ("T+1", "强制", "当日买入不可当日卖出", True),
        ("涨跌停", "拦截", "触及涨跌停不予撮合", True),
        ("交易成本", "计入", "佣金 / 印花税 / 滑点全额计提", True),
    ]
    cells = "".join(
        f'<div class="ql-guard"><i>{escape(name)}</i>'
        f'<b class="{"ql-guard-flag" if is_flag else ""}">{escape(value)}</b>'
        f"<em>{escape(desc)}</em></div>"
        for name, value, desc, is_flag in guards
    )
    st.markdown(
        f"""
        <div class="ql-cage">
          <div class="ql-cage-head">
            <div><span>DETERMINISTIC GUARDRAILS · 代码说了算</span>
            <strong>每一个操作草稿，都被这 8 条红线检查过</strong></div>
            <small>阈值读自风控配置</small>
          </div>
          <div class="ql-cage-grid">{cells}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_historical_demo_entry() -> None:
    with st.container(key="historical_demo_entry", border=False):
        intro, action = st.columns([4, 1])
        with intro:
            st.markdown(
                """
                <section class="ql-demo-entry">
                  <span>完整决策示例</span>
                  <h2>三分钟走完证据、风控、成交与复盘</h2>
                  <p>使用冻结历史数据和独立模拟账本；不会连接券商，也不会进入正式实验或训练集。</p>
                </section>
                """,
                unsafe_allow_html=True,
            )
        if action.button(
            "打开示例",
            key="open_historical_demo_workspace",
            icon=":material/play_circle:",
            type="primary",
            width="stretch",
        ):
            st.session_state[HISTORICAL_DEMO_OPEN_KEY] = True
            st.rerun()


def _render_historical_demo_steps(stage: str) -> None:
    completed = 4 if stage == "completed" else 3 if stage == "pretrade_ready" else 0
    labels = ("发现候选", "冻结研究", "交易前检查", "成交与复盘")
    items = "".join(
        f'<div class="ql-demo-step {"ql-demo-step-done" if index <= completed else ""}">'
        f"<b>{index:02d}</b><span>{label}</span></div>"
        for index, label in enumerate(labels, start=1)
    )
    st.markdown(f'<div class="ql-demo-steps">{items}</div>', unsafe_allow_html=True)


def _render_historical_demo_workspace(settings: Settings) -> None:
    controls = st.columns([1, 1, 4])
    if controls[0].button(
        "返回今日",
        key="close_historical_demo_workspace",
        icon=":material/arrow_back:",
        width="stretch",
    ):
        st.session_state[HISTORICAL_DEMO_OPEN_KEY] = False
        st.rerun()
    if controls[1].button(
        "重新开始",
        key="clear_historical_demo_workspace",
        icon=":material/refresh:",
        width="stretch",
    ):
        try:
            reset_historical_research_demo(settings)
            st.session_state.pop(HISTORICAL_DEMO_PREPARED_KEY, None)
            st.session_state.pop(HISTORICAL_DEMO_RESULT_KEY, None)
            st.session_state.pop("historical_demo_user_confirmed", None)
            st.rerun()
        except Exception as exc:
            st.error(_friendly_error(exc, "隔离演示账本暂时无法重置。"))

    st.markdown(
        """
        <section class="ql-demo-boundary">
          <span>ISOLATED HISTORICAL DEMO · 冻结历史演示</span>
          <strong>证据基准日 2026-01-05 · 独立账本 · 用户确认</strong>
          <p>演示使用的行情、研究、订单与结果全部冻结在历史基准日（2026-01-05），与今日真实行情无关。工程链路真实运行，但不构成实时建议或收益证明。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    prepared = st.session_state.get(HISTORICAL_DEMO_PREPARED_KEY)
    result = st.session_state.get(HISTORICAL_DEMO_RESULT_KEY)
    active = result or prepared
    _render_historical_demo_steps(str((active or {}).get("stage") or "idle"))

    if active is None:
        st.subheader("从一组冻结候选开始")
        st.write("系统将读取版本化历史样本，选择研究分数最高的候选，并运行真实的确定性交易前检查。此时不会创建订单。")
        if st.button(
            "生成研究与交易前检查",
            key="prepare_historical_demo",
            icon=":material/psychology:",
            type="primary",
        ):
            try:
                with st.spinner("冻结证据并运行交易前检查..."):
                    prepared = prepare_historical_research_demo(settings)
                st.session_state[HISTORICAL_DEMO_PREPARED_KEY] = prepared
                if prepared.get("stage") == "completed":
                    st.session_state[HISTORICAL_DEMO_RESULT_KEY] = prepared
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "隔离决策示例暂时无法准备。"))
        return

    dataset = active["dataset"]
    candidate = active["selected_candidate"]
    research = active["research"]
    pretrade = active["pretrade"]
    candidates = sorted(
        active["candidates"], key=lambda item: float(item["research_score"]), reverse=True
    )
    st.subheader("1 · 发现候选")
    st.caption(
        f"冻结数据日 {dataset['signal_date']} · 来源 {dataset['source']} · 指纹 {dataset['fingerprint'][:12]}"
    )
    st.dataframe(
        [
            {
                "候选": item["name"],
                "代码": item["symbol"],
                "类别": item["category"],
                "研究分数": float(item["research_score"]),
                "建议": RESEARCH_ACTION_LABELS.get(item["suggested_action"], item["suggested_action"]),
            }
            for item in candidates
        ],
        hide_index=True,
        width="stretch",
    )

    st.subheader("2 · 冻结研究")
    _render_metric_grid(
        [
            ("当前候选", candidate["name"]),
            ("研究结论", RESEARCH_ACTION_LABELS.get(research["suggested_action"], research["suggested_action"])),
            ("研究分数", f"{float(candidate['research_score']):.0f}/100"),
            ("研究身份", "已冻结" if research.get("frozen") else "未冻结"),
        ]
    )
    evidence_columns = st.columns(3)
    evidence_groups = (
        ("支持证据", research["supporting_evidence"]),
        ("反对证据", research["opposing_evidence"]),
        ("失效条件", research["invalidation_conditions"]),
    )
    for column, (title, items) in zip(evidence_columns, evidence_groups, strict=True):
        with column:
            st.markdown(f"**{title}**")
            for item in items:
                st.write(f"· {item}")

    st.subheader("3 · 交易前检查")
    _render_metric_grid(
        [
            ("确定性规则", "通过" if pretrade.get("hard_risk_passed") else "未通过"),
            ("参考价格", _money(pretrade.get("reference_price"))),
            ("模拟数量", int(pretrade.get("requested_quantity") or 0)),
            ("预计费用", _money(pretrade.get("estimated_transaction_fees"))),
            ("操作后现金", _money(pretrade.get("post_trade_cash"))),
            ("下跌 10% 影响", _money(pretrade.get("loss_if_symbol_down_10pct"))),
        ]
    )
    if pretrade.get("hard_failures"):
        st.error("；".join(str(item) for item in pretrade["hard_failures"]))
    elif result is None:
        st.success("账户、交易规则、费用和组合影响均已核对；尚未创建任何订单。")

    if result is None:
        confirmed = st.checkbox(
            "我确认这是隔离历史模拟，订单不会连接券商，也不会进入正式实验或训练集。",
            key="historical_demo_user_confirmed",
        )
        if st.button(
            "确认隔离模拟订单",
            key="confirm_historical_demo_order",
            icon=":material/check_circle:",
            type="primary",
            disabled=not confirmed or not bool(pretrade.get("allowed_to_submit")),
        ):
            try:
                with st.spinner("写入隔离订单、模拟次日开盘成交并完成盯市..."):
                    result = confirm_historical_research_demo(
                        settings,
                        check_id=pretrade["check_id"],
                        dataset_fingerprint=dataset["fingerprint"],
                        confirmed=confirmed,
                    )
                st.session_state[HISTORICAL_DEMO_RESULT_KEY] = result
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "隔离模拟订单未完成，未改变普通用户账本。"))
        return

    order = result["order"]
    fills = result.get("fills") or []
    account = result["account"]
    transaction_cost = sum(
        float(item.get("transaction_fees") or 0) + float(item.get("slippage") or 0)
        for item in fills
    )
    pnl = float(account.get("equity") or 0) - 100_000.0
    st.subheader("4 · 成交与复盘")
    _render_metric_grid(
        [
            ("订单状态", STATUS_LABELS.get(order["status"], order["status"])),
            ("成交数量", int(order.get("filled_quantity") or 0)),
            ("交易费用", _money(transaction_cost)),
            ("成交后总资产", _money(account.get("equity"))),
            ("样本期盈亏", _money(pnl)),
            ("正式证据污染", int(result["formal_experiments_in_demo_database"])),
        ]
    )
    replay = st.columns(3)
    replay[0].markdown(
        f"**当时判断**\n\n{RESEARCH_ACTION_LABELS.get(research['suggested_action'], research['suggested_action'])} · "
        f"{candidate['name']}"
    )
    replay[1].markdown(
        f"**真实模拟结果**\n\n{dataset['execution_date']} 次日开盘成交，收盘后账本盈亏 {_money(pnl)}。"
    )
    replay[2].markdown(
        "**证据边界**\n\n历史结果只验证产品闭环；不计入训练、正式前瞻或收益能力结论。"
    )
    st.caption(
        f"研究身份 {research['research_id']} · 隔离账本 {result['isolated_database']} · "
        f"正式实验 {result['formal_experiments_in_demo_database']}"
    )


def render_home(settings: Settings) -> None:
    if st.session_state.get(HISTORICAL_DEMO_OPEN_KEY):
        _render_historical_demo_workspace(settings)
        return
    _render_home_hero(settings)
    path = settings.resolve(settings.get("system.database_path"))
    try:
        readiness = _ui_readiness_snapshot(settings)
        snapshot = build_product_home(settings, readiness=readiness)
        important_tasks = Round9Repository(path).decision_tasks(status="open", limit=8)
        active_theses = Round8Repository(path).theses(
            statuses=("active", "strengthened", "unchanged", "weakened", "damaged", "broken")
        )
    except Exception as exc:
        render_page_state(
            "error",
            "首页暂时无法读取",
            _friendly_error(exc, "账户、任务或研究状态暂不可用；没有创建任何订单或新结论。"),
            retry_label="重新读取",
            retry_key="home_safe_retry",
        )
        return

    try:
        unread_all = NotificationRepository(path).list(
            unread_only=True,
            limit=50,
        )
        unread_notifications = [
            item for item in unread_all if item.get("severity") in {"warning", "critical"}
        ] + [item for item in unread_all if item.get("severity") == "info"]
    except Exception:
        # A notification read failure must not hide the user's account state.
        unread_notifications = []

    def thesis_status(item: dict[str, Any]) -> str:
        return str(item.get("status") or item.get("lifecycle_status") or "unavailable")

    thesis_review_count = sum(
        thesis_status(item) in {"weakened", "damaged", "broken"}
        or any(
            assumption.get("status") == "needs_review"
            for assumption in item.get("assumptions") or []
        )
        for item in active_theses
    )
    thesis_red_line_count = sum(
        thesis_status(item) in {"damaged", "broken"} for item in active_theses
    )
    critical_tasks = sum(item.get("severity") == "critical" for item in important_tasks)
    pending_orders = snapshot.get("pending_orders") or []
    action_count = len(important_tasks) + thesis_review_count + len(pending_orders)
    if critical_tasks or thesis_red_line_count:
        field_state, headline = "alert", "有高优先级事项需要先处理"
    elif action_count:
        field_state, headline = "review", f"有 {action_count} 项等待确认或复核"
    else:
        field_state, headline = "calm", "暂无需要立即确认的交易操作"
    account = snapshot.get("account")
    readiness = snapshot.get("data_status") or {}
    source_states = (readiness.get("data") or {}).get("source_states") or {}
    available_sources = sum(bool(value) for value in source_states.values())
    source_total = len(source_states)
    st.markdown(
        f"""
        <section class="ql-decision-field ql-decision-{field_state}">
          <div><span>DECISION FIELD</span><h2>{escape(headline)}</h2>
          <p>账户、任务、研究变化与待处理委托已按当前数据库状态汇总；保持不动也是有效决策。</p></div>
          <div class="ql-field-signal"><i></i><b></b><em>{action_count:02d}</em><small>OPEN ITEMS</small></div>
        </section>
        <div class="ql-status-rail">
          <span><b>{len(important_tasks)}</b> 决策任务</span>
          <span><b>{thesis_review_count}</b> 论文待复评</span>
          <span><b>{len(pending_orders)}</b> 待处理委托</span>
          <span><b>{snapshot.get("unread_notifications", 0)}</b> 未读通知</span>
          <span><b>{available_sources}/{source_total or 0}</b> 数据源有记录</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    state = snapshot.get("state")
    if state == "no_account":
        render_page_state(
            "empty",
            "还没有用户模拟账户",
            "创建账户后，这里会显示资产、持仓、订单和可回溯的决策链。",
        )
        if st.button("创建模拟账户", key="home_create_account", type="primary"):
            _go_to("组合与交易")
    elif state == "no_data":
        render_page_state(
            "unavailable",
            "可信行情尚不可用",
            "账户与历史研究仍可读取；依赖当前行情的研究和交易动作保持关闭，不会用样例行情填充。",
        )
    elif state == "degraded":
        render_page_state(
            "degraded",
            "部分能力正在降级运行",
            "最近成功版本仍可阅读；当日成交与正式前瞻会保持关闭，直到数据条件恢复。",
        )

    st.subheader("账户快照")
    investor = snapshot.get("investor_portfolio")
    if account:
        _render_metric_grid(
            [
                ("模拟总资产", f"¥{account['equity']:,.2f}"),
                ("可用现金", f"¥{account['available_cash']:,.2f}"),
                ("今日盈亏", f"¥{account['today_pnl']:,.2f}"),
                ("累计收益", f"{account['total_return']:.2%}"),
                ("当前持仓", len(account["positions"])),
                ("红线风险", thesis_red_line_count),
            ]
        )
    elif investor and investor.get("nav"):
        latest = investor["nav"][-1]
        _render_metric_grid(
            [
                ("外部只读组合", _money(latest.get("equity"))),
                ("现金", _money(latest.get("cash"))),
                ("当日盈亏", _money(latest.get("today_pnl"))),
                ("持仓", len(investor.get("positions") or [])),
            ]
        )
    else:
        st.caption("尚无可展示账户。首页不会用演示资产替代真实账本。")

    decision_column, research_column = st.columns([1.08, 0.92])
    with decision_column:
        st.subheader("现在需要你决定")
        if unread_notifications:
            st.markdown("**最新提醒**")
            for item in unread_notifications[:3]:
                notice, action = st.columns([4.2, 1])
                with notice:
                    notification_title, notification_content = _notification_display(item)
                    severity = {
                        "critical": "重要",
                        "warning": "请复核",
                        "info": "更新",
                    }.get(item.get("severity"), "更新")
                    st.markdown(f"**{severity} · {escape(notification_title)}**")
                    st.caption(f"{notification_content} · {_time(item.get('created_at'))}")
                if action.button(
                    NOTIFICATION_ACTION_LABELS.get(str(item.get("action_type") or ""), "查看"),
                    key=f"home_notification_{item['notification_id']}",
                    width="stretch",
                ):
                    _open_notification(settings, item)
            if len(unread_notifications) > 3 and st.button(
                "打开全部通知",
                key="home_open_notifications",
            ):
                _go_to("专业空间")
        if important_tasks:
            for task in important_tasks[:5]:
                severity = "立即处理" if task.get("severity") == "critical" else "需要复核"
                st.warning(
                    f"{severity} · {task.get('title', '决策任务')}\n\n{task.get('user_summary', '请打开任务查看详情。')}"
                )
        elif not pending_orders and not snapshot.get("risk_items"):
            render_page_state(
                "success",
                "当前没有必须处理的事项",
                "没有新动作并不代表系统空闲；数据与论文仍会按既定规则持续检查。",
            )
        if pending_orders:
            st.markdown("**待处理模拟委托**")
            st.dataframe(
                [
                    {
                        "标的": item.get("symbol") or "—",
                        "方向": "买入" if item.get("side") == "buy" else "卖出",
                        "状态": _status_label(item.get("status")),
                        "数量": item.get("requested_quantity") or 0,
                        "可成交日": item.get("eligible_trade_date") or "—",
                    }
                    for item in pending_orders[:6]
                ],
                hide_index=True,
                width="stretch",
            )
            if st.button("打开待处理订单", key="home_open_pending_order"):
                pending_order = pending_orders[0]
                clear_product_research_context(st.session_state)
                _go_to(
                    "组合与交易",
                    symbol=pending_order.get("symbol"),
                    account_id=pending_order.get("account_id") or (account or {}).get("account_id"),
                    order_id=pending_order.get("order_id"),
                )
        for item in (snapshot.get("risk_items") or [])[:4]:
            st.warning(item.get("message") or "有一项风险需要复核。")
        if st.button("查看全部任务与通知", key="home_open_tasks"):
            _go_to("专业空间")

    with research_column:
        st.subheader("研究变化与论文")
        if active_theses:
            st.dataframe(
                [
                    {
                        "标的": item.get("symbol") or "—",
                        "状态": thesis_status(item),
                        "待复核假设": sum(
                            assumption.get("status") == "needs_review"
                            for assumption in item.get("assumptions") or []
                        ),
                        "红线": len(item.get("red_lines") or []),
                        "下次检查": item.get("next_check_at") or "待安排",
                    }
                    for item in active_theses[:8]
                ],
                hide_index=True,
                width="stretch",
            )
        decisions = snapshot.get("latest_ai_suggestions") or []
        if decisions:
            selected_run_id = st.selectbox(
                "最近研究",
                [item["run_id"] for item in decisions],
                format_func=lambda value: next(
                    f"{item['symbol']} · {item['as_of']} · {item.get('action', 'review')}"
                    for item in decisions
                    if item["run_id"] == value
                ),
                key="home_selected_research_run",
            )
            selected = next(item for item in decisions if item["run_id"] == selected_run_id)
            if st.button("打开研究上下文", key="home_open_research", type="primary"):
                requested = selected.get("requested_as_of") or selected.get("as_of")
                effective = selected.get("effective_as_of") or selected.get("as_of")
                _go_to(
                    "研究台",
                    symbol=selected["symbol"],
                    research_run_id=selected["run_id"],
                    research_requested_as_of=requested,
                    research_effective_as_of=effective,
                )
        else:
            render_page_state(
                "empty",
                "尚无已保存研究",
                "可以从市场线索进入，也可以在研究台手动输入股票或 ETF。",
            )
            if st.button("开始研究", key="home_start_research"):
                _go_to("研究台")

    if account and account.get("positions"):
        with st.expander("持仓明细", expanded=False):
            st.dataframe(
                pd.DataFrame(account["positions"])[
                    [
                        "symbol",
                        "quantity",
                        "latest_price",
                        "market_value",
                        "unrealized_pnl",
                        "weight",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )
    with st.expander("数据状态与正式实验边界"):
        _render_readiness_summary(readiness)
        _render_formal_experiment_summary(settings)


def _render_historical_demo(settings: Settings) -> None:
    live = live_demo_status(settings)
    st.markdown("**Live：当前服务器真实数据**")
    if live["data_available"]:
        st.caption("真实数据状态已读取；不可用或不可操作状态会原样展示。")
    else:
        st.warning("当前没有可用服务器数据，Live 不会伪造成功。")
    st.markdown("**Historical Research Demo：冻结、版本化、可离线复现**")
    st.caption("独立演示数据库；仅用于探索研究，不计入正式成绩、训练集或用户模拟账户。")
    if st.button("运行冻结历史 Demo", key="product_historical_demo"):
        try:
            with st.spinner("加载冻结证据并完成隔离演示..."):
                st.session_state["historical_demo_result"] = run_historical_research_demo(settings)
        except Exception as exc:
            st.error(_friendly_error(exc, "历史演示暂时无法完成。"))
    demo = st.session_state.get("historical_demo_result")
    if demo:
        selected = demo["selected_candidate"]
        st.success(
            f"演示完成：{selected['name']} → {STATUS_LABELS.get(demo['order']['status'], demo['order']['status'])}"
        )
        _render_metric_grid(
            [
                ("冻结数据日", demo["dataset"]["signal_date"]),
                ("成交数量", demo["order"]["filled_quantity"]),
                ("成交后总资产", _money(demo["account"]["equity"])),
                ("正式实验污染", "0"),
            ]
        )
        st.caption(demo["claim_boundary"])


def render_market_and_discovery(settings: Settings) -> None:
    navigation_context = consume_product_context(st.session_state, "market_discovery")
    if navigation_context is not None and navigation_context.symbol:
        # Navigation may prefill a search, but never starts a market request by itself.
        st.session_state["product_search"] = navigation_context.symbol
        st.session_state["product_market_focus_symbol"] = navigation_context.symbol
    try:
        readiness = _ui_readiness_snapshot(settings)
    except Exception as exc:
        render_page_state(
            "error",
            "数据状态读取失败",
            _friendly_error(exc, "暂时无法确认行情来源和新鲜度。"),
        )
        return
    if readiness["data"]["source_states"]:
        _render_readiness_summary(readiness)
    else:
        render_page_state(
            "unavailable",
            "尚无可信数据刷新记录",
            "免费源不可用时这里会保持空，不会显示合成行情。可稍后重试刷新。",
        )

    radar_date = st.date_input("市场截止日期", value=date.today(), key="product_radar_date")
    if st.button("刷新市场状态", key="product_refresh_radar", type="primary"):
        try:
            with st.spinner("读取真实行情并计算市场状态..."):
                st.session_state["product_radar"] = build_market_radar(
                    settings, radar_date, include_sectors=True
                )
            record_product_usage(
                settings,
                event_type="market_refresh_completed",
                entrypoint="市场与发现",
                payload={"as_of": radar_date.isoformat()},
            )
        except Exception as exc:
            st.error(_friendly_error(exc, "市场数据暂不可用，系统不会显示合成行情。"))
            record_product_usage(
                settings,
                event_type="flow_interrupted_by_data_unavailable",
                entrypoint="市场与发现",
                payload={"error_type": type(exc).__name__},
            )
    radar = st.session_state.get("product_radar")
    if radar:
        columns = st.columns(4)
        columns[0].metric("市场状态", radar["market_regime"])
        columns[1].metric("风险偏好", radar["risk_appetite"])
        columns[2].metric("上涨宽度", f"{radar['breadth']['positive_20']:.0%}")
        columns[3].metric(
            "数据覆盖", f"{radar['coverage']['available']}/{radar['coverage']['requested']}"
        )
        st.dataframe(pd.DataFrame(radar["instruments"]), hide_index=True, width="stretch")
        if radar.get("sectors"):
            st.subheader("行业趋势")
            st.dataframe(pd.DataFrame(radar["sectors"]), hide_index=True, width="stretch")

    st.subheader("资金活跃度")
    evidence = EvidenceRepository(settings.resolve(settings.get("system.database_path")))
    flows = evidence.flows("market", limit=10)
    if not flows:
        st.caption("资金快照不可用。系统不会把 signed-turnover 代理显示成主力净流入。")
    else:
        flow_rows = []
        for item in flows[:10]:
            methodology = str(
                item.get("methodology") or item.get("payload", {}).get("methodology") or ""
            )
            label = (
                "估算代理"
                if "proxy" in methodology.lower() or "signed" in methodology.lower()
                else "已确认数据"
            )
            payload = item.get("payload") or {}
            flow_rows.append(
                {
                    "口径": label,
                    "来源": item.get("source") or "—",
                    "时间": _time(item.get("available_at")),
                    "质量": item.get("quality") or "—",
                    "成交额": _money(payload.get("turnover") or payload.get("amount")),
                    "市场宽度": _percent(payload.get("breadth")),
                    "说明": methodology or "供应商原始口径",
                }
            )
        st.dataframe(flow_rows, hide_index=True, width="stretch")
        st.caption("“估算代理”只表示成交活跃度方向，不代表已确认机构持仓或主力净流入。")

    st.subheader("搜索与候选")
    keyword = st.text_input(
        "股票代码或名称", placeholder="例如 600519、贵州茅台", key="product_search"
    )
    focus_symbol = st.session_state.get("product_market_focus_symbol")
    if focus_symbol:
        st.caption(f"已从提醒带入 {focus_symbol}。点击搜索后再核对行情日期、来源和资金流口径。")
    if st.button("搜索", key="product_search_button"):
        try:
            st.session_state["product_search_results"] = search_stocks(settings, keyword)["results"]
            st.session_state["product_search_attempted"] = True
        except Exception as exc:
            st.error(_friendly_error(exc, "搜索暂时失败，请稍后重试。"))
    results = st.session_state.get("product_search_results", [])
    if results:
        st.dataframe(results, hide_index=True, width="stretch")
        symbols = [str(item.get("symbol")) for item in results if item.get("symbol")]
        if symbols:
            selected_symbol = st.selectbox(
                "选择研究标的",
                symbols,
                format_func=lambda value: next(
                    str(item.get("name") or value) + f" ({value})"
                    for item in results
                    if item.get("symbol") == value
                ),
                key="product_search_selected_symbol",
            )
            if st.button("前往 AI 研究", key="product_search_to_research", type="primary"):
                _go_to("研究台", symbol=selected_symbol)
    elif keyword.strip() and st.session_state.get("product_search_attempted"):
        render_page_state("empty", "没有找到匹配标的", "请检查代码或名称后重试。")
    if st.button("生成透明候选列表", key="product_recommend"):
        try:
            st.session_state["product_candidates"] = recommend_stocks(
                settings, candidate_limit=20, top_n=10, save=True
            )
        except Exception as exc:
            st.error(_friendly_error(exc, "候选暂时无法生成；系统不会补造结果。"))
    candidates = st.session_state.get("product_candidates")
    if candidates:
        st.dataframe(candidates["candidates"], hide_index=True, width="stretch")
        st.caption("候选表示优先研究顺序，不是买入建议。资金流不能单独生成订单。")
        candidate_symbols = [
            str(item.get("symbol"))
            for item in candidates.get("candidates", [])
            if item.get("symbol")
        ]
        if candidate_symbols:
            candidate_symbol = st.selectbox(
                "从候选中选择",
                candidate_symbols,
                key="product_candidate_selected_symbol",
            )
            if st.button("研究这个候选", key="product_candidate_to_research"):
                _go_to("研究台", symbol=candidate_symbol)


def render_ai_research(settings: Settings) -> None:
    """Render the lightweight index; reports now live on their own route."""

    _render_research_hub(settings)
    return

def _research_identity_from_index(item: dict[str, Any]) -> tuple[str, str, str, str] | None:
    symbol = str(item.get("symbol") or "").strip()
    run_id = str(item.get("run_id") or "").strip()
    requested = str(item.get("requested_as_of") or item.get("as_of") or "").strip()
    effective = str(item.get("effective_as_of") or item.get("as_of") or "").strip()
    if not all((symbol, run_id, requested, effective)):
        return None
    return symbol, run_id, requested, effective


def _open_research_detail(item: dict[str, Any]) -> None:
    identity = _research_identity_from_index(item)
    if identity is None:
        st.error("这份研究的身份信息不完整，暂时不能打开。")
        return
    symbol, run_id, requested, effective = identity
    _go_to(
        "研究详情",
        symbol=symbol,
        research_run_id=run_id,
        research_requested_as_of=requested,
        research_effective_as_of=effective,
    )


def _open_research_roundtable(item: dict[str, Any]) -> None:
    identity = _research_identity_from_index(item)
    if identity is None:
        st.error("这份研究的身份信息不完整，暂时不能发起圆桌。")
        return
    symbol, run_id, requested, effective = identity
    _go_to(
        "专家圆桌",
        symbol=symbol,
        research_run_id=run_id,
        research_requested_as_of=requested,
        research_effective_as_of=effective,
    )


def _render_research_hub(settings: Settings) -> None:
    """A small index query first; full evidence is loaded only after selection."""

    path = settings.resolve(settings.get("system.database_path"))
    st.caption("打开一份报告后会进入独立详情页；这里不会预先读取所有冻结证据。")
    query_col, action_col, stage_col = st.columns([1.25, 1, 1])
    query = query_col.text_input(
        "搜索研究",
        placeholder="代码或研究编号",
        key="product_research_index_query",
    ).strip()
    action_options = {
        "": "全部结论",
        "buy": "建议买入",
        "hold": "建议持有",
        "review": "等待复核",
        "sell": "建议卖出",
        "avoid": "暂不参与",
    }
    action = action_col.selectbox(
        "结论筛选",
        list(action_options),
        format_func=action_options.get,
        key="product_research_index_action",
    )
    stage_options = {
        "": "全部证据阶段",
        "research_only": "研究线索",
        "formal_forward": "正式前瞻",
        "shadow": "影子验证",
    }
    stage = stage_col.selectbox(
        "证据阶段",
        list(stage_options),
        format_func=stage_options.get,
        key="product_research_index_stage",
    )
    filter_signature = (query, action, stage)
    if st.session_state.get("product_research_index_filters") != filter_signature:
        st.session_state["product_research_index_filters"] = filter_signature
        st.session_state["product_research_index_page"] = 1
    page = max(1, int(st.session_state.get("product_research_index_page") or 1))
    try:
        index = DecisionRepository(path).research_page(
            page=page,
            page_size=8,
            query=query or None,
            action=action or None,
            evidence_stage=stage or None,
        )
    except Exception as exc:
        render_page_state(
            "error",
            "研究索引暂时无法读取",
            _friendly_error(exc, "未能读取已保存研究；不会创建或改写任何报告。"),
        )
        return
    total = int(index["total"])
    total_pages = max(1, math.ceil(total / int(index["page_size"])))
    if page > total_pages:
        st.session_state["product_research_index_page"] = total_pages
        st.rerun()
    st.markdown(
        f'<div class="ql-section-title"><span>研究档案</span><strong>活跃研究报告 <em>{total} 份</em></strong></div>',
        unsafe_allow_html=True,
    )
    items = list(index["items"])
    if items:
        selected = _render_recent_research_cards(items)
        if selected:
            _open_research_detail(selected)
        pager_left, pager_status, pager_right = st.columns([1, 2, 1])
        if pager_left.button(
            "上一页",
            icon=":material/arrow_back:",
            disabled=page <= 1,
            key="research_index_previous_page",
        ):
            st.session_state["product_research_index_page"] = page - 1
            st.rerun()
        pager_status.caption(f"第 {page} / {total_pages} 页，每页 8 份")
        if pager_right.button(
            "下一页",
            icon=":material/arrow_forward:",
            disabled=page >= total_pages,
            key="research_index_next_page",
        ):
            st.session_state["product_research_index_page"] = page + 1
            st.rerun()
    else:
        render_page_state(
            "empty",
            "没有匹配的研究报告",
            "可以调整筛选条件，或从下方创建一份新的研究。",
        )

    with st.expander("创建新研究", expanded=not bool(items)):
        fields = st.columns([1.4, 1])
        symbol = fields[0].text_input(
            "研究标的",
            value=str(st.session_state.get("product_research_symbol") or "sh510300"),
            placeholder="股票或 ETF 代码，例如 sh600519",
            key="product_research_symbol_new",
        ).strip()
        as_of = fields[1].date_input("研究截止日期", value=date.today(), key="product_research_date_new")
        if st.button(
            "生成独立研究报告",
            type="primary",
            key="product_run_research_from_hub",
            disabled=not bool(symbol),
        ):
            update_product_selection(st.session_state, symbol=symbol, requested_as_of=as_of)
            mark_research_loading(st.session_state, symbol=symbol, requested_as_of=as_of)
            loading = _research_loading_indicator()
            try:
                output = analyze_symbol(settings, symbol, as_of)
                repository = DecisionRepository(path)
                repository.save(
                    output["decision_run"],
                    research_context=research_persistence_context(output),
                    provenance=ResearchProvenance(
                        origin="user_interactive_research",
                        requested_as_of=as_of,
                        evidence_stage="research_only",
                    ),
                )
                stored = repository.get(output["decision_run"].run_id)
                if stored is None:
                    raise RuntimeError("persisted research record is unavailable")
                report = build_stored_audit_package(stored)
                identity = cache_research_report(
                    st.session_state,
                    report,
                    symbol=symbol,
                    requested_as_of=as_of,
                )
                bind_product_research_context(st.session_state, identity)
                record_product_usage(
                    settings,
                    event_type="ai_recommendation_viewed",
                    entrypoint="研究台",
                    symbol=symbol,
                    reference_id=identity.run_id,
                )
                _go_to(
                    "研究详情",
                    symbol=identity.symbol,
                    research_run_id=identity.run_id,
                    research_requested_as_of=identity.requested_as_of,
                    research_effective_as_of=identity.effective_as_of,
                )
            except Exception as exc:
                mark_research_failed(st.session_state, symbol=symbol, requested_as_of=as_of)
                st.error(_friendly_error(exc, "研究暂时失败；缺失事实不会由模型补造。"))
            finally:
                loading.empty()


def _load_current_research_detail(settings: Settings) -> tuple[dict[str, Any], Any] | None:
    context = product_context(st.session_state)
    if not (
        context.symbol
        and context.research_run_id
        and context.research_requested_as_of
        and context.research_effective_as_of
    ):
        render_page_state(
            "empty",
            "先打开一份研究报告",
            "研究详情只展示有完整冻结身份的报告。",
        )
        if st.button("返回研究台", key="research_detail_missing_back"):
            _go_to("研究台")
        return None
    # Frozen reports are immutable.  Once a verified report has been loaded in
    # this browser session, reuse it for button clicks, tabs, and the assistant
    # rail instead of reparsing the full payload on every Streamlit rerun.
    cached = cached_research_report(
        st.session_state,
        symbol=context.symbol,
        requested_as_of=context.research_requested_as_of,
    )
    if cached is not None and context_matches_research(context, cached):
        identity = research_identity(cached)
        if identity is not None and identity.effective_as_of == context.research_effective_as_of:
            return cached, identity

    path = settings.resolve(settings.get("system.database_path"))
    try:
        with st.spinner("正在验证并加载冻结研究..."):
            record = DecisionRepository(path).get(context.research_run_id)
            validated = validate_research_record(
                record,
                run_id=context.research_run_id,
                symbol=context.symbol,
            )
            if (
                validated["requested_as_of"].isoformat() != context.research_requested_as_of
                or validated["effective_as_of"].isoformat() != context.research_effective_as_of
            ):
                raise ValueError("research route does not match the frozen report identity")
            assert record is not None
            report = build_stored_audit_package(record)
            identity = cache_research_report(
                st.session_state,
                report,
                symbol=context.symbol,
                requested_as_of=context.research_requested_as_of,
            )
            bind_product_research_context(st.session_state, identity)
            return report, identity
    except Exception as exc:
        clear_product_research_context(st.session_state)
        render_page_state(
            "error",
            "这份研究暂时无法打开",
            _friendly_error(exc, "研究身份无法验证，系统已停止加载它。"),
        )
        if st.button("返回研究台", key="research_detail_invalid_back"):
            _go_to("研究台")
        return None


def render_research_detail(settings: Settings) -> None:
    loaded = _load_current_research_detail(settings)
    if loaded is None:
        return
    report, identity = loaded
    decision = report.get("decision", {})
    context = report.get("analysis_context_pack", {})
    data = report.get("data") or {}
    st.caption(f"{identity.symbol} · 数据截至 {identity.effective_as_of} · 冻结研究")
    metrics = st.columns(4)
    metrics[0].metric("研究结论", _research_action_label(decision.get("action")))
    metrics[1].metric("置信度", f"{float(decision.get('confidence', 0)):.0%}")
    metrics[2].metric("建议仓位上限", f"{float(decision.get('target_weight', 0)):.0%}")
    metrics[3].metric("证据质量", f"{float(context.get('quality_score', 0)):.0%}")
    evidence = decision.get("evidence", {})
    support, oppose = st.columns(2)
    with support:
        _render_research_items(
            "支持证据",
            evidence.get("supporting") or decision.get("supporting_evidence"),
            empty_copy="本次研究没有生成足够的可展示支持证据；结论应作为复核线索，而不是买卖理由。",
        )
    with oppose:
        _render_research_items(
            "反对证据",
            evidence.get("opposing") or decision.get("opposing_evidence"),
            empty_copy="本次研究没有生成可展示的反对证据；这不代表风险不存在，仍需结合数据状态复核。",
        )
    _render_research_items(
        "失效条件",
        decision.get("invalidation_conditions"),
        empty_copy="本次研究没有提供明确的失效条件；不应把它理解为低风险结论。",
        formatter=_research_condition_label,
    )
    degraded_sources = list(data.get("degraded_sources") or [])
    if not data.get("source") or int(data.get("bars") or 0) <= 0:
        data_status = "unavailable"
    elif degraded_sources:
        data_status = "degraded"
    else:
        data_status = "available"
    with st.expander("研究上下文、来源和数据质量"):
        _render_evidence_context(context)
        st.caption(
            f"数据状态 {_status_label(data_status)} · 来源 {data.get('source') or '暂未提供'} · "
            f"样本 {int(data.get('bars') or 0)} · 降级项 {len(degraded_sources)}"
        )
        st.caption(f"上下文指纹：{str(context.get('fingerprint') or '—')[:16]}…")
    with st.expander("审计原文（仅在需要核对时）"):
        raw_conditions = _research_items(decision.get("invalidation_conditions"))
        if raw_conditions:
            for item in raw_conditions:
                st.code(item, language=None)
        else:
            st.caption("这份报告没有可核对的失效条件原文。")
    st.caption(
        f"研究编号 {identity.run_id} · 请求截止日 {identity.requested_as_of} · "
        "AI 提供研究线索，模拟订单仍由你独立确认。"
    )
    actions = st.columns(3)
    if actions[0].button("前往组合与交易", type="primary", key="research_detail_to_simulator"):
        _go_to(
            "组合与交易",
            symbol=identity.symbol,
            research_run_id=identity.run_id,
            research_requested_as_of=identity.requested_as_of,
            research_effective_as_of=identity.effective_as_of,
        )
    if actions[1].button(
        "进入专家圆桌",
        icon=":material/groups:",
        key=f"research_detail_roundtable_{identity.run_id}",
    ):
        _go_to(
            "专家圆桌",
            symbol=identity.symbol,
            research_run_id=identity.run_id,
            research_requested_as_of=identity.requested_as_of,
            research_effective_as_of=identity.effective_as_of,
        )
    if actions[2].button("返回研究台", icon=":material/arrow_back:", key="research_detail_back"):
        _go_to("研究台", symbol=identity.symbol)


_ROUNDTABLE_STATUS_COPY = {
    "queued": "等待后台任务",
    "running": "讨论进行中",
    "completed": "讨论已完成",
    "degraded": "讨论已完成（部分输出降级）",
    "failed": "讨论未完成",
    "cancelled": "讨论已停止",
}


def _roundtable_status_copy(value: Any) -> str:
    return _ROUNDTABLE_STATUS_COPY.get(str(value or "").lower(), "状态待确认")


def _roundtable_stage_html(
    *,
    participants: list[str],
    catalog: dict[str, dict[str, str]],
    turns: list[dict[str, Any]],
    status: str,
) -> str:
    latest_by_participant: dict[str, dict[str, Any]] = {}
    for turn in turns:
        participant = str(turn.get("participant") or "")
        if participant:
            latest_by_participant[participant] = turn
    seats = []
    for index, key in enumerate(participants[:8]):
        item = catalog.get(key, {"label": key, "perspective": "研究视角"})
        turn = latest_by_participant.get(key, {})
        latest = _research_user_copy(turn.get("statement") or "正在等待发言…")
        latest = latest[:92] + ("…" if len(latest) > 92 else "")
        seats.append(
            "<article class=\"ql-roundtable-seat ql-roundtable-seat-"
            f"{index}\" aria-label=\"{escape(str(item['label']))} 的席位\">"
            f"<div class=\"ql-chibi ql-chibi-{index % 6}\"><i></i><b></b><span></span></div>"
            "<div class=\"ql-seat-copy\">"
            f"<strong>{escape(str(item['label']))}</strong>"
            f"<small>{escape(str(item.get('perspective') or '研究视角'))}</small>"
            f"<p>{escape(latest)}</p>"
            "</div></article>"
        )
    status_copy = _roundtable_status_copy(status)
    return (
        "<section class=\"ql-roundtable-stage\">"
        f"<div class=\"ql-roundtable-status\"><span></span>{escape(status_copy)}</div>"
        "<div class=\"ql-roundtable-table\"><i></i><b>ROUND TABLE</b><small>冻结研究 · 多视角复核</small></div>"
        f"{''.join(seats)}"
        "</section>"
    )


def _render_roundtable_transcript(session: dict[str, Any], catalog: dict[str, dict[str, str]]) -> None:
    turns = list(session.get("turns") or [])
    if not turns:
        st.caption("专家入席后，逐轮发言会在这里持续出现。")
        return
    available_rounds = sorted({int(item.get("round_number") or 1) for item in turns})
    selected_round = st.segmented_control(
        "查看轮次",
        options=available_rounds,
        default=available_rounds[-1],
        format_func=lambda value: f"第 {value} 轮",
        key=f"roundtable_transcript_round_{session['session_id']}",
    )
    target_round = int(selected_round or available_rounds[-1])
    for turn in [item for item in turns if int(item.get("round_number") or 1) == target_round]:
        participant = str(turn.get("participant") or "")
        label = str(turn.get("participant_label") or catalog.get(participant, {}).get("label") or participant)
        stance = {
            "bullish": "偏支持",
            "bearish": "偏谨慎",
            "neutral": "中性",
            "mixed": "存在分歧",
        }.get(str(turn.get("stance") or ""), "等待复核")
        with st.container(border=True):
            title, confidence = st.columns([3, 1])
            title.markdown(f"**{label}** · {stance}")
            confidence.caption(f"把握 {float(turn.get('confidence') or 0):.0%}")
            st.write(_research_user_copy(turn.get("statement") or "本轮没有可展示的发言。"))
            challenges = _research_items(turn.get("challenges"))
            gaps = _research_items(turn.get("evidence_gaps"))
            if challenges or gaps:
                with st.expander("本轮质疑与待补证据"):
                    for item in challenges:
                        st.write(f"质疑：{_research_user_copy(item)}")
                    for item in gaps:
                        st.write(f"证据缺口：{_research_user_copy(item)}")


def _render_roundtable_summary(session: dict[str, Any]) -> None:
    synthesis = session.get("synthesis") if isinstance(session.get("synthesis"), dict) else {}
    if not synthesis:
        st.caption("主持人会在所有专家发言结束后生成圆桌总结。")
        return
    st.markdown("#### 圆桌总结")
    st.write(_research_user_copy(synthesis.get("summary") or "圆桌没有生成可展示的总结。"))
    for label, key in (("仍未解决的分歧", "unresolved_disagreements"), ("待补证据", "evidence_gaps"), ("下一步", "recommended_next_steps")):
        items = _research_items(synthesis.get(key))
        if items:
            st.markdown(f"**{label}**")
            for item in items:
                st.write(f"• {_research_user_copy(item)}")


def _render_roundtable_session(settings: Settings, session_id: str) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    repository = RoundtableRepository(path)
    session = repository.get(session_id)
    if session is None:
        render_page_state("error", "圆桌记录无法读取", "这场讨论可能已被清理，未修改原始研究。")
        return
    catalog = {item["key"]: item for item in roundtable_participant_catalog()}
    st.markdown(
        _roundtable_stage_html(
            participants=list(session.get("participants") or []),
            catalog=catalog,
            turns=list(session.get("turns") or []),
            status=str(session.get("status") or "queued"),
        ),
        unsafe_allow_html=True,
    )
    st.progress(float(session.get("progress") or 0.0), text=str(session.get("progress_message") or "正在准备讨论"))
    job_id = session.get("job_id")
    job = JobRepository(path).job(str(job_id)) if job_id else None
    if job and job.get("status") in {"queued", "running"}:
        controls = st.columns([1, 3])
        if controls[0].button("停止讨论", key=f"cancel_roundtable_{session_id}"):
            JobRepository(path).cancel(str(job_id), "cancelled_from_roundtable_workspace")
            repository.record_progress(
                session_id,
                status="cancelled",
                progress=float(session.get("progress") or 0),
                message="已请求停止本次讨论",
            )
            st.rerun()
        controls[1].caption("讨论在后台继续；切换页面或刷新浏览器后仍可回到这场圆桌。")
    transcript_tab, summary_tab = st.tabs(["讨论过程", "主持人总结"])
    with transcript_tab:
        _render_roundtable_transcript(session, catalog)
    with summary_tab:
        _render_roundtable_summary(session)


def render_roundtable(settings: Settings) -> None:
    loaded = _load_current_research_detail(settings)
    if loaded is None:
        return
    _, identity = loaded
    path = settings.resolve(settings.get("system.database_path"))
    repository = RoundtableRepository(path)
    catalog_items = roundtable_participant_catalog()
    catalog = {item["key"]: item for item in catalog_items}
    sessions = repository.sessions_for_source(identity.run_id, limit=20)
    selected_session_id = st.session_state.get("product_roundtable_session_id")
    known_session_ids = [str(item["session_id"]) for item in sessions]
    if selected_session_id not in known_session_ids:
        selected_session_id = known_session_ids[0] if known_session_ids else None
        st.session_state["product_roundtable_session_id"] = selected_session_id
    if sessions:
        selected_session_id = st.selectbox(
            "本报告的圆桌记录",
            known_session_ids,
            index=known_session_ids.index(selected_session_id) if selected_session_id else 0,
            format_func=lambda value: next(
                f"{_roundtable_status_copy(item['status'])} · {str(item['created_at'])[:16]} · {item['topic'][:24]}"
                for item in sessions
                if item["session_id"] == value
            ),
            key="product_roundtable_session_id",
        )
    active_session = repository.get(str(selected_session_id)) if selected_session_id else None
    defaults = list(active_session.get("participants") or []) if active_session else ["bull", "bear", "risk"]
    defaults = [item for item in defaults if item in catalog]
    with st.expander("发起新的圆桌讨论", expanded=active_session is None):
        participants = st.multiselect(
            "邀请专家",
            options=list(catalog),
            default=defaults,
            format_func=lambda key: catalog[key]["label"],
            key=f"roundtable_participants_{identity.run_id}",
        )
        topic = st.text_area(
            "本次重点复核的问题",
            value="这份研究最可能错在哪里？还有哪些关键证据需要补齐？",
            max_chars=500,
            key=f"roundtable_topic_{identity.run_id}",
        )
        rounds = st.select_slider(
            "讨论轮数",
            options=[1, 2, 3],
            value=2,
            key=f"roundtable_rounds_{identity.run_id}",
        )
        st.markdown(
            _roundtable_stage_html(
                participants=participants,
                catalog=catalog,
                turns=[],
                status="queued",
            ),
            unsafe_allow_html=True,
        )
        st.caption("圆桌只围绕当前冻结报告讨论，不会改写研究结论、仓位或模拟订单。")
        if st.button(
            "开始专家圆桌",
            type="primary",
            key=f"submit_roundtable_{identity.run_id}",
            disabled=len(participants) < 2 or not str(topic).strip(),
        ):
            try:
                _validated_roundtable_source(path, identity)
                submitted = submit_roundtable_job(
                    settings,
                    source_run_id=identity.run_id,
                    symbol=identity.symbol,
                    as_of=identity.effective_as_of,
                    participants=participants,
                    topic=str(topic).strip(),
                    rounds=int(rounds),
                )
                st.session_state["product_roundtable_session_id"] = submitted["session"]["session_id"]
                _queue_product_feedback("圆桌已提交到后台，页面会持续显示逐轮发言。")
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "圆桌暂时无法启动；当前研究报告没有被修改。"))
    session_id = st.session_state.get("product_roundtable_session_id")
    if session_id:
        current = repository.get(str(session_id))
        if current and str(current.get("status")) in {"queued", "running"}:
            @st.fragment(run_every=2)
            def live_roundtable() -> None:
                _render_roundtable_session(settings, str(session_id))

            live_roundtable()
        else:
            _render_roundtable_session(settings, str(session_id))
    else:
        render_page_state(
            "empty",
            "还没有圆桌讨论",
            "选择两位或更多专家后开始；每一轮发言都会保存，可随时回来查看。",
        )
    if st.button("返回研究详情", icon=":material/arrow_back:", key="roundtable_back_research"):
        _go_to(
            "研究详情",
            symbol=identity.symbol,
            research_run_id=identity.run_id,
            research_requested_as_of=identity.requested_as_of,
            research_effective_as_of=identity.effective_as_of,
        )


def render_simulator(settings: Settings) -> None:
    st.info(
        "这是用户模拟账户。所有订单只写入隔离模拟账本，不会发送到券商；AI 交易前检查用于提示与规则核对，不是审批，最终是否创建委托由你决定。"
    )
    navigation_context = consume_product_context(st.session_state, "simulator")
    if navigation_context is not None:
        if navigation_context.symbol:
            st.session_state["product_order_symbol"] = navigation_context.symbol
        if navigation_context.account_id:
            st.session_state["product_account_select"] = navigation_context.account_id
        if navigation_context.order_id:
            st.session_state["product_order_detail"] = navigation_context.order_id
    context = product_context(st.session_state)
    repository = user_simulator_repository(settings)
    try:
        accounts = repository.accounts(include_closed=False)
    except Exception as exc:
        render_page_state(
            "error",
            "模拟账户读取失败",
            _friendly_error(exc, "账户账本暂时无法读取，未创建或修改任何订单。"),
        )
        return
    if not accounts:
        name = st.text_input("账户名称", value="我的模拟账户", key="product_account_name")
        capital = st.number_input(
            "初始资金", min_value=1_000.0, value=100_000.0, step=10_000.0, key="product_capital"
        )
        if st.button("创建模拟账户", type="primary", key="product_create_account"):
            account = create_user_paper_account(
                settings,
                name=name,
                initial_capital=capital,
                idempotency_key=f"product-account:{uuid.uuid4()}",
            )
            record_product_usage(
                settings,
                event_type="simulator_account_created",
                entrypoint="组合与交易",
                account_id=account["account_id"],
            )
            st.rerun()
        return
    account_ids = [item["account_id"] for item in accounts]
    if st.session_state.get("product_account_select") not in account_ids:
        st.session_state["product_account_select"] = (
            context.account_id if context.account_id in account_ids else account_ids[0]
        )
    account_id = st.selectbox(
        "模拟账户",
        account_ids,
        format_func=lambda value: next(
            item["name"] for item in accounts if item["account_id"] == value
        ),
        key="product_account_select",
    )
    if context.account_id not in {None, account_id}:
        st.session_state.pop("product_context_order_id", None)
        st.session_state.pop("product_order_detail", None)
        st.session_state.pop("product_pretrade_check", None)
    st.session_state["product_context_account_id"] = account_id
    context = product_context(st.session_state)
    overview = repository.overview(account_id)
    _render_metric_grid(
        [
            ("总资产", f"¥{overview['equity']:,.2f}"),
            ("可用现金", f"¥{overview['available_cash']:,.2f}"),
            ("冻结现金", f"¥{overview.get('frozen_cash', 0):,.2f}"),
            ("今日盈亏", f"¥{overview['today_pnl']:,.2f}"),
            ("已实现盈亏", f"¥{overview['realized_pnl']:,.2f}"),
            ("累计费用", f"¥{overview['cumulative_fees']:,.2f}"),
        ]
    )
    if st.button("用服务器最新行情更新账户", key="product_mark_account"):
        try:
            mark_user_paper_account(settings, account_id=account_id)
            st.success("账户行情、持仓盈亏和净值已更新。")
            st.rerun()
        except Exception as exc:
            st.error(_friendly_error(exc, "当前无法完成盯市，已保留上次有效价格。"))
    if overview["positions"]:
        st.subheader("持仓")
        position_rows = [
            {
                "标的": item["symbol"],
                "数量": item["quantity"],
                "可卖": item["sellable_quantity"],
                "冻结": int(item.get("frozen_quantity") or 0)
                + int(item.get("reserved_sell_quantity") or 0),
                "平均成本": _money(item["average_cost"]),
                "最新价": _money(item["latest_price"]),
                "市值": _money(item["market_value"]),
                "未实现盈亏": _money(item["unrealized_pnl"]),
                "收益率": _percent(item["return"]),
                "仓位": _percent(item["weight"]),
            }
            for item in overview["positions"]
        ]
        st.dataframe(position_rows, hide_index=True, width="stretch")

    st.subheader("买入、加仓、减仓或清仓")
    actions = ["买入新标的", "加仓", "减仓", "清仓"]
    action = st.selectbox("操作", actions, key="product_order_action")
    previous_action = st.session_state.get("product_order_action_context")
    if previous_action not in {None, action}:
        st.session_state.pop("product_pretrade_check", None)
    st.session_state["product_order_action_context"] = action
    position_map = {item["symbol"]: item for item in overview["positions"]}
    if action == "买入新标的":
        st.session_state.setdefault(
            "product_order_symbol",
            context.symbol or "sh510300",
        )
        symbol = st.text_input("标的", key="product_order_symbol").strip()
        side = "buy"
        default_quantity = 100
    elif not position_map:
        st.info("当前没有持仓，请先选择“买入新标的”。")
        return
    else:
        symbol = st.selectbox("选择持仓", list(position_map), key="product_position_action")
        side = "buy" if action == "加仓" else "sell"
        default_quantity = (
            int(position_map[symbol]["sellable_quantity"]) if action == "清仓" else 100
        )
    research_invalidated = update_product_selection(
        st.session_state,
        symbol=symbol,
    )
    context = product_context(st.session_state)
    context_report = (
        cached_research_report(
            st.session_state,
            symbol=context.symbol,
            requested_as_of=context.research_requested_as_of,
        )
        if context.symbol and context.research_requested_as_of
        else None
    )
    linked_identity = research_identity(context_report)
    research_run_id = (
        context.research_run_id if context_matches_research(context, context_report) else None
    )
    if context.research_run_id and research_run_id is None:
        clear_product_research_context(st.session_state)
        linked_identity = None
        research_invalidated = True
    if research_invalidated:
        st.warning("研究报告与当前标的或日期不一致，关联已清除；不会静默替换成其他研究。")
    if research_run_id and linked_identity:
        st.info(
            f"本次操作关联研究：{linked_identity.symbol} · 请求截止日 "
            f"{linked_identity.requested_as_of} · 有效数据日 "
            f"{linked_identity.effective_as_of} · run_id `{linked_identity.run_id}`"
        )
    else:
        st.caption("研究关联：未关联研究；本次仅运行确定性的交易与风控检查。")
    quantity = int(
        st.number_input(
            "数量",
            min_value=1,
            value=max(1, default_quantity),
            step=100,
            disabled=action == "清仓",
            key=f"product_order_quantity_{action}_{symbol}",
        )
    )
    if st.button("运行 AI 交易前检查", type="primary", key="product_pretrade"):
        try:
            check = run_pretrade_check(
                settings,
                account_id=account_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                research_run_id=research_run_id,
            )
            st.session_state["product_pretrade_check"] = check
            record_product_usage(
                settings,
                event_type="pretrade_check_entered",
                entrypoint="组合与交易",
                account_id=account_id,
                symbol=symbol,
                reference_id=check["check_id"],
            )
        except Exception as exc:
            st.error(_friendly_error(exc, "交易前检查未完成，请检查行情、交易日和账户约束。"))
    check = st.session_state.get("product_pretrade_check")
    if check and check.get("account_id") == account_id:
        request_matches = _pretrade_request_matches(
            check,
            account_id=account_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            research_run_id=research_run_id,
            research_link_status="linked" if research_run_id else "unlinked",
        )
        st.subheader("交易前检查")
        st.caption(
            "依次核对 AI 观点、你的模拟委托和行情结算方式。AI 只提供研究意见，"
            "不会替你否决模拟交易；账户、行情和交易规则仍会如实检查。"
        )
        _render_metric_grid(
            [
                ("AI 观点", _research_action_label(check["suggested_action"])),
                ("你的委托", "买入" if side == "buy" else "卖出"),
                ("AI 建议数量", int(check["suggested_quantity"])),
                ("操作后现金", _money(check["post_trade_cash"])),
                ("操作后仓位", _percent(check["post_trade_single_weight"])),
                ("下跌10%影响", _money(check["loss_if_symbol_down_10pct"])),
            ]
        )
        st.caption(
            f"参考价格 {_money(check['reference_price'])} · 行情时间 {_time(check['reference_time'])} · "
            f"预计费用 {_money(check['estimated_transaction_fees'])} · "
            f"行情类型 {_quote_kind_label(check['quote'].get('quote_kind'))}"
        )
        if check.get("research_run_id"):
            st.caption(f"交易前检查研究关联：run_id `{check['research_run_id']}`")
        else:
            st.caption("交易前检查研究关联：未关联研究")
        if check["supporting_evidence"]:
            st.markdown(
                "**支持证据：** "
                + "；".join(_research_user_copy(item) for item in check["supporting_evidence"][:5])
            )
        if check["opposing_evidence"]:
            st.markdown(
                "**反对证据：** "
                + "；".join(_research_user_copy(item) for item in check["opposing_evidence"][:5])
            )
        if check["invalidation_conditions"]:
            st.markdown(
                "**失效条件：** "
                + "；".join(
                    _research_condition_label(item) for item in check["invalidation_conditions"][:5]
                )
            )
        for failure in check["hard_failures"]:
            st.error(f"不能创建委托：{_pretrade_reason_label(failure)}")
        for warning in check["warnings"]:
            st.warning(f"需要复核：{_pretrade_reason_label(warning)}")
        if not request_matches:
            st.warning("账户、标的、方向、数量或研究关联已变化，请重新运行交易前检查。")
        try:
            checked_quote = MarketQuote.model_validate(check.get("quote") or {})
            permitted_simulation_modes = available_user_paper_simulation_modes(checked_quote)
        except Exception:
            checked_quote = None
            permitted_simulation_modes = ()
        quote_actionable = INTRADAY_SIMULATION in permitted_simulation_modes
        ai_disagrees_with_request = (
            (side == "buy" and check["suggested_action"] not in {"buy", "add"})
            or (side == "sell" and check["suggested_action"] not in {"sell", "reduce"})
        )
        if ai_disagrees_with_request:
            st.info("AI 不建议这一方向，但这不会替你否决模拟交易；上面的确定性检查通过后，你仍可按自己的委托创建订单。")
        simulation_mode = (
            INTRADAY_SIMULATION
            if quote_actionable
            else NEXT_OPEN_SIMULATION
            if NEXT_OPEN_SIMULATION in permitted_simulation_modes
            else None
        )
        close_reference_acknowledged = False
        if simulation_mode == NEXT_OPEN_SIMULATION:
            st.info("当前只有权威收盘参考价。创建后会成为下一交易日的待成交模拟委托，不会按这条价格即时成交。")
            close_reference_acknowledged = st.checkbox(
                "我明白收盘价只用于参考，订单将在下一交易日等待可用行情结算",
                key=f"product_close_reference_ack_{check['check_id']}",
            )
        elif simulation_mode == INTRADAY_SIMULATION:
            st.caption("当前使用可执行行情，确认后会创建盘中模拟委托；成交仍以后端订单生命周期为准。")
        else:
            st.error("当前行情既不是可执行的实时行情，也不是可用于下一交易日模拟的权威收盘参考价，不能创建委托。")
        can_confirm = bool(
            check["allowed_to_submit"]
            and request_matches
            and simulation_mode
            and (simulation_mode != NEXT_OPEN_SIMULATION or close_reference_acknowledged)
        )
        if not check["allowed_to_submit"]:
            st.button("当前规则未通过，无法创建委托", key="product_confirm_order_blocked", disabled=True)
        elif not request_matches:
            st.button("请重新运行交易前检查", key="product_confirm_order_refresh", disabled=True)
        elif simulation_mode == NEXT_OPEN_SIMULATION and not close_reference_acknowledged:
            st.button(
                "确认创建下一交易日模拟委托",
                key="product_confirm_order_ack",
                disabled=True,
                help="先确认上方的收盘价说明，订单才会在下一交易日等待可用行情结算。",
            )
            st.caption("勾选收盘价说明后，即可确认创建下一交易日模拟委托。")
        elif simulation_mode is None:
            st.button("当前行情无法创建委托", key="product_confirm_order_quote", disabled=True)
        elif can_confirm and st.button(
            "确认创建盘中模拟委托"
            if simulation_mode == INTRADAY_SIMULATION
            else "确认创建下一交易日模拟委托",
            key="product_confirm_order",
            type="primary",
        ):
            try:
                order = submit_user_paper_order(
                    settings,
                    check_id=check["check_id"],
                    quantity=quantity,
                    idempotency_key=f"product-order:{check['check_id']}:{quantity}",
                    user_confirmation={
                        "confirmed": True,
                        "check_id": check["check_id"],
                        "account_id": account_id,
                        "symbol": symbol,
                        "side": side,
                        "quantity": quantity,
                        "source": "five_entry_ui_user_directed",
                        "simulation_mode": simulation_mode,
                        "close_reference_acknowledged": close_reference_acknowledged,
                    },
                )
                st.success(f"模拟订单已创建：{_status_label(order['status'])}")
                record_product_usage(
                    settings,
                    event_type="simulator_order_confirmed",
                    entrypoint="组合与交易",
                    account_id=account_id,
                    symbol=symbol,
                    reference_id=order["order_id"],
                )
                st.session_state["product_context_order_id"] = order["order_id"]
                st.session_state.pop("product_pretrade_check", None)
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "订单确认失败；行情变化时必须重新检查。"))

    st.subheader("委托生命周期")
    orders = repository.orders(account_id, limit=100)
    if orders:
        st.dataframe(
            [
                {
                    "订单": item["order_id"][:8],
                    "标的": item["symbol"],
                    "方向": "买入" if item["side"] == "buy" else "卖出",
                    "委托数量": item["requested_quantity"],
                    "已成交": item["filled_quantity"],
                    "剩余": item["remaining_quantity"],
                    "状态": _status_label(item["status"]),
                    "研究": _research_link_label(item.get("research_run_id")),
                    "可成交日": item["eligible_trade_date"],
                    "提交时间": _time(item["requested_at"]),
                }
                for item in orders
            ],
            hide_index=True,
            width="stretch",
        )
        order_ids = [item["order_id"] for item in orders]
        if st.session_state.get("product_order_detail") not in order_ids:
            st.session_state["product_order_detail"] = (
                context.order_id if context.order_id in order_ids else order_ids[0]
            )
        selected_order_id = st.selectbox(
            "查看订单详情",
            order_ids,
            format_func=lambda value: next(
                f"{item['symbol']} · {_status_label(item['status'])} · {value[:8]}"
                for item in orders
                if item["order_id"] == value
            ),
            key="product_order_detail",
        )
        st.session_state["product_context_order_id"] = selected_order_id
        selected_order = next(item for item in orders if item["order_id"] == selected_order_id)
        detail_cols = st.columns(4)
        detail_cols[0].metric(
            "状态", _status_label(selected_order["status"])
        )
        detail_cols[1].metric("参考价格", _money(selected_order["reference_price"]))
        detail_cols[2].metric("冻结现金", _money(selected_order.get("reserved_cash")))
        detail_cols[3].metric("冻结数量", int(selected_order.get("reserved_quantity") or 0))
        if selected_order.get("research_run_id"):
            st.caption(
                f"订单研究关联：{selected_order['symbol']} · run_id "
                f"`{selected_order['research_run_id']}`"
            )
        else:
            st.caption("订单研究关联：未关联研究")
        if selected_order.get("rejection_reason"):
            st.warning(f"原因：{selected_order['rejection_reason']}")
        events = repository.order_events(selected_order_id)
        if events:
            st.markdown("**订单事件时间线**")
            st.dataframe(
                [
                    {
                        "时间": _time(item["created_at"]),
                        "事件": _status_label(item["event_type"]),
                        "研究": _research_link_label(item.get("research_run_id")),
                        "说明": item.get("detail") or "—",
                    }
                    for item in events
                ],
                hide_index=True,
                width="stretch",
            )
        order_fills = [
            item for item in repository.fills(account_id) if item["order_id"] == selected_order_id
        ]
        if order_fills:
            st.markdown("**成交明细**")
            st.dataframe(
                [
                    {
                        "成交日": item["trade_date"],
                        "数量": item["quantity"],
                        "价格": _money(item["fill_price"]),
                        "研究": _research_link_label(item.get("research_run_id")),
                        "佣金": _money(item["commission"]),
                        "税费": _money(float(item["stamp_duty"]) + float(item["transfer_fee"])),
                        "滑点": _money(item["slippage"]),
                    }
                    for item in order_fills
                ],
                hide_index=True,
                width="stretch",
            )
        if selected_order["status"] in {"pending", "partially_filled"}:
            action_cols = st.columns(2)
            if action_cols[0].button("用服务器行情尝试成交", key=f"settle_{selected_order_id}"):
                try:
                    result = settle_user_paper_order(
                        settings,
                        order_id=selected_order_id,
                        fill_key=f"five-entry:{selected_order_id}:{date.today().isoformat()}",
                    )
                    st.success(
                        f"订单状态：{_status_label(result['order']['status'])}"
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(_friendly_error(exc, "当前不能成交，委托会继续保留或明确过期。"))
            if action_cols[1].button("撤销未完成委托", key=f"cancel_{selected_order_id}"):
                try:
                    cancel_user_paper_order(settings, selected_order_id)
                    st.success("委托已撤销，冻结资源已释放。")
                    st.rerun()
                except Exception as exc:
                    st.error(_friendly_error(exc, "该订单当前不能撤销。"))
    else:
        st.caption("暂无委托。系统会完整保留待处理、部分成交、已成交、已撤销、已拒绝和已过期等状态。")
    fills = repository.fills(account_id)
    if fills:
        st.subheader("全部成交与费用")
        st.dataframe(
            [
                {
                    "成交日": item["trade_date"],
                    "标的": item["symbol"],
                    "方向": "买入" if item["side"] == "buy" else "卖出",
                    "数量": item["quantity"],
                    "价格": _money(item["fill_price"]),
                    "研究": _research_link_label(item.get("research_run_id")),
                    "总费用": _money(
                        float(item["commission"])
                        + float(item["stamp_duty"])
                        + float(item["transfer_fee"])
                    ),
                }
                for item in fills
            ],
            hide_index=True,
            width="stretch",
        )
    curve = repository.snapshots(account_id)
    if curve:
        st.subheader("净值曲线")
        curve_frame = pd.DataFrame(curve).set_index("snapshot_date")
        chart_columns = [
            column for column in ("equity", "benchmark_equity") if column in curve_frame
        ]
        st.line_chart(curve_frame[chart_columns])
    with st.expander("单笔交易复盘"):
        review_order = st.selectbox(
            "关联订单",
            [""] + [item["order_id"] for item in orders],
            format_func=lambda value: "不关联具体订单" if not value else value[:8],
            key="product_review_order",
        )
        review_note = st.text_area("复盘记录", key="product_review_note")
        if st.button("保存复盘", key="product_save_review"):
            repository.record_review(
                account_id,
                order_id=review_order or None,
                symbol=next(
                    (item["symbol"] for item in orders if item["order_id"] == review_order), None
                ),
                review_type="user_trade_review",
                payload={"note": review_note, "source": "vnext_product_ui"},
            )
            st.success("复盘已保存，并与当时订单和研究上下文保持关联。")
        reviews = repository.reviews(account_id)
        if reviews:
            st.dataframe(
                [
                    {
                        "时间": _time(item["created_at"]),
                        "标的": item.get("symbol") or "—",
                        "订单": str(item.get("order_id") or "—")[:8],
                        "记录": item["payload"].get("note") or "—",
                    }
                    for item in reviews
                ],
                hide_index=True,
                width="stretch",
            )


def render_review(settings: Settings) -> None:
    """Read-only decision memory assembled from authoritative product ledgers."""

    path = settings.resolve(settings.get("system.database_path"))
    simulator = user_simulator_repository(settings)
    lifecycle = Round8Repository(path)
    try:
        accounts = simulator.accounts(include_closed=True)
        theses = lifecycle.theses()
    except Exception as exc:
        render_page_state(
            "error",
            "复盘账本暂时无法读取",
            _friendly_error(exc, "系统未修改任何历史记录，请稍后重试。"),
        )
        return

    active = [
        item
        for item in theses
        if item.get("lifecycle_status")
        in {
            "active",
            "strengthened",
            "unchanged",
            "weakened",
            "damaged",
            "broken",
            "draft_pending_confirmation",
        }
    ]
    review_needed = sum(
        item.get("lifecycle_status") in {"weakened", "damaged", "broken"}
        or any(
            assumption.get("status") == "needs_review"
            for assumption in item.get("assumptions") or []
        )
        for item in active
    )
    metrics = st.columns(3)
    metrics[0].metric("投资论文", len(active))
    metrics[1].metric("需要复核", review_needed)
    metrics[2].metric("模拟账户", len(accounts))

    st.subheader("论文状态与失效条件")
    if active:
        st.dataframe(
            [
                {
                    "标的": item.get("symbol") or "—",
                    "状态": _thesis_status_label(item.get("lifecycle_status")),
                    "核心判断": item.get("core_thesis") or "—",
                    "红线": len(item.get("red_lines") or []),
                    "待复核假设": sum(
                        assumption.get("status") == "needs_review"
                        for assumption in item.get("assumptions") or []
                    ),
                    "下一次检查": item.get("next_check_at") or "尚未安排",
                }
                for item in active
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        render_page_state(
            "empty",
            "还没有可复盘的投资论文",
            "先在研究台生成报告，再由你确认是否采纳。草稿不会自动变成生效论文。",
        )

    st.subheader("模拟决策时间线")
    if not accounts:
        render_page_state(
            "empty",
            "还没有模拟交易记录",
            "创建模拟账户并确认第一笔订单后，这里会把研究身份、委托、成交、费用和复盘记录串联展示。",
        )
        if st.button("前往组合与交易", key="review_open_simulator", type="primary"):
            _go_to("组合与交易")
        return

    account_ids = [item["account_id"] for item in accounts]
    account_id = st.selectbox(
        "复盘账户",
        account_ids,
        format_func=lambda value: next(
            item["name"] for item in accounts if item["account_id"] == value
        ),
        key="product_review_account",
    )
    orders = simulator.orders(account_id, limit=200)
    fills = simulator.fills(account_id)
    reviews = simulator.reviews(account_id, limit=200)
    overview = simulator.overview(account_id)
    outcome = _review_outcome_summary(overview, orders, fills, reviews)
    account_metrics = st.columns(4)
    account_metrics[0].metric("总资产", _money(overview.get("equity")))
    account_metrics[1].metric("累计收益", _percent(overview.get("total_return")))
    account_metrics[2].metric("累计费用", _money(overview.get("cumulative_fees")))
    account_metrics[3].metric("已完成成交", len(fills))
    _render_review_outcome(outcome)

    curve = simulator.snapshots(account_id)
    if curve:
        st.subheader("账户结果曲线")
        curve_frame = pd.DataFrame(curve).set_index("snapshot_date")
        chart_columns = [
            column for column in ("equity", "benchmark_equity") if column in curve_frame
        ]
        st.line_chart(curve_frame[chart_columns])
        st.caption("曲线来自已保存的模拟账户快照；基准仅在同日数据可用时显示。")
    else:
        st.caption("尚无已保存的净值快照；系统不会用成交价或演示数据拼接结果曲线。")

    if orders:
        st.dataframe(
            [
                {
                    "提交时间": _time(item.get("requested_at")),
                    "标的": item.get("symbol") or "—",
                    "方向": "买入" if item.get("side") == "buy" else "卖出",
                    "状态": _status_label(item.get("status")),
                    "委托/成交": f"{item.get('requested_quantity', 0)} / {item.get('filled_quantity', 0)}",
                    "研究身份": _research_link_label(item.get("research_run_id")),
                    "拒绝原因": item.get("rejection_reason") or "—",
                }
                for item in orders
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("该账户还没有委托。空历史是正常状态，不会用演示订单填充。")

    if reviews:
        with st.expander("用户复盘记录", expanded=True):
            for item in reviews[:3]:
                with st.container(border=True):
                    st.caption(
                        f"{_time(item.get('created_at'))} · "
                        f"{item.get('symbol') or '未关联标的'} · "
                        f"订单 {str(item.get('order_id') or '未关联')[:12]}"
                    )
                    st.write((item.get("payload") or {}).get("note") or "未填写复盘内容。")
            st.dataframe(
                [
                    {
                        "记录时间": _time(item.get("created_at")),
                        "标的": item.get("symbol") or "—",
                        "订单": str(item.get("order_id") or "—")[:12],
                        "类型": item.get("review_type") or "—",
                        "记录": (item.get("payload") or {}).get("note") or "—",
                    }
                    for item in reviews
                ],
                hide_index=True,
                width="stretch",
            )
    st.caption(
        "复盘页只读取当前用户模拟账本与论文生命周期；正式影子、历史演示和外部记录不会混入该时间线。"
    )


HELP_DOCUMENTS = (
    (
        "quick_start",
        "01 · 开始",
        "快速开始",
        "从发现线索到完成一次可追溯的模拟决策。",
    ),
    (
        "research_roundtable",
        "02 · 研究",
        "研究与圆桌",
        "读懂 AI 结论、证据和多角色交叉质疑。",
    ),
    (
        "simulation",
        "03 · 模拟",
        "模拟交易",
        "理解 AI 检查、人工确认和订单状态。",
    ),
    (
        "review_notifications",
        "04 · 复盘",
        "复盘与通知",
        "把研究、订单、成交和后续结果重新连起来。",
    ),
    (
        "assistant_settings",
        "05 · 助手",
        "AI 助手与设置",
        "继续追问当前研究，并管理本机偏好。",
    ),
    (
        "status_faq",
        "06 · 状态",
        "状态与常见问题",
        "看懂数据状态、限制条件和系统边界。",
    ),
)


def render_help_center(settings: Settings) -> None:
    del settings  # Help content is deliberately independent from runtime availability.
    documents_by_key = {document[0]: document for document in HELP_DOCUMENTS}
    legacy_targets = {document[2]: document[0] for document in HELP_DOCUMENTS}
    legacy_targets["常见问题"] = "status_faq"
    requested_document = st.session_state.pop("product_help_document_target", None)
    selected = str(st.session_state.get("product_help_document") or "").strip()
    if requested_document:
        selected = legacy_targets.get(str(requested_document), str(requested_document))
    selected = legacy_targets.get(selected, selected)
    if selected not in documents_by_key:
        selected = "quick_start"
    st.session_state["product_help_document"] = selected

    st.markdown(
        '<div class="ql-section-title"><span>使用文档</span><strong>选择一篇，打开完整说明</strong></div>',
        unsafe_allow_html=True,
    )
    for start in range(0, len(HELP_DOCUMENTS), 3):
        for column, document in zip(
            st.columns(3), HELP_DOCUMENTS[start : start + 3], strict=True
        ):
            key, section, title, summary = document
            with column:
                st.markdown(
                    (
                        '<article class="ql-help-card">'
                        f"<span>{section}</span><strong>{title}</strong><p>{summary}</p>"
                        "</article>"
                    ),
                    unsafe_allow_html=True,
                )
                if st.button(
                    "正在阅读" if key == selected else "打开文档",
                    key=f"help_open_document_{key}",
                    disabled=key == selected,
                    width="stretch",
                ):
                    st.session_state["product_help_document"] = key
                    st.rerun()

    _, section, title, _ = documents_by_key[selected]
    st.markdown(
        f'<div class="ql-section-title"><span>{section}</span><strong>{title}</strong></div>',
        unsafe_allow_html=True,
    )

    if selected == "quick_start":
        st.markdown(
            """
            1. 在「市场与发现」搜索股票或 ETF，先确认行情日期、来源和数据状态。
            2. 打开「研究台」生成或继续一份研究。系统会把本次研究的时间、标的和证据身份固定下来。
            3. 先阅读支持证据、反对证据和失效条件；有分歧时，从该研究报告进入专家圆桌继续追问。
            4. 在「组合与交易」创建模拟账户，填写你自己的方向和数量，再运行交易前检查。
            5. 你确认模拟委托后，到「决策复盘」回看订单、成交、费用和当时的研究依据。
            """
        )
        actions = st.columns(2)
        if actions[0].button("从市场与发现开始", key="help_start_market", type="primary"):
            _go_to("市场与发现")
        if actions[1].button("直接打开研究台", key="help_start_research"):
            _go_to("研究台")
    elif selected == "research_roundtable":
        st.markdown(
            """
            **先看时间。** 请求截止日、有效数据日和研究编号应属于同一份研究；日期不同的报告不会静默复用。

            **再看两面证据。** 支持证据解释为什么值得继续研究，反对证据和失效条件告诉你什么情况下应该停下或重新判断。

            **最后看下一步。** AI 的动作和数量只是研究意见。你可以采纳、部分采纳或完全不采纳，但任何模拟订单仍要经过交易前检查并由你确认。

            专家圆桌不是另起一套结论：它围绕一份已打开的冻结研究，让不同角色交叉质疑。先在研究报告中选择「专家圆桌」，再查看各轮观点、未解决分歧和引用的证据；圆桌不会改写原报告、仓位或订单。
            """
        )
        st.info("专家圆桌的入口在具体研究报告内，避免把没有证据身份的泛泛讨论误当成交易依据。")
        if st.button("打开研究台", key="help_open_research", type="primary"):
            _go_to("研究台")
    elif selected == "simulation":
        st.markdown(
            """
            **AI 检查卡是什么？** 它把研究意见、你的委托、参考价格、预计费用、操作后现金和仓位影响放在一起，方便你在提交前核对。它不是自动交易，也不是“AI 批准才允许买”。

            **AI 不推荐还能买吗？** 可以。你仍能按自己的方向创建模拟委托；系统会保留这次逆建议操作的上下文。现金、整手、T+1、停牌、涨跌停、仓位上限和可卖数量等明确交易规则不能绕过。

            **为什么有时没有确认按钮？** 当只有最近收盘价、行情过期或当前不能形成可执行模拟时，系统会明确显示原因，不会把参考价格伪装成盘中成交。账户、标的、方向、数量、行情或研究身份变化后，旧检查也需要重新运行。

            **订单会怎样变化？** 委托会依次呈现等待、部分成交、已成交、撤销、拒绝或过期等状态；持仓、市值、已实现与未实现盈亏、费用和现金都从模拟账本读取。
            """
        )
        if st.button("打开模拟交易", key="help_open_simulator", type="primary"):
            _go_to("组合与交易")
    elif selected == "review_notifications":
        st.markdown(
            """
            决策复盘把一笔模拟订单、关联研究、成交记录和后续结果放在同一条时间线上。这样你可以区分：当时看到了什么、最终做了什么、后来发生了什么，而不是用事后结果改写原判断。

            用户模拟账户、正式影子账户、历史演示和外部只读成交记录始终隔离。你的自由模拟买卖不会改写系统的前瞻证据，历史演示也不会被当作正式成绩。

            通知会提示订单、研究、数据和风险变化。耗时的研究或 AI 对话会转入后台；离开页面后仍可在专业空间查看进度、失败原因或安全重试。
            """
        )
        actions = st.columns(2)
        if actions[0].button("打开决策复盘", key="help_open_review", type="primary"):
            _go_to("决策复盘")
        if actions[1].button("查看提醒与任务", key="help_open_notifications"):
            st.session_state["product_mine_view_target"] = "提醒与任务"
            _go_to("专业空间")
    elif selected == "assistant_settings":
        st.markdown(
            """
            右侧 AI 助手会识别你正在查看的标的或冻结研究，并把新问题关联到该上下文。短问题会直接回答；耗时问题会进入后台，页面可以继续使用，完成后消息会回到当前会话。

            追问时优先问具体问题，例如“这份报告的反对证据是什么？”或“这个条件失效后应该检查什么？”。AI 可以解释、比较和起草操作，但不会替你向券商下单。

            在「设置」中可以选择默认模型、更新本机 API Key 和保存通知收件地址。密钥不会写入研究、订单或聊天记录；只填写邮箱不会假装已经配置邮件投递。
            """
        )
        actions = st.columns(2)
        if actions[0].button("打开 AI 助手", key="help_open_global_assistant", type="primary"):
            st.session_state["global_ai_assistant_open"] = True
            st.rerun()
        if actions[1].button("打开设置", key="help_open_settings"):
            _go_to("设置")
    else:
        st.caption("页面会用“正常、部分可用、数据降级、暂不可用或失败”说明当前状态。")
        with st.expander("为什么研究结果可能是观察或需要复核？", expanded=True):
            st.write("观察、持有、需要复核和空候选都是正常研究结果。它们不等同于系统故障，也不构成买卖指令。")
        with st.expander("为什么显示的不是实时行情？"):
            st.write("页面会展示最近可信交易日和新鲜度；非交易日、数据未更新或只拿到收盘价时，不会伪造当日实时行情。")
        with st.expander("为什么无法确认模拟委托？"):
            st.write("通常是行情不可执行、现金或可卖数量不足、整手、涨跌停、停牌、数据过期或仓位规则未通过。页面会显示中文原因和下一步。")
        with st.expander("AI 会自动交易吗？"):
            st.write("不会。AI 可以研究、解释和起草操作，但模拟委托必须由你单独确认，系统不连接券商。")
        with st.expander("系统已经证明能赚钱吗？"):
            st.write("没有。工程质量、回测和历史演示不等于稳定超额收益；正式结论需要持续、自然到期的前瞻证据。")
        if st.button("查看系统状态与任务", key="help_open_runtime"):
            st.session_state["product_mine_view_target"] = "系统状态"
            _go_to("专业空间")


def render_mine(settings: Settings) -> None:
    views = ("账户与论文", "提醒与任务", "AI 对话", "系统状态", "高级与审计")
    requested_view = st.session_state.pop("product_mine_view_target", None)
    default_view = requested_view if requested_view in views else (
        "提醒与任务"
        if st.session_state.get("product_notification_target")
        or int(st.session_state.get(PRODUCT_ATTENTION_COUNT_KEY) or 0)
        else "账户与论文"
    )
    st.info("账户、外部只读记录、正式影子账户和历史演示保持独立身份与账本。")
    view = st.segmented_control(
        "专业空间视图",
        views,
        default=default_view,
        required=True,
        key="product_mine_view",
        label_visibility="collapsed",
        width="stretch",
    )
    if view == "账户与论文":
        _render_account_workspace(settings)
    elif view == "提醒与任务":
        _render_mine_attention(settings)
    elif view == "AI 对话":
        _render_mine_chat(settings)
    elif view == "系统状态":
        _render_mine_runtime(settings)
    else:
        _render_mine_advanced(settings)


def _render_account_workspace(settings: Settings) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    repository = Round5Repository(path)
    lifecycle = Round8Repository(path)
    account_view = st.segmented_control(
        "账户与论文内容",
        ["真实组合", "投资论文", "AI 建议与外部成交"],
        default="真实组合",
        required=True,
        key="product_account_workspace_view",
        label_visibility="collapsed",
        width="stretch",
    )
    if account_view == "投资论文":
        _render_investment_theses(lifecycle)
        return
    portfolios = repository.investor_portfolios()
    st.subheader("真实只读投资组合")
    if not portfolios:
        cols = st.columns(2)
        name = cols[0].text_input("组合名称", value="我的真实持仓", key="product_portfolio_name")
        cash = cols[1].number_input("现金", min_value=0.0, value=0.0, key="product_portfolio_cash")
        if st.button("创建只读组合", key="product_create_portfolio"):
            create_investor_portfolio(settings, name=name, cash=cash)
            st.rerun()
    else:
        portfolio_id = st.selectbox(
            "选择组合",
            [item["portfolio_id"] for item in portfolios],
            format_func=lambda value: next(
                item["name"] for item in portfolios if item["portfolio_id"] == value
            ),
            key="product_portfolio_select",
        )
        overview = repository.investor_overview(portfolio_id)
        if account_view == "真实组合":
            _render_investor_portfolio_overview(
                settings,
                portfolio_id=portfolio_id,
                overview=overview,
            )
            return

        st.subheader("AI建议采纳与外部成交")
        default_symbol = overview["positions"][0]["symbol"] if overview["positions"] else "sh510300"
        recommendation_symbol = st.text_input(
            "检查标的",
            value=default_symbol,
            key="product_investor_recommendation_symbol",
        )
        if st.button("生成AI检查卡", key="product_build_investor_recommendation"):
            try:
                with st.spinner("冻结证据并计算建议动作、数量区间和组合影响..."):
                    recommendation = build_investor_recommendation(
                        settings,
                        portfolio_id=portfolio_id,
                        symbol=recommendation_symbol,
                    )
                st.session_state["product_investor_recommendation_id"] = recommendation[
                    "recommendation_id"
                ]
                st.success("AI检查卡已生成；系统不会向券商发送订单。")
            except Exception as exc:
                st.error(_friendly_error(exc, "当前无法生成建议，缺失数据不会被补造。"))
        recommendations = repository.recommendations(portfolio_id)
        if recommendations:
            selected_recommendation_id = st.selectbox(
                "历史建议",
                [item["recommendation_id"] for item in reversed(recommendations)],
                format_func=lambda value: next(
                    f"{item['symbol']} · {_research_action_label(item['action'])} · {item['as_of']}"
                    for item in recommendations
                    if item["recommendation_id"] == value
                ),
                key="product_recommendation_select",
            )
            detail = investor_recommendation_detail(
                settings, recommendation_id=selected_recommendation_id
            )
            recommendation = detail["recommendation"]
            card = recommendation["payload"]
            quantity_range = card.get("suggested_quantity_range") or [0, 0]
            _render_metric_grid(
                [
                    ("建议动作", _research_action_label(card.get("suggested_action"))),
                    ("建议数量", f"{quantity_range[0]}–{quantity_range[1]}"),
                    ("操作后仓位", _percent(card.get("post_trade_weight"))),
                    ("现金变化", _money(card.get("cash_change"))),
                    ("最大计划损失", _money(card.get("maximum_planned_loss"))),
                ]
            )
            if card.get("actionable"):
                st.success("当前建议可供用户复核；真实交易仍需在外部券商手工完成。")
            else:
                st.warning("当前建议不可直接操作，请查看行情或数据可靠性原因。")
            left, right = st.columns(2)
            with left:
                st.markdown("**支持证据**")
                for item in card.get("supporting_evidence") or []:
                    st.write(f"• {item}")
            with right:
                st.markdown("**反对证据**")
                for item in card.get("opposing_evidence") or []:
                    st.write(f"• {item}")
            if card.get("invalidation_conditions"):
                st.markdown("**失效条件：** " + "；".join(card["invalidation_conditions"]))
            reliability = card.get("data_reliability") or {}
            st.caption(
                f"研究上下文完整度 {_percent(reliability.get('context_quality'))} · "
                f"行情可信等级 {_data_trust_label(reliability.get('quote_trust_level'))} · "
                f"建议只用于产品复核"
            )
            decision_label = st.radio(
                "我的决定",
                ["adopted", "partially_adopted", "rejected", "user_override"],
                format_func=_adoption_decision_label,
                horizontal=True,
                key="product_adoption_decision",
            )
            record_fill = decision_label == "user_override" or (
                decision_label != "rejected"
                and st.checkbox(
                    "同时记录已在外部券商手工完成的实际成交",
                    key="product_adoption_fill",
                )
            )
            actual_quantity = actual_price = None
            actual_trade_date = None
            transaction_cost = 0.0
            trade_side = None
            if record_fill:
                fill_cols = st.columns(5)
                trade_side = fill_cols[0].selectbox(
                    "成交方向",
                    ["buy", "sell"],
                    format_func=lambda value: "买入" if value == "buy" else "卖出",
                    key="product_external_trade_side",
                )
                actual_quantity = int(
                    fill_cols[1].number_input(
                        "实际数量",
                        min_value=1,
                        value=max(1, int(quantity_range[0] or 100)),
                        step=100,
                    )
                )
                actual_price = float(
                    fill_cols[2].number_input(
                        "实际价格", min_value=0.01, value=float(card.get("start_price") or 1.0)
                    )
                )
                actual_trade_date = fill_cols[3].date_input("实际成交日", value=date.today())
                transaction_cost = float(
                    fill_cols[4].number_input("实际费用", min_value=0.0, value=0.0)
                )
            adoption_note = st.text_input("备注", key="product_adoption_note")
            if st.button("保存采纳记录", key="product_save_adoption"):
                try:
                    record_recommendation_adoption(
                        settings,
                        recommendation_id=selected_recommendation_id,
                        decision=decision_label,
                        actual_quantity=actual_quantity,
                        actual_price=actual_price,
                        actual_trade_date=actual_trade_date,
                        trade_side=trade_side,
                        transaction_cost=transaction_cost,
                        note=adoption_note or None,
                    )
                    st.success("采纳记录已保存；结算前可修正，历史版本会完整保留。")
                    st.rerun()
                except Exception as exc:
                    st.error(_friendly_error(exc, "采纳记录保存失败。"))
            if detail["revisions"]:
                st.markdown("**采纳修订历史**")
                st.dataframe(
                    [
                        {
                            "版本": item["revision_number"],
                            "决定": _adoption_decision_label(item["decision"]),
                            "数量": item["actual_quantity"] or "—",
                            "价格": _money(item["actual_price"]),
                            "成交日": item["actual_trade_date"] or "—",
                            "费用": _money(item["transaction_cost"]),
                            "已结算": "是" if item["settled"] else "否",
                            "记录时间": _time(item["recorded_at"]),
                        }
                        for item in detail["revisions"]
                    ],
                    hide_index=True,
                    width="stretch",
                )
            if detail["outcomes"]:
                st.markdown("**建议后的5/20日结果**")
                st.dataframe(
                    [
                        {
                            "周期": f"{item['horizon_days']}日",
                            "到期日": item["due_date"],
                            "起始价": _money(item["start_price"]),
                            "结束价": _money(item["end_price"]),
                            "收益": _percent(item["realized_return_pct"], already_percent=True),
                            "来源": item["source"],
                        }
                        for item in detail["outcomes"]
                    ],
                    hide_index=True,
                    width="stretch",
                )
        effects = investor_recommendation_effects(settings, portfolio_id=portfolio_id)
        if effects["records"]:
            with st.expander("采纳与未采纳建议的产品效果"):
                st.dataframe(
                    [
                        {
                            "分组": key,
                            "到期样本": value["samples"],
                            "平均收益": _percent(value["average_return_pct"], already_percent=True),
                        }
                        for key, value in effects["comparison"].items()
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(effects["claim_boundary"])

def _render_investment_theses(lifecycle: Round8Repository) -> None:
    st.subheader("投资论文")
    theses = lifecycle.theses()
    if not theses:
        render_page_state(
            "empty",
            "还没有投资论文",
            "采纳或部分采纳一份 AI 建议后，系统会创建可持续复评的投资论文。",
        )
        return
    st.dataframe(
        [
            {
                "标的": item["symbol"],
                "状态": _thesis_status_label(item["lifecycle_status"]),
                "核心论文": item["core_thesis"],
                "当前生效版本": (
                    f"v{item['current_frozen_revision']['revision_number']}"
                    if item.get("current_frozen_revision")
                    else "尚未生效"
                ),
                "冻结时间": (
                    _time(item["current_frozen_revision"].get("frozen_at"))
                    if item.get("current_frozen_revision")
                    else "待用户确认"
                ),
                "下一次检查": item.get("next_check_at") or "待安排",
                "红线数量": len(item.get("red_lines") or []),
                "假设数量": len(item.get("assumptions") or []),
                "检查次数": len(item.get("checks") or []),
            }
            for item in theses
        ],
        hide_index=True,
        width="stretch",
    )


def _render_investor_portfolio_overview(
    settings: Settings,
    *,
    portfolio_id: str,
    overview: dict[str, Any],
) -> None:
    st.subheader("账户快照")
    if overview["nav"]:
        latest = overview["nav"][-1]
        columns = st.columns(4)
        columns[0].metric("总资产", f"¥{latest['equity']:,.2f}")
        columns[1].metric("今日盈亏", f"¥{latest['today_pnl']:,.2f}")
        columns[2].metric("现金", f"¥{latest['cash']:,.2f}")
        columns[3].metric("数据状态", _status_label(latest["data_status"]))
    if overview["positions"]:
        st.dataframe(
            [
                {
                    "标的": item["symbol"],
                    "数量": item["quantity"],
                    "平均成本": _money(item["average_cost"]),
                    "最新价": _money(item["latest_price"]),
                    "行情状态": _status_label(item["price_status"]),
                    "已实现盈亏": _money(item["realized_pnl"]),
                    "行业": item.get("industry") or "未分类",
                }
                for item in overview["positions"]
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("当前组合还没有持仓记录。可以导入外部持仓或成交，系统只读保存。")
    if st.button("刷新真实组合盯市", key="product_mark_investor"):
        try:
            result = mark_investor_portfolios(settings, as_of=date.today(), portfolio_id=portfolio_id)
            st.success(f"已更新 {len(result['portfolios'])} 个组合。")
            st.rerun()
        except Exception as exc:
            st.error(_friendly_error(exc, "组合盯市暂时失败，已保留上次有效价格。"))

    st.subheader("导入外部记录")
    st.caption("导入只更新这个只读组合，不会创建券商连接，也不会影响正式实验或模拟账户。")
    import_type = st.selectbox(
        "导入内容",
        ["positions", "trades"],
        format_func=lambda value: {"positions": "持仓", "trades": "成交记录"}[value],
        key="product_import_type",
    )
    uploaded = st.file_uploader("导入持仓或成交 CSV", type=["csv"], key="product_csv")
    if uploaded is not None and st.button("预览导入", key="product_preview_import"):
        try:
            content = uploaded.getvalue().decode("utf-8-sig")
            preview = preview_investor_csv(
                settings,
                portfolio_id=portfolio_id,
                import_type=import_type,
                csv_content=content,
                idempotency_key=f"ui-import:{uploaded.name}:{len(content)}",
            )
            st.session_state["product_import_preview"] = preview
        except Exception as exc:
            st.error(_friendly_error(exc, "CSV 校验失败，请检查列名、数量、价格和日期。"))
    preview = st.session_state.get("product_import_preview")
    if preview and preview.get("portfolio_id") == portfolio_id:
        st.dataframe(preview["rows"], hide_index=True, width="stretch")
        if st.button("确认导入", key="product_confirm_import"):
            try:
                confirm_investor_import(settings, import_id=preview["import_id"], confirm=True)
                st.success("CSV 已确认导入。")
                st.session_state.pop("product_import_preview", None)
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "导入确认失败，原组合数据未被覆盖。"))


def _render_mine_attention(settings: Settings) -> None:
    st.subheader("提醒与任务")
    views = ("决策任务", "通知中心")
    requested_view = st.session_state.pop("product_mine_attention_view_target", None)
    default_view = requested_view if requested_view in views else (
        "通知中心" if st.session_state.get("product_notification_target") else "决策任务"
    )
    view = st.segmented_control(
        "提醒与任务内容",
        views,
        default=default_view,
        required=True,
        key="product_mine_attention_view",
        label_visibility="collapsed",
    )
    if view == "通知中心":
        _render_notification_center(settings)
    else:
        _render_decision_task_center(settings)


def _render_mine_chat(settings: Settings) -> None:
    st.subheader("AI 对话")
    st.caption("在这里继续追问已有研究、查看引用证据，或等待后台任务完成。交易草稿仍需单独确认。")
    _render_chat(settings)


def _render_mine_runtime(settings: Settings) -> None:
    st.subheader("系统状态")
    view = st.segmented_control(
        "系统状态内容",
        ["数据状态", "持续运行"],
        default="数据状态",
        required=True,
        key="product_mine_runtime_view",
        label_visibility="collapsed",
    )
    if view == "数据状态":
        _render_readiness_summary(_ui_readiness_snapshot(settings))
        return
    report = soak_report(settings)
    if report["status"] == "no_observations":
        render_page_state(
            "empty",
            "尚无持续运行观测",
            "系统不会因为暂时没有观测就声称已经连续运行多日。",
        )
        return
    duration_hours = float(report["actual_duration_seconds"]) / 3600
    metrics = st.columns(3)
    metrics[0].metric("实际观测时长", f"{duration_hours:.2f}小时")
    metrics[1].metric("观测点", report["observation_count"])
    metrics[2].metric("数据源切换", report["provider_switches"])
    st.caption(report["claim_boundary"])


def _render_mine_advanced(settings: Settings) -> None:
    st.subheader("高级与审计")
    st.caption("模型路由、Agent 输出、实验账本和运行调试与日常投资工作区隔离。")
    audit_intro, audit_action = st.columns([3, 1])
    with audit_intro:
        st.write("需要排查模型、任务或数据链路时，再进入工程审计视图。")
    if audit_action.button("进入工程审计", key="open_engineering_audit", width="stretch"):
        st.session_state["quantlab_experience_mode"] = "audit"
        st.rerun()

    view = st.segmented_control(
        "高级内容",
        ["正式证据", "隔离演示", "开机自启"],
        default="正式证据",
        required=True,
        key="product_mine_advanced_view",
        label_visibility="collapsed",
    )
    if view == "正式证据":
        _render_formal_experiment_summary(settings)
    elif view == "隔离演示":
        _render_historical_demo(settings)
    else:
        st.code(
            "quantlab runtime-autostart-install\nquantlab runtime-autostart-status\nquantlab runtime-autostart-disable\nquantlab runtime-autostart-remove"
        )
        st.caption("安装必须由用户显式执行；任务命令不包含 API Key，运行进程使用单实例租约防重复。")


def _render_notification_center(settings: Settings) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    notifications = NotificationRepository(path)
    target = st.session_state.get("product_notification_target")
    if target:
        st.info(f"当前通知：{target['title']}")
        if target.get("content"):
            st.caption(str(target["content"]))
        if target.get("data_as_of"):
            st.caption(
                f"数据截至 {target['data_as_of']} · 通知时间 {_time(target.get('created_at'))}"
            )
        if st.button("关闭当前通知", key="close_notification_target"):
            st.session_state.pop("product_notification_target", None)
            st.rerun()
    controls = st.columns(5)
    severity_label = controls[0].selectbox(
        "重要程度",
        ["全部", "info", "warning", "critical"],
        format_func=lambda value: "全部" if value == "全部" else _severity_label(value),
        key="notification_severity",
    )
    account_filter = controls[1].text_input("账户筛选", key="notification_account")
    symbol_filter = controls[2].text_input("标的筛选", key="notification_symbol")
    unread_only = controls[3].checkbox("只看未读", key="notification_unread")
    include_archived = controls[4].checkbox("包含归档", key="notification_archived")
    items = notifications.list(
        unread_only=unread_only,
        include_archived=include_archived,
        account_id=account_filter or None,
        symbol=symbol_filter or None,
        severity=None if severity_label == "全部" else severity_label,
        limit=200,
    )
    top = st.columns(2)
    top[0].metric("未读通知", notifications.unread_count(account_filter or None))
    if top[1].button("全部标为已读", key="notification_read_all"):
        marked = _mark_visible_notifications_read(notifications, items)
        _queue_product_feedback(f"已标记 {marked} 条通知为已读。")
        st.rerun()
    if not items:
        render_page_state(
            "empty",
            "当前筛选条件下没有通知",
            "新的订单、研究、数据或风险变化会在这里出现；不会用演示消息填充。",
        )
    for item in items[:50]:
        notification_title, notification_content = _notification_display(item)
        severity = _severity_label(item["severity"])
        title = (
            f"{'未读' if not item['read'] else '已读'} · {severity} · "
            f"{notification_title} · {_time(item['created_at'])}"
        )
        with st.expander(title):
            st.write(notification_content)
            st.caption(
                f"账户 {item.get('account_id') or '—'} · 标的 {item.get('symbol') or '—'} · "
                f"数据截至 {item.get('data_as_of') or '—'}"
            )
            buttons = st.columns(3)
            if not item["read"] and buttons[0].button(
                "标为已读", key=f"read_notification_{item['notification_id']}"
            ):
                notifications.mark_read(item["notification_id"])
                _queue_product_feedback("通知已标为已读。")
                st.rerun()
            if not item["archived"] and buttons[1].button(
                "归档", key=f"archive_notification_{item['notification_id']}"
            ):
                notifications.archive(item["notification_id"])
                _queue_product_feedback("通知已归档。")
                st.rerun()
            if item.get("action_type") and buttons[2].button(
                NOTIFICATION_ACTION_LABELS.get(str(item["action_type"]), "查看关联内容"),
                key=f"open_notification_{item['notification_id']}",
            ):
                _open_notification(settings, item)
    preferences = notifications.preferences()
    delivery_worker = NotificationDeliveryWorker(settings, worker_id="product-ui")
    try:
        email_status = delivery_worker.channel_status("email")
    except Exception:
        email_status = {"state": "not_configured", "enabled": False, "recipient_configured": False}
    with st.expander("通知偏好", expanded=False):
        _render_notification_preferences(notifications, preferences)
    with st.expander("外部渠道与投递状态", expanded=False):
        if preferences:
            st.dataframe(
                [
                    {
                        "通知类型": NOTIFICATION_TYPE_LABELS.get(
                            item["notification_type"], "其他通知"
                        ),
                        "启用": bool(item["enabled"]),
                        "最低级别": _severity_label(item["minimum_severity"]),
                        "冷却秒数": item["cooldown_seconds"],
                    }
                    for item in preferences
                ],
                hide_index=True,
                width="stretch",
            )
        _render_email_delivery_status(email_status)
        st.caption("这里显示真实队列状态；邮件仍由后台通知 Worker 投递，不会在页面中直接连接发件服务。")
        if st.button("管理邮件设置", key="open_email_settings"):
            _go_to("设置")


def _render_notification_preferences(
    notifications: NotificationRepository,
    preferences: list[dict[str, Any]],
) -> None:
    preference_by_type = {str(item["notification_type"]): item for item in preferences}
    notification_type = st.selectbox(
        "通知类型",
        sorted(NOTIFICATION_TYPE_LABELS),
        format_func=lambda value: NOTIFICATION_TYPE_LABELS[value],
        key="notification_preference_type",
    )
    current = preference_by_type.get(notification_type, {})
    mandatory = notification_type in MANDATORY_NOTIFICATION_TYPES
    columns = st.columns(3)
    enabled = True
    if mandatory:
        columns[0].toggle(
            "接收站内通知",
            value=True,
            disabled=True,
            key=f"notification_enabled_{notification_type}",
        )
    else:
        enabled = columns[0].toggle(
            "接收站内通知",
            value=bool(current.get("enabled", True)),
            key=f"notification_enabled_{notification_type}",
        )
    minimum_severity = columns[1].selectbox(
        "最低级别",
        ["info", "warning", "critical"],
        index=["info", "warning", "critical"].index(str(current.get("minimum_severity", "info"))),
        format_func=lambda value: {
            "info": "全部更新",
            "warning": "仅提醒与重要",
            "critical": "仅重要",
        }[value],
        key=f"notification_minimum_{notification_type}",
    )
    cooldown_seconds = int(
        columns[2].number_input(
            "同类冷却秒数",
            min_value=0,
            max_value=604_800,
            value=int(current.get("cooldown_seconds", 300)),
            step=60,
            key=f"notification_cooldown_{notification_type}",
        )
    )
    if mandatory:
        st.caption("关键风险通知始终保留；下面的级别和冷却仍会记录，不能压掉风险提示。")
    else:
        st.caption("偏好只影响之后的非强制站内通知，不会改变已生成的通知或任何交易规则。")
    if st.button("保存通知偏好", key="save_notification_preference"):
        notifications.update_preferences(
            [
                {
                    "notification_type": notification_type,
                    "enabled": enabled,
                    "minimum_severity": minimum_severity,
                    "cooldown_seconds": cooldown_seconds,
                }
            ]
        )
        _queue_product_feedback("通知偏好已保存。")
        st.rerun()


def _render_decision_task_center(settings: Settings) -> None:
    repository = Round9Repository(settings.resolve(settings.get("system.database_path")))
    refresh_column, _ = st.columns([1, 3])
    if refresh_column.button("刷新任务", key="decision_tasks_refresh"):
        try:
            with st.spinner("核对论文、订单和复盘条件..."):
                refresh_decision_tasks(settings)
            st.success("任务已按当前条件重新核对。")
        except Exception as exc:
            st.error(_friendly_error(exc, "任务刷新失败；已保留上次成功结果。"))
    status = st.selectbox(
        "任务状态",
        ["open", "acknowledged", "resolved", "dismissed"],
        format_func=lambda value: {
            "open": "待处理",
            "acknowledged": "已知悉",
            "resolved": "已解决",
            "dismissed": "已忽略",
        }[value],
        key="decision_task_status_filter",
    )
    tasks = repository.decision_tasks(status=status, limit=100)
    if not tasks:
        st.caption("当前筛选条件下没有任务。")
        return
    for task in tasks:
        with st.expander(f"{_severity_label(task['severity'])} · {task['title']}"):
            st.write(task["user_summary"])
            if task.get("symbol"):
                st.caption(f"标的：{task['symbol']}")
            controls = st.columns(3)
            if status == "open" and controls[0].button(
                "标记已知悉", key=f"ack_task_{task['task_id']}"
            ):
                repository.update_task_status(task["task_id"], "acknowledged")
                st.rerun()
            if status in {"open", "acknowledged"} and controls[1].button(
                "标记已解决", key=f"resolve_task_{task['task_id']}"
            ):
                repository.update_task_status(task["task_id"], "resolved")
                st.rerun()
            if status in {"open", "acknowledged"} and controls[2].button(
                "忽略", key=f"dismiss_task_{task['task_id']}"
            ):
                repository.update_task_status(task["task_id"], "dismissed")
                st.rerun()


def _render_chat(
    settings: Settings,
    *,
    default_symbol: str | None = None,
    default_research_run_id: str | None = None,
) -> None:
    path = settings.resolve(settings.get("system.database_path"))
    context_key = f"{default_symbol or 'all'}:{default_research_run_id or 'unlinked'}"
    try:
        repository = ChatRepository(path)
        jobs = JobRepository(path)
        conversations = _chat_conversations_for_context(
            repository.conversations(limit=50),
            symbol=default_symbol,
            research_run_id=default_research_run_id,
        )
    except Exception as exc:
        render_page_state(
            "error",
            "AI 对话暂时无法打开",
            _friendly_error(exc, "会话索引暂不可用；没有创建订单、研究或新的 AI 对话消息。"),
        )
        return
    if default_research_run_id:
        st.caption(
            f"AI 对话关联研究：{default_symbol or '—'} · run_id "
            f"`{default_research_run_id}`；仅显示完全匹配的会话。"
        )
        if not conversations:
            if st.button(
                "为当前研究报告创建追问会话",
                key=f"chat_create_report_{default_research_run_id}",
            ):
                try:
                    create_chat_conversation(
                        settings,
                        title=f"研究追问 · {default_symbol or '当前标的'}",
                        symbol=default_symbol,
                        research_run_id=default_research_run_id,
                        idempotency_key=f"product-report-chat:{default_research_run_id}",
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(_friendly_error(exc, "无法创建这份报告的追问会话；请重新打开有效研究报告。"))
            return
    elif default_symbol:
        st.caption(f"AI 对话关联研究：{default_symbol} · 未关联研究")
    if not conversations:
        title = st.text_input(
            "新会话标题",
            value="投资研究追问",
            key=f"chat_title_{context_key}",
        )
        if st.button("创建 AI 对话", key=f"chat_create_{context_key}"):
            try:
                conversation = create_chat_conversation(
                    settings,
                    title=title,
                    symbol=default_symbol,
                    research_run_id=default_research_run_id,
                    idempotency_key=f"product-chat:{uuid.uuid4()}",
                )
                st.session_state[f"chat_conversation_{context_key}"] = conversation["conversation_id"]
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "无法创建 AI 对话；请稍后重试。"))
        return
    conversation_ids = [item["conversation_id"] for item in conversations]
    select_key = f"chat_select_{context_key}"
    if st.session_state.get(select_key) not in conversation_ids:
        st.session_state[select_key] = conversation_ids[0]
    conversation_id = st.selectbox(
        "会话",
        conversation_ids,
        format_func=lambda value: next(
            item["title"] for item in conversations if item["conversation_id"] == value
        ),
        key=select_key,
    )
    messages = repository.messages(conversation_id, limit=100)
    for message in messages[-20:]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
            citations = repository.citations(message["message_id"])
            if citations:
                with st.expander("引用证据"):
                    st.dataframe(
                        [
                            {
                                "证据块": item["data_type"],
                                "来源": item["source"],
                                "截至": item["as_of"],
                                "质量": _status_label(item["data_quality"]),
                            }
                            for item in citations
                        ],
                        hide_index=True,
                        width="stretch",
                    )
            if message.get("job_id"):
                job = jobs.job(message["job_id"])
                if job:
                    st.caption(
                        f"后台任务：{_status_label(job['status'])} · "
                        f"进度 {float(job.get('progress') or 0):.0%}"
                    )
                    if job["status"] in {"queued", "running"} and st.button(
                        "取消任务", key=f"cancel_chat_job_{job['job_id']}"
                    ):
                        jobs.cancel(job["job_id"], "cancelled_from_five_entry_ui")
                        st.rerun()
                    if job["status"] == "failed":
                        payload = job["payload"]
                        retry_matches = bool(
                            (
                                default_symbol is None
                                or (payload.get("symbol") or default_symbol) == default_symbol
                            )
                            and (
                                default_symbol is None
                                or (payload.get("research_run_id") or None)
                                == default_research_run_id
                            )
                        )
                        if not retry_matches:
                            st.error("该失败任务的标的或研究身份已变化，不能在当前上下文重试。")
                        elif st.button(
                            "安全重试",
                            key=f"retry_chat_job_{job['job_id']}",
                        ):
                            submit_chat_job(
                                settings,
                                conversation_id=conversation_id,
                                content=payload["content"],
                                idempotency_key=f"retry:{job['job_id']}:{uuid.uuid4()}",
                                account_id=payload.get("account_id"),
                                symbol=payload.get("symbol") or default_symbol,
                                quantity=payload.get("quantity"),
                                research_run_id=payload.get("research_run_id"),
                                allow_research=bool(payload.get("allow_research")),
                            )
                            st.rerun()
    actions = repository.actions(conversation_id, limit=100)
    pending_actions = [item for item in actions if item["status"] == "confirmation_required"]
    if pending_actions:
        st.markdown("**需要独立确认的操作草稿**")
        for action in pending_actions:
            draft = action["draft_payload"]
            action_label = {
                "user_paper_order": "模拟委托",
                "notification_alert": "提醒规则",
                "notification_rule": "通知规则",
            }.get(action["action_type"], "待确认操作")
            with st.expander(
                f"{action_label} · {action.get('symbol') or '无标的'} · 等待确认"
            ):
                st.write(
                    draft.get("summary") or draft.get("reason") or "AI 对话只创建了草稿，尚未执行。"
                )
                quantity = None
                simulation_mode = None
                close_reference_acknowledged = False
                if action["action_type"] == "user_paper_order":
                    quantity = int(
                        st.number_input(
                            "确认数量",
                            min_value=1,
                            value=max(1, int(draft.get("quantity") or 100)),
                            step=100,
                            key=f"chat_action_quantity_{action['action_id']}",
                        )
                    )
                    permitted_modes = tuple(draft.get("permitted_simulation_modes") or ())
                    if permitted_modes:
                        simulation_mode = st.radio(
                            "模拟方式",
                            permitted_modes,
                            format_func=lambda value: {
                                INTRADAY_SIMULATION: "盘中模拟委托",
                                NEXT_OPEN_SIMULATION: "下一交易日模拟委托",
                            }.get(value, "待确认模拟方式"),
                            horizontal=True,
                            key=f"chat_action_mode_{action['action_id']}",
                        )
                    else:
                        st.error("这份草稿没有可用的模拟方式，请重新让 AI 对话运行交易前检查。")
                    if simulation_mode == NEXT_OPEN_SIMULATION:
                        st.info("这份草稿只使用收盘参考价，不会立即成交。")
                        close_reference_acknowledged = st.checkbox(
                            "我明白订单将等待下一交易日的可用行情结算",
                            key=f"chat_action_close_reference_ack_{action['action_id']}",
                        )
                buttons = st.columns(2)
                needs_mode_acknowledgement = (
                    action["action_type"] == "user_paper_order"
                    and (
                        simulation_mode is None
                        or (
                            simulation_mode == NEXT_OPEN_SIMULATION
                            and not close_reference_acknowledged
                        )
                    )
                )
                if buttons[0].button(
                    "独立确认",
                    key=f"confirm_chat_action_{action['action_id']}",
                    disabled=needs_mode_acknowledgement,
                ):
                    try:
                        confirm_chat_action(
                            settings,
                            action_id=action["action_id"],
                            quantity=quantity,
                            simulation_mode=simulation_mode,
                            close_reference_acknowledged=close_reference_acknowledged,
                        )
                        st.success("操作已确认；模拟订单会按已选方式重新验证服务器行情。")
                        st.rerun()
                    except Exception as exc:
                        st.error(_friendly_error(exc, "确认失败，草稿未自动执行。"))
                if buttons[1].button("取消草稿", key=f"cancel_chat_action_{action['action_id']}"):
                    cancel_chat_action(settings, action["action_id"])
                    st.rerun()
    allow_research = st.checkbox(
        "允许创建新的研究任务（可能进入后台并调用多个 AI 研究角色）",
        value=False,
        key=f"chat_allow_research_{context_key}",
    )
    prompt = st.chat_input("基于研究上下文继续追问", key=f"chat_input_{context_key}")
    if prompt:
        try:
            submission = submit_chat_job(
                settings,
                conversation_id=conversation_id,
                content=prompt,
                idempotency_key=f"five-entry:{uuid.uuid4()}",
                symbol=default_symbol,
                research_run_id=default_research_run_id,
                allow_research=allow_research,
            )
            st.session_state["latest_chat_job_id"] = submission["job_id"]
            record_product_usage(
                settings,
                event_type="chat_question_submitted",
                entrypoint="研究台" if default_symbol else "专业空间",
                symbol=default_symbol,
                reference_id=conversation_id,
                payload={"question_type": "context_follow_up"},
            )
            st.rerun()
        except Exception as exc:
            st.error(_friendly_error(exc, "AI 对话暂不可用；失败不会自动确认订单或重复付费。"))


def _llm_preference_values(settings: Settings, provider: str) -> tuple[str, str]:
    if provider == "deepseek":
        return (
            str(settings.get("llm.deepseek_model", "deepseek-chat")),
            str(settings.get("llm.deepseek_base_url", "")),
        )
    if provider == "openai":
        return (
            str(settings.get("llm.openai_model", "gpt-5.6-terra")),
            str(settings.get("llm.openai_base_url", "")),
        )
    return (
        str(settings.get("llm.model", "")),
        str(settings.get("llm.base_url", "")),
    )


def _email_delivery_copy(status: dict[str, Any]) -> tuple[str, str]:
    state = str(status.get("state") or "not_configured")
    copy = {
        "not_configured": ("未配置发件服务", "还没有可用的邮件渠道配置。"),
        "disabled": ("邮件通知未启用", "发件服务已准备好，但不会投递邮件。"),
        "not_ready": ("发件服务未就绪", "本机 SMTP 配置或收件地址还不完整。"),
        "ready": ("邮件渠道已就绪", "新通知会先进入队列，再由通知 Worker 投递。"),
        "queued": ("测试邮件排队中", "等待通知 Worker 处理，页面不会直接连接 SMTP。"),
        "sending": ("正在投递", "通知 Worker 正在向发件服务提交邮件。"),
        "retrying": ("等待重试", "上次投递未完成，系统会按退避策略重试。"),
        "quiet_deferred": ("静默时段延后", "邮件会在静默结束后继续排队。"),
        "daily_limit_deferred": ("达到当日上限", "邮件会在下一投递窗口继续排队。"),
        "failed": ("投递失败", "请检查本机发件服务配置；系统没有把失败当作成功。"),
        "delivered": ("SMTP 已接受", "发件服务已接受最近一次邮件，不等同于已进入收件箱。"),
    }
    return copy.get(state, ("邮件状态待确认", "通知渠道状态暂不可用。"))


def _render_email_delivery_status(status: dict[str, Any]) -> None:
    title, detail = _email_delivery_copy(status)
    columns = st.columns([2, 1, 1])
    columns[0].metric("邮件渠道", title)
    columns[1].metric("收件地址", "已保存" if status.get("recipient_configured") else "未填写")
    columns[2].metric("通知开关", "已启用" if status.get("enabled") else "未启用")
    st.caption(detail)
    latest = status.get("latest_delivery") or {}
    if latest.get("attempted_at"):
        st.caption(
            f"最近状态：{_time(latest.get('attempted_at'))} · 尝试 {int(latest.get('attempts') or 0)} 次"
        )


def render_settings(settings: Settings) -> None:
    """Personal local configuration, intentionally separated from audit controls."""

    st.subheader("AI 服务")
    active_provider = str(settings.get("llm.provider", "deepseek")).strip().lower()
    if active_provider not in LLM_PROVIDER_OPTIONS:
        active_provider = "deepseek"
    provider = st.selectbox(
        "默认 AI 服务",
        options=list(LLM_PROVIDER_OPTIONS),
        index=list(LLM_PROVIDER_OPTIONS).index(active_provider),
        format_func=lambda value: LLM_PROVIDER_LABELS[value],
        key="settings_llm_provider",
    )
    default_model, default_base_url = _llm_preference_values(settings, provider)
    configured = llm_provider_key_configured(provider)
    summary, boundary = st.columns([1, 2])
    summary.metric("当前默认服务", LLM_PROVIDER_LABELS[provider])
    boundary.caption(
        f"API Key：{masked_secret_status(configured)}。密钥只保存在本机 `.env`，"
        "不会写入研究、订单、聊天记录或页面日志。"
    )
    st.caption(LLM_PROVIDER_HELP[provider])
    model = st.text_input(
        "模型名称",
        value=default_model,
        placeholder="例如：deepseek-chat 或 gpt-5.6-terra",
        key=f"settings_llm_model_{provider}",
    )
    endpoint_required = provider == "openai_compatible"
    base_url = st.text_input(
        "请求地址" + ("（必填）" if endpoint_required else "（可选）"),
        value=default_base_url,
        placeholder=(
            "https://your-provider.example/v1"
            if endpoint_required
            else "留空使用该服务的官方请求地址"
        ),
        key=f"settings_llm_base_url_{provider}",
    )
    api_key = st.text_input(
        "API Key",
        type="password",
        placeholder="输入新密钥以更新；留空则保留当前密钥",
        key=f"settings_llm_key_{provider}",
    )
    actions = st.columns(2)
    if actions[0].button("保存 AI 设置", type="primary", key="save_llm_settings"):
        try:
            save_llm_product_preferences(
                settings.root,
                provider=provider,
                api_key=api_key or None,
                model=model,
                base_url=base_url or None,
            )
            st.session_state[f"settings_llm_key_{provider}"] = ""
            _queue_product_feedback(
                f"{LLM_PROVIDER_LABELS[provider]} 已保存为默认服务；之后新建的 AI 任务会使用它。"
            )
            st.rerun()
        except Exception:
            st.error("AI 设置未保存，请检查模型名称、请求地址和安全约束。")
    if actions[1].button(
        "移除当前服务的本机密钥",
        key="remove_llm_key",
        disabled=not configured,
    ):
        try:
            remove_local_llm_key(settings.root, provider=provider)
            _queue_product_feedback(f"已移除本机保存的 {LLM_PROVIDER_LABELS[provider]} 密钥。")
            st.rerun()
        except Exception:
            st.error("无法移除本机 AI 密钥。")

    st.divider()
    st.subheader("通知接收")
    delivery = NotificationDeliveryWorker(settings, worker_id="product-settings")
    try:
        preferences = delivery.preferences(None)
        email_preference = next(
            (item for item in preferences if item.get("channel") == "email" and item.get("account_id") is None),
            None,
        )
        email_config = dict((email_preference or {}).get("config") or {})
        email_status = delivery.channel_status("email")
    except Exception as exc:
        email_preference, email_config = None, {}
        email_status = {"state": "not_configured", "enabled": False, "recipient_configured": False}
        st.warning(_friendly_error(exc, "通知设置暂时无法读取。"))
    _render_email_delivery_status(email_status)
    recipient = st.text_input(
        "接收通知的邮箱",
        value=str(email_config.get("to_address") or ""),
        placeholder="name@example.com",
        key="settings_notification_recipient",
    ).strip()
    st.caption("收件地址可在这里保存；SMTP 主机、发件人和密码仍只在本机高级配置中维护。")
    if st.button("保存接收邮箱", key="save_notification_recipient"):
        if recipient and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            st.error("请输入有效的邮箱地址。")
        else:
            try:
                merged = {**email_config}
                if recipient:
                    merged["to_address"] = recipient
                else:
                    merged.pop("to_address", None)
                existing_enabled = bool((email_preference or {}).get("enabled")) and bool(recipient)
                delivery.configure_channel(
                    channel="email",
                    enabled=existing_enabled,
                    quiet_start=(email_preference or {}).get("quiet_start"),
                    quiet_end=(email_preference or {}).get("quiet_end"),
                    timezone=str((email_preference or {}).get("timezone") or "Asia/Shanghai"),
                    daily_maximum=int((email_preference or {}).get("daily_maximum") or 50),
                    config=merged,
                )
                _queue_product_feedback("接收邮箱已保存。")
                st.rerun()
            except Exception:
                st.error("收件地址未保存，请检查本机邮件配置。")
    email_actions = st.columns(2)
    if email_status.get("configuration_ready") and not email_status.get("enabled"):
        if email_actions[0].button("启用邮件通知", key="enable_email_notifications"):
            try:
                delivery.configure_channel(
                    channel="email",
                    enabled=True,
                    quiet_start=(email_preference or {}).get("quiet_start"),
                    quiet_end=(email_preference or {}).get("quiet_end"),
                    timezone=str((email_preference or {}).get("timezone") or "Asia/Shanghai"),
                    daily_maximum=int((email_preference or {}).get("daily_maximum") or 50),
                    config=email_config,
                )
                _queue_product_feedback("邮件通知已启用。")
                st.rerun()
            except Exception:
                st.error("邮件通知未启用，请检查本机发件服务。")
    elif email_status.get("enabled"):
        if email_actions[0].button("暂停邮件通知", key="disable_email_notifications"):
            delivery.configure_channel(
                channel="email",
                enabled=False,
                quiet_start=(email_preference or {}).get("quiet_start"),
                quiet_end=(email_preference or {}).get("quiet_end"),
                timezone=str((email_preference or {}).get("timezone") or "Asia/Shanghai"),
                daily_maximum=int((email_preference or {}).get("daily_maximum") or 50),
                config=email_config,
            )
            _queue_product_feedback("邮件通知已暂停。")
            st.rerun()
    else:
        email_actions[0].button("启用邮件通知", key="enable_email_notifications", disabled=True)
    if email_actions[1].button(
        "发送测试邮件",
        key="queue_email_test",
        disabled=not bool(email_status.get("ready")),
    ):
        try:
            delivery.queue_email_test()
            _queue_product_feedback("测试邮件已进入通知队列，等待通知 Worker 投递。")
            st.rerun()
        except Exception:
            st.error("测试邮件未入队，请检查邮件渠道状态。")


def _global_chat_context_label(
    page: str,
) -> tuple[str, str | None, str | None, str | None, str]:
    """Return the complete UI identity used by the persistent right rail."""

    context = product_context(st.session_state)
    page_scope = f"page:{page}"
    if context.order_id:
        page_scope = f"{page_scope}:order:{context.order_id}"
    parts = [f"当前：{page}"]
    if context.account_id:
        parts.append(f"模拟账户 {context.account_id[:8]}")
    if context.research_run_id and context.symbol:
        parts.append(f"{context.symbol} 的冻结研究")
    elif context.symbol:
        parts.append(context.symbol)
    if context.order_id:
        parts.append(f"订单 {context.order_id[:8]}")
    return (
        " · ".join(parts),
        context.symbol,
        context.research_run_id,
        context.account_id,
        page_scope,
    )


def _render_global_ai_assistant(settings: Settings, *, page: str) -> None:
    """A lazy right rail: closed means no conversation or database work."""

    is_open = bool(st.session_state.get("global_ai_assistant_open", False))
    with st.container(key="global_ai_assistant", border=False):
        if not is_open:
            if st.button(
                "打开 AI 助手",
                icon=":material/auto_awesome:",
                key="open_global_ai_assistant",
                help="在当前页面继续向 AI 提问",
            ):
                st.session_state["global_ai_assistant_open"] = True
                st.rerun()
            return
        heading, collapse = st.columns([4, 1])
        heading.markdown("**AI 助手**")
        if collapse.button(
            "收起",
            icon=":material/chevron_right:",
            key="collapse_global_ai_assistant",
            help="收起 AI 助手",
        ):
            st.session_state["global_ai_assistant_open"] = False
            st.rerun()
        context_label, symbol, research_run_id, account_id, page_scope = _global_chat_context_label(page)
        st.caption(context_label)
        st.caption("当前页面的对话会单独保存；回答、订单草稿和研究不会跨页面或账户混用。")
        path = settings.resolve(settings.get("system.database_path"))
        context_key = ":".join(
            [
                page_scope,
                account_id or "unbound",
                symbol or "unlinked",
                research_run_id or "unlinked",
            ]
        )
        try:
            repository = ChatRepository(path)
            jobs = JobRepository(path)
            conversations = repository.conversations_for_context(
                account_id=account_id,
                symbol=symbol,
                research_run_id=research_run_id,
                page_scope=page_scope,
                limit=20,
            )
        except Exception as exc:
            st.error(_friendly_error(exc, "AI 助手暂时无法连接会话。"))
            return
        conversation_id = st.session_state.get(f"global_chat_conversation_{context_key}")
        conversation_ids = [item["conversation_id"] for item in conversations]
        if conversation_id not in conversation_ids:
            conversation_id = conversation_ids[0] if conversation_ids else None
            st.session_state[f"global_chat_conversation_{context_key}"] = conversation_id
        if conversation_id:
            recent_messages = repository.messages(conversation_id, limit=8)[-6:]
            citations_by_message = repository.citations_for_messages(
                [str(message["message_id"]) for message in recent_messages]
            )
            for message in recent_messages:
                role = "用户" if message.get("role") == "user" else "AI"
                st.markdown(
                    f"<div class=\"ql-assistant-message ql-assistant-{message.get('role')}\">"
                    f"<b>{role}</b><p>{escape(str(message.get('content') or ''))}</p></div>",
                    unsafe_allow_html=True,
                )
                citations = citations_by_message.get(str(message["message_id"]), [])
                if citations:
                    with st.expander(f"引用证据 · {len(citations)} 条", expanded=False):
                        for citation in citations[:8]:
                            st.caption(
                                f"{citation['data_type']} · {citation['source']} · "
                                f"截至 {_time(citation.get('as_of'))} · "
                                f"{_status_label(citation.get('data_quality'))}"
                            )
                if message.get("job_id") and message.get("role") == "user":
                    job = jobs.job(message["job_id"])
                    if job:
                        status = str(job.get("status") or "queued")
                        st.caption(
                            f"后台任务：{_status_label(status)} · "
                            f"进度 {float(job.get('progress') or 0):.0%}"
                        )
                        task_actions = st.columns(2)
                        if status in {"queued", "running"}:
                            if task_actions[0].button(
                                "刷新进度",
                                icon=":material/refresh:",
                                key=f"refresh_global_chat_job_{job['job_id']}",
                            ):
                                st.rerun()
                            if task_actions[1].button(
                                "取消",
                                key=f"cancel_global_chat_job_{job['job_id']}",
                            ):
                                jobs.cancel(job["job_id"], "cancelled_from_global_assistant")
                                _queue_product_feedback("AI 助手任务已取消。")
                                st.rerun()
                        elif status == "failed" and task_actions[0].button(
                            "安全重试",
                            key=f"retry_global_chat_job_{job['job_id']}",
                        ):
                            payload = dict(job.get("payload") or {})
                            submit_chat_job(
                                settings,
                                conversation_id=conversation_id,
                                content=str(payload.get("content") or message.get("content") or ""),
                                idempotency_key=f"global-chat-retry:{job['job_id']}:{uuid.uuid4()}",
                                account_id=account_id,
                                symbol=symbol,
                                research_run_id=research_run_id,
                                allow_research=False,
                            )
                            _queue_product_feedback("AI 助手任务已重新排队。")
                            st.rerun()
            pending_actions = [
                item
                for item in repository.actions(conversation_id, limit=20)
                if item["status"] == "confirmation_required"
            ]
            if pending_actions:
                st.markdown("**等待你确认的操作草稿**")
                for action in pending_actions[:3]:
                    draft = action.get("draft_payload") or {}
                    st.markdown(
                        '<div class="ql-assistant-action-draft">'
                        f"<b>{escape(str(action.get('symbol') or '无标的'))}</b>"
                        f"<p>{escape(str(draft.get('summary') or draft.get('reason') or '操作草稿待确认'))}</p>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                st.caption("操作草稿不会自动执行；需进入完整会话重新核对数量、模式与行情。")
                if st.button(
                    "打开独立确认页",
                    icon=":material/open_in_new:",
                    key=f"open_global_chat_confirmation_{context_key}",
                    width="stretch",
                ):
                    st.session_state["product_mine_view_target"] = "AI 对话"
                    st.session_state["chat_select_all:unlinked"] = conversation_id
                    _go_to("专业空间")
        else:
            st.caption("提出第一个问题后，系统才会创建一条与当前页面关联的会话。")
        # A form clears its widget only after submission.  This avoids writing
        # to a live Streamlit widget key, which previously made the assistant
        # fail after a successful send on some Streamlit versions.
        with st.form(key=f"global_chat_form_{context_key}", clear_on_submit=True):
            prompt = st.text_area(
                "向 AI 提问",
                placeholder="例如：这份研究下一步最该核对什么？",
                height=86,
                key=f"global_chat_prompt_{context_key}",
                label_visibility="collapsed",
            ).strip()
            submitted = st.form_submit_button(
                "发送",
                icon=":material/send:",
                type="primary",
            )
        if submitted and prompt:
            try:
                if conversation_id is None:
                    conversation = create_chat_conversation(
                        settings,
                        title=f"AI 助手 · {page}",
                        account_id=account_id,
                        symbol=symbol,
                        research_run_id=research_run_id,
                        page_scope=page_scope,
                        idempotency_key=f"global-chat:{context_key}:{uuid.uuid4()}",
                    )
                    conversation_id = conversation["conversation_id"]
                    st.session_state[f"global_chat_conversation_{context_key}"] = conversation_id
                submit_chat_job(
                    settings,
                    conversation_id=conversation_id,
                    content=prompt,
                    idempotency_key=f"global-chat-job:{uuid.uuid4()}",
                    account_id=account_id,
                    symbol=symbol,
                    research_run_id=research_run_id,
                    allow_research=False,
                )
                _queue_product_feedback("问题已转入后台处理；完成后会显示在这里。")
                st.rerun()
            except Exception as exc:
                st.error(_friendly_error(exc, "AI 助手暂时无法回答；没有创建订单或改写研究。"))
        elif submitted:
            st.caption("先输入一个问题，再发送给 AI 助手。")


__all__ = [
    "PRIMARY_ENTRYPOINTS",
    "render_ai_research",
    "render_help_center",
    "render_home",
    "render_market_and_discovery",
    "render_mine",
    "render_product_app",
    "render_review",
    "render_simulator",
]
