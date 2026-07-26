from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Any, MutableMapping

import streamlit as st


PRODUCT_PAGES = (
    "今日",
    "市场与发现",
    "研究台",
    "组合与交易",
    "决策复盘",
)
PRODUCT_UTILITY_PAGES = (
    "专业空间",
    "帮助中心",
)
# These are full workspaces reached from the primary navigation, rather than
# additional primary destinations.  Keeping the distinction explicit prevents
# a detail view from turning the sidebar into an ever-growing page list.
PRODUCT_DETAIL_PAGES = (
    "研究详情",
    "专家圆桌",
    "设置",
)
PRODUCT_ROUTE_PAGES = PRODUCT_PAGES + PRODUCT_UTILITY_PAGES + PRODUCT_DETAIL_PAGES
PRODUCT_ROUTE_PARENTS = {
    "研究详情": "研究台",
    "专家圆桌": "研究台",
    "设置": "专业空间",
}
PRODUCT_PAGE_KEY = "product_active_page"
PRODUCT_NAVIGATION_KEY = "product_navigation_choice"
PRODUCT_PAGE_TARGET_KEY = "product_navigation_target"
PRODUCT_NAVIGATION_COMPACT_KEY = "product_navigation_compact"
PRODUCT_MOBILE_NAVIGATION_OPEN_KEY = "product_mobile_navigation_open"
PRODUCT_CONTEXT_REVISION_KEY = "product_context_revision"
PRODUCT_ATTENTION_COUNT_KEY = "product_attention_count"
RESEARCH_CACHE_KEY = "product_research_reports"
RESEARCH_REQUEST_STATE_KEY = "product_research_request_states"

PRODUCT_PAGE_ICONS = {
    "今日": ":material/home:",
    "市场与发现": ":material/travel_explore:",
    "研究台": ":material/psychology:",
    "组合与交易": ":material/account_balance_wallet:",
    "决策复盘": ":material/history:",
}


@dataclass(frozen=True)
class ProductContext:
    symbol: str | None = None
    research_run_id: str | None = None
    research_requested_as_of: str | None = None
    research_effective_as_of: str | None = None
    account_id: str | None = None
    order_id: str | None = None


@dataclass(frozen=True)
class ResearchIdentity:
    symbol: str
    requested_as_of: str
    effective_as_of: str
    run_id: str
    origin: str = "unavailable"
    evidence_stage: str = "unavailable"
    context_id: str | None = None
    context_fingerprint: str | None = None


def current_product_page(state: MutableMapping[str, Any]) -> str:
    requested = state.get(PRODUCT_PAGE_KEY)
    return requested if requested in PRODUCT_ROUTE_PAGES else PRODUCT_PAGES[0]


def product_navigation_page(page: str) -> str:
    """Return the primary sidebar entry that owns a route."""

    return PRODUCT_ROUTE_PARENTS.get(page, page)


def product_navigation_is_compact(state: MutableMapping[str, Any]) -> bool:
    """Return the user's navigation density preference for the current session."""

    return bool(state.get(PRODUCT_NAVIGATION_COMPACT_KEY, False))


def set_product_navigation_compact(
    state: MutableMapping[str, Any],
    compact: bool,
) -> None:
    """Keep the product sidebar present while switching between its two densities."""

    state[PRODUCT_NAVIGATION_COMPACT_KEY] = bool(compact)


def set_product_page(
    state: MutableMapping[str, Any],
    page: str,
    *,
    symbol: str | None = None,
    research_run_id: str | None = None,
    research_requested_as_of: str | None = None,
    research_effective_as_of: str | None = None,
    account_id: str | None = None,
    order_id: str | None = None,
) -> None:
    if page not in PRODUCT_ROUTE_PAGES:
        raise ValueError(f"unknown product page: {page}")
    normalized_symbol = symbol.strip() if symbol is not None else None
    if research_run_id and (
        not normalized_symbol or not research_requested_as_of or not research_effective_as_of
    ):
        raise ValueError(
            "research context requires symbol, requested_as_of, effective_as_of, and run_id"
        )
    state[PRODUCT_PAGE_KEY] = page
    state[PRODUCT_PAGE_TARGET_KEY] = page
    if normalized_symbol:
        previous_symbol = state.get("product_context_symbol")
        if previous_symbol != normalized_symbol:
            clear_product_research_context(state)
            state.pop("product_context_order_id", None)
            state.pop("product_pretrade_check", None)
        state["product_context_symbol"] = normalized_symbol
    if research_run_id:
        if state.get("product_context_research_run_id") != research_run_id:
            state.pop("product_pretrade_check", None)
        state["product_context_research_run_id"] = research_run_id
    if research_requested_as_of:
        state["product_context_research_requested_as_of"] = research_requested_as_of
    if research_effective_as_of:
        state["product_context_research_effective_as_of"] = research_effective_as_of
    if account_id:
        if state.get("product_context_account_id") not in {None, account_id}:
            state.pop("product_context_order_id", None)
            state.pop("product_pretrade_check", None)
        state["product_context_account_id"] = account_id
    if order_id:
        state["product_context_order_id"] = order_id
    state[PRODUCT_CONTEXT_REVISION_KEY] = int(state.get(PRODUCT_CONTEXT_REVISION_KEY) or 0) + 1


def product_context(state: MutableMapping[str, Any]) -> ProductContext:
    return ProductContext(
        symbol=state.get("product_context_symbol"),
        research_run_id=state.get("product_context_research_run_id"),
        research_requested_as_of=state.get("product_context_research_requested_as_of"),
        research_effective_as_of=state.get("product_context_research_effective_as_of"),
        account_id=state.get("product_context_account_id"),
        order_id=state.get("product_context_order_id"),
    )


def consume_product_context(
    state: MutableMapping[str, Any],
    consumer: str,
) -> ProductContext | None:
    """Return navigation context once per explicit page transition.

    Streamlit widgets own their values after the first destination render.  A
    navigation payload may seed those widgets once, but must not overwrite a
    user's later edits on every rerun.
    """

    normalized_consumer = consumer.strip()
    if not normalized_consumer:
        raise ValueError("product context consumer is required")
    revision = int(state.get(PRODUCT_CONTEXT_REVISION_KEY) or 0)
    marker = f"product_context_consumed:{normalized_consumer}"
    if state.get(marker) == revision:
        return None
    state[marker] = revision
    return product_context(state)


def clear_product_research_context(state: MutableMapping[str, Any]) -> None:
    state.pop("product_context_research_run_id", None)
    state.pop("product_context_research_requested_as_of", None)
    state.pop("product_context_research_effective_as_of", None)
    state.pop("product_pretrade_check", None)


def update_product_selection(
    state: MutableMapping[str, Any],
    *,
    symbol: str,
    requested_as_of: date | str | None = None,
) -> bool:
    """Update the active symbol and invalidate a mismatched research link."""

    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        raise ValueError("product symbol is required")
    requested = None
    if requested_as_of is not None:
        requested = (
            requested_as_of.isoformat()
            if isinstance(requested_as_of, date)
            else str(requested_as_of).strip()
        )
    context = product_context(state)
    symbol_changed = context.symbol not in {None, normalized_symbol}
    research_mismatch = bool(
        context.research_run_id
        and (
            context.symbol != normalized_symbol
            or (requested is not None and context.research_requested_as_of != requested)
        )
    )
    if research_mismatch:
        clear_product_research_context(state)
    if symbol_changed:
        state.pop("product_context_order_id", None)
    if symbol_changed or research_mismatch:
        state.pop("product_pretrade_check", None)
    state["product_context_symbol"] = normalized_symbol
    return research_mismatch


def bind_product_research_context(
    state: MutableMapping[str, Any],
    identity: ResearchIdentity,
) -> None:
    """Bind the currently displayed, fully identified report to navigation."""

    context = product_context(state)
    changed = (
        context.symbol != identity.symbol
        or context.research_run_id != identity.run_id
        or context.research_requested_as_of != identity.requested_as_of
        or context.research_effective_as_of != identity.effective_as_of
    )
    if context.symbol not in {None, identity.symbol}:
        state.pop("product_context_order_id", None)
    if changed:
        state.pop("product_pretrade_check", None)
    state["product_context_symbol"] = identity.symbol
    state["product_context_research_run_id"] = identity.run_id
    state["product_context_research_requested_as_of"] = identity.requested_as_of
    state["product_context_research_effective_as_of"] = identity.effective_as_of


def research_request_key(symbol: str, requested_as_of: date | str) -> str:
    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        raise ValueError("research symbol is required")
    requested = (
        requested_as_of.isoformat()
        if isinstance(requested_as_of, date)
        else str(requested_as_of).strip()
    )
    if not requested:
        raise ValueError("research requested as_of is required")
    return f"{normalized_symbol}|{requested}"


def research_identity(report: dict[str, Any] | None) -> ResearchIdentity | None:
    if not isinstance(report, dict):
        return None
    identity = report.get("ui_research_identity")
    if not isinstance(identity, dict):
        return None
    values = {
        "symbol": str(identity.get("symbol") or "").strip(),
        "requested_as_of": str(identity.get("requested_as_of") or "").strip(),
        "effective_as_of": str(identity.get("effective_as_of") or "").strip(),
        "run_id": str(identity.get("run_id") or "").strip(),
    }
    if not all(values.values()):
        return None
    values.update(
        {
            "origin": str(identity.get("origin") or "unavailable"),
            "evidence_stage": str(identity.get("evidence_stage") or "unavailable"),
            "context_id": identity.get("context_id"),
            "context_fingerprint": identity.get("context_fingerprint"),
        }
    )
    return ResearchIdentity(**values)


def cache_research_report(
    state: MutableMapping[str, Any],
    report: dict[str, Any],
    *,
    symbol: str,
    requested_as_of: date | str,
) -> ResearchIdentity:
    persisted = report.get("research_identity")
    persisted = persisted if isinstance(persisted, dict) else {}
    run_id = str(persisted.get("run_id") or report.get("run_id") or "").strip()
    report_symbol = str(report.get("symbol") or "").strip()
    effective_as_of = str(
        (report.get("data") or {}).get("effective_as_of") or report.get("as_of") or ""
    ).strip()
    requested_argument = (
        requested_as_of.isoformat()
        if isinstance(requested_as_of, date)
        else str(requested_as_of).strip()
    )
    requested = str(persisted.get("requested_as_of") or requested_argument).strip()
    if requested != requested_argument:
        raise ValueError("research requested_as_of does not match persisted identity")
    if not run_id or report_symbol != symbol.strip() or not effective_as_of or not requested:
        raise ValueError("research report identity is incomplete or mismatched")
    identity = ResearchIdentity(
        symbol=report_symbol,
        requested_as_of=requested,
        effective_as_of=effective_as_of,
        run_id=run_id,
        origin=str(persisted.get("origin") or "user_interactive_research"),
        evidence_stage=str(persisted.get("evidence_stage") or "research_only"),
        context_id=persisted.get("context_id"),
        context_fingerprint=persisted.get("context_fingerprint"),
    )
    stored = dict(report)
    stored["ui_research_identity"] = identity.__dict__.copy()
    cache = dict(state.get(RESEARCH_CACHE_KEY) or {})
    cache[research_request_key(report_symbol, requested)] = stored
    state[RESEARCH_CACHE_KEY] = cache
    _set_research_request_state(
        state,
        report_symbol,
        requested,
        status="success",
        active_run_id=run_id,
    )
    return identity


def cached_research_report(
    state: MutableMapping[str, Any],
    *,
    symbol: str,
    requested_as_of: date | str,
) -> dict[str, Any] | None:
    key = research_request_key(symbol, requested_as_of)
    request_state = (state.get(RESEARCH_REQUEST_STATE_KEY) or {}).get(key) or {}
    if request_state.get("status") != "success":
        return None
    report = (state.get(RESEARCH_CACHE_KEY) or {}).get(key)
    identity = research_identity(report)
    if identity is None or research_request_key(identity.symbol, identity.requested_as_of) != key:
        return None
    if str(report.get("run_id") or "") != identity.run_id:
        return None
    if request_state.get("active_run_id") != identity.run_id:
        return None
    if (
        str((report.get("data") or {}).get("effective_as_of") or report.get("as_of") or "")
        != identity.effective_as_of
    ):
        return None
    return report


def mark_research_loading(
    state: MutableMapping[str, Any], *, symbol: str, requested_as_of: date | str
) -> None:
    clear_product_research_context(state)
    _set_research_request_state(
        state, symbol, requested_as_of, status="loading", active_run_id=None
    )


def mark_research_failed(
    state: MutableMapping[str, Any], *, symbol: str, requested_as_of: date | str
) -> None:
    clear_product_research_context(state)
    _set_research_request_state(state, symbol, requested_as_of, status="failed", active_run_id=None)


def research_request_state(
    state: MutableMapping[str, Any], *, symbol: str, requested_as_of: date | str
) -> str:
    key = research_request_key(symbol, requested_as_of)
    return str(
        ((state.get(RESEARCH_REQUEST_STATE_KEY) or {}).get(key) or {}).get("status") or "idle"
    )


def has_previous_research_report(
    state: MutableMapping[str, Any], *, symbol: str, requested_as_of: date | str
) -> bool:
    return research_request_key(symbol, requested_as_of) in (state.get(RESEARCH_CACHE_KEY) or {})


def restore_previous_research_report(
    state: MutableMapping[str, Any], *, symbol: str, requested_as_of: date | str
) -> dict[str, Any] | None:
    key = research_request_key(symbol, requested_as_of)
    report = (state.get(RESEARCH_CACHE_KEY) or {}).get(key)
    identity = research_identity(report)
    if identity is None or identity.symbol != symbol.strip():
        return None
    _set_research_request_state(
        state,
        symbol,
        requested_as_of,
        status="success",
        active_run_id=identity.run_id,
    )
    return cached_research_report(state, symbol=symbol, requested_as_of=requested_as_of)


def _set_research_request_state(
    state: MutableMapping[str, Any],
    symbol: str,
    requested_as_of: date | str,
    *,
    status: str,
    active_run_id: str | None,
) -> None:
    if status not in {"idle", "loading", "success", "failed"}:
        raise ValueError("invalid research request state")
    key = research_request_key(symbol, requested_as_of)
    states = dict(state.get(RESEARCH_REQUEST_STATE_KEY) or {})
    states[key] = {"status": status, "active_run_id": active_run_id}
    state[RESEARCH_REQUEST_STATE_KEY] = states


def context_matches_research(
    context: ProductContext,
    report: dict[str, Any] | None,
) -> bool:
    identity = research_identity(report)
    return bool(
        identity
        and context.symbol == identity.symbol
        and context.research_run_id == identity.run_id
        and context.research_requested_as_of == identity.requested_as_of
        and context.research_effective_as_of == identity.effective_as_of
    )


def render_product_navigation() -> str:
    target = st.session_state.pop(PRODUCT_PAGE_TARGET_KEY, None)
    if target in PRODUCT_ROUTE_PAGES:
        st.session_state[PRODUCT_PAGE_KEY] = target
        st.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] = False
        target_navigation_page = product_navigation_page(target)
        if target_navigation_page in PRODUCT_PAGES:
            st.session_state[PRODUCT_NAVIGATION_KEY] = target_navigation_page
    active = current_product_page(st.session_state)
    active_navigation_page = product_navigation_page(active)
    if active_navigation_page not in PRODUCT_PAGES:
        selected_navigation_page = st.session_state.get(PRODUCT_NAVIGATION_KEY)
        active_navigation_page = (
            selected_navigation_page
            if selected_navigation_page in PRODUCT_PAGES
            else PRODUCT_PAGES[0]
        )
    compact = product_navigation_is_compact(st.session_state)
    mobile_navigation_open = bool(
        st.session_state.get(PRODUCT_MOBILE_NAVIGATION_OPEN_KEY, False)
    )
    with st.sidebar:
        mobile_class = "ql-mobile-navigation-open" if mobile_navigation_open else ""
        st.markdown(
            f'<span class="ql-mobile-navigation {mobile_class}" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="ql-brand">
              <div class="ql-brand-mark" aria-hidden="true"><i></i><b></b><span></span></div>
              <div><strong>QuantLab</strong><small>Evidence-led investing</small></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button(
            "关闭导航",
            icon=":material/close:",
            key="product_mobile_navigation_close",
            width="stretch",
        ):
            st.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] = False
            st.rerun()
        attention_count = int(st.session_state.get(PRODUCT_ATTENTION_COUNT_KEY) or 0)
        mode_class = "ql-navigation-mode-compact" if compact else "ql-navigation-mode-expanded"
        st.markdown(
            f'<span class="ql-navigation-mode {mode_class}" aria-hidden="true"></span>',
            unsafe_allow_html=True,
        )

        with st.container(key="product_navigation_mode_control", border=False):
            if st.button(
                "展开导航" if compact else "收起导航",
                icon=(
                    ":material/keyboard_double_arrow_right:"
                    if compact
                    else ":material/keyboard_double_arrow_left:"
                ),
                help="展开完整导航" if compact else "收起为图标导航",
                key="toggle_product_navigation_density",
                width=44,
            ):
                set_product_navigation_compact(st.session_state, not compact)
                st.rerun()

        with st.container(key="product_expanded_navigation", border=False):
            st.markdown(
                """
                <div class="ql-nav-label">投资工作区</div>
                """,
                unsafe_allow_html=True,
            )
            selected = st.radio(
                "主导航",
                PRODUCT_PAGES,
                index=PRODUCT_PAGES.index(active_navigation_page),
                key=PRODUCT_NAVIGATION_KEY,
                label_visibility="collapsed",
            )
            if attention_count:
                st.markdown(
                    f'<div class="ql-sidebar-attention">{attention_count} 条提醒需要复核</div>',
                    unsafe_allow_html=True,
                )
                if st.button(
                    "查看需要处理的提醒",
                    key="open_attention_notifications",
                    width="stretch",
                ):
                    st.session_state[PRODUCT_PAGE_TARGET_KEY] = "专业空间"
                    st.rerun()
            st.markdown(
                """
                <div class="ql-sidebar-boundary">
                  <span>研究辅助 · 用户确认</span>
                  <p>模拟订单不会连接券商；缺失与降级数据保持可见。</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('<div class="ql-nav-label ql-nav-utility-label">我的与工具</div>', unsafe_allow_html=True)
            if st.button(
                "打开 AI 助手",
                key="open_global_assistant_from_navigation",
                icon=":material/auto_awesome:",
                width="stretch",
            ):
                st.session_state["global_ai_assistant_open"] = True
                st.rerun()
            if st.button(
                "我的账户",
                key="open_product_account_workspace",
                icon=":material/account_balance_wallet:",
                width="stretch",
            ):
                st.session_state["product_mine_view_target"] = "账户与论文"
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "专业空间"
                st.rerun()
            if st.button(
                "通知与任务",
                key="open_product_notifications_workspace",
                icon=":material/notifications:",
                width="stretch",
            ):
                st.session_state["product_mine_view_target"] = "提醒与任务"
                st.session_state["product_mine_attention_view_target"] = "通知中心"
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "专业空间"
                st.rerun()
            if st.button(
                "设置",
                key="open_product_settings",
                icon=":material/settings:",
                width="stretch",
            ):
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "设置"
                st.rerun()
            if st.button(
                "帮助中心",
                key="open_product_help_center",
                icon=":material/help:",
                width="stretch",
            ):
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "帮助中心"
                st.rerun()

        with st.container(key="product_compact_navigation", border=False):
            for index, page in enumerate(PRODUCT_PAGES):
                if st.button(
                    page,
                    key=f"product_compact_navigation_page_{index}",
                    icon=PRODUCT_PAGE_ICONS[page],
                    help=f"前往{page}",
                    type="primary" if page == active_navigation_page else "tertiary",
                    width="stretch",
                ):
                    st.session_state[PRODUCT_PAGE_TARGET_KEY] = page
                    st.rerun()
            if st.button(
                "打开 AI 助手",
                key="open_compact_global_assistant",
                icon=":material/auto_awesome:",
                help="打开 AI 助手",
                type="tertiary",
                width="stretch",
            ):
                st.session_state["global_ai_assistant_open"] = True
                st.rerun()
            if st.button(
                "我的账户",
                key="open_compact_product_account_workspace",
                icon=":material/account_balance_wallet:",
                help="我的账户",
                type="tertiary",
                width="stretch",
            ):
                st.session_state["product_mine_view_target"] = "账户与论文"
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "专业空间"
                st.rerun()
            if st.button(
                "设置",
                key="open_compact_product_settings",
                icon=":material/settings:",
                help="设置",
                type="tertiary",
                width="stretch",
            ):
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "设置"
                st.rerun()
            if st.button(
                "帮助中心",
                key="open_compact_product_help_center",
                icon=":material/help:",
                help="帮助中心",
                type="tertiary",
                width="stretch",
            ):
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "帮助中心"
                st.rerun()
            if attention_count and st.button(
                "需要复核的提醒",
                key="open_compact_attention_notifications",
                icon=":material/notifications:",
                help=f"{attention_count} 条提醒需要复核",
                type="tertiary",
                width="stretch",
            ):
                st.session_state[PRODUCT_PAGE_TARGET_KEY] = "专业空间"
                st.rerun()
    # A hidden route stays open until the user explicitly chooses another
    # primary destination.  The radio itself is still rendered for keyboard
    # access, but its parent item remains highlighted.
    navigation_changed = selected in PRODUCT_PAGES and selected != active_navigation_page
    if navigation_changed:
        st.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] = False
    page = (
        selected
        if navigation_changed
        else active
    )
    st.session_state[PRODUCT_PAGE_KEY] = page
    if navigation_changed and mobile_navigation_open:
        st.rerun()
    if st.button(
        "打开导航",
        icon=":material/menu:",
        key="product_mobile_navigation_toggle",
        help="打开页面导航",
    ):
        st.session_state[PRODUCT_MOBILE_NAVIGATION_OPEN_KEY] = True
        st.rerun()
    return page


def render_page_state(
    state: str,
    title: str,
    detail: str,
    *,
    retry_label: str | None = None,
    retry_key: str | None = None,
) -> bool:
    normalized = state.strip().lower()
    labels = {
        "loading": ("正在处理", "···"),
        "empty": ("尚无内容", "○"),
        "degraded": ("降级运行", "△"),
        "unavailable": ("暂不可用", "—"),
        "error": ("读取失败", "×"),
        "success": ("状态正常", "✓"),
    }
    label, mark = labels.get(normalized, ("当前状态", "·"))
    st.markdown(
        f"""
        <section class="ql-state-card ql-state-{escape(normalized)}" role="status">
          <div class="ql-state-mark" aria-hidden="true">{mark}</div>
          <div><span>{label}</span><strong>{escape(title)}</strong><p>{escape(detail)}</p></div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    if retry_label and retry_key:
        return st.button(retry_label, key=retry_key)
    return False


def apply_product_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ql-canvas: #f2efe7;
            --ql-surface: #fbfaf6;
            --ql-surface-soft: #f6f2ea;
            --ql-ink: #252720;
            --ql-ink-soft: #464b42;
            --ql-muted: #676c63;
            --ql-line: #ddd7ca;
            --ql-line-strong: #c9c1b3;
            --ql-warm: #a55339;
            --ql-warm-dark: #7f3f2d;
            --ql-warm-soft: #f0ded5;
            --ql-pine: #386858;
            --ql-pine-soft: #dce9e3;
            --ql-blue: #48647b;
            --ql-blue-soft: #dde6ec;
            --ql-ai: #6b6487;
            --ql-ai-soft: #e7e2ef;
            --ql-radius-sm: 10px;
            --ql-radius: 16px;
            --ql-radius-lg: 24px;
            --ql-shadow: 0 1px 0 rgba(56,48,37,.03), 0 18px 50px rgba(56,48,37,.055);
            --ql-font: "Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI Variable Text", "Segoe UI", sans-serif;
            --ql-display: "Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", "DengXian", sans-serif;
        }
        html, body, [class*="css"] { font-family: var(--ql-font); }
        html { font-size: 17px; }
        body, .stApp { color: var(--ql-ink); background: var(--ql-canvas); }
        .stApp {
            background-image:
              radial-gradient(circle at 76% -10%, rgba(165,83,57,.075), transparent 31rem),
              linear-gradient(rgba(77,82,72,.025) 1px, transparent 1px),
              linear-gradient(90deg, rgba(77,82,72,.02) 1px, transparent 1px);
            background-size: auto, 32px 32px, 32px 32px;
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stToolbar"],
        [data-testid="stAppDeployButton"] { display:none !important; }
        [data-testid="stAppViewContainer"] > .main { background: transparent; }
        .main .block-container {
            max-width: 1540px;
            padding: 1.25rem 2.4rem 4rem;
        }
        .main .block-container > div { min-width: 0; }
        [data-testid="stSidebar"] {
            width: 270px !important;
            min-width: 270px !important;
            background: rgba(248,246,240,.97);
            border-right: 1px solid var(--ql-line);
        }
        [data-testid="stSidebar"] > div:first-child { width: 270px !important; }
        [data-testid="stSidebarContent"] { padding: .9rem .85rem 1.2rem; }
        /* The application owns sidebar density. Do not leave Streamlit's detached arrow in the page. */
        [data-testid="stSidebarHeader"],
        [data-testid="stSidebarCollapseButton"],
        [data-testid="stExpandSidebarButton"],
        [data-testid="stSidebarCollapsedControl"] { display:none !important; }
        /* A stale native-collapse state must not move the product navigation off-screen. */
        [data-testid="stSidebar"]:has(.ql-navigation-mode) {
            transform:none !important;
            transition:none !important;
        }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) {
            width:72px !important;
            min-width:72px !important;
            max-width:72px !important;
            transform:none !important;
        }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) > div:first-child { width:72px !important; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) [data-testid="stSidebarContent"] {
            padding:.6rem .45rem 1rem;
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] .stCaption { font-size: .88rem; line-height: 1.55; }
        .ql-brand { display:flex; align-items:center; gap:.72rem; padding:.65rem .45rem 1.25rem; }
        .ql-brand strong { display:block; font: 720 1.28rem/1.1 var(--ql-display); letter-spacing:0; }
        .ql-brand small { display:block; margin-top:.3rem; color:var(--ql-muted); font-size:.65rem; letter-spacing:.07em; text-transform:uppercase; }
        .ql-brand-mark { position:relative; width:38px; height:38px; border:1px solid var(--ql-line-strong); border-radius:50%; background:var(--ql-surface); box-shadow:inset 0 0 0 6px rgba(165,83,57,.04); }
        .ql-brand-mark i,.ql-brand-mark b,.ql-brand-mark span { position:absolute; display:block; border-radius:50%; }
        .ql-brand-mark i { width:7px; height:7px; left:8px; top:19px; background:var(--ql-pine); }
        .ql-brand-mark b { width:9px; height:9px; left:21px; top:9px; background:var(--ql-warm); }
        .ql-brand-mark span { width:5px; height:5px; left:23px; top:25px; background:var(--ql-blue); }
        .ql-brand-mark:before,.ql-brand-mark:after { content:""; position:absolute; height:1px; background:var(--ql-line-strong); transform-origin:left center; }
        .ql-brand-mark:before { width:17px; left:13px; top:20px; transform:rotate(-34deg); }
        .ql-brand-mark:after { width:14px; left:14px; top:22px; transform:rotate(23deg); }
        .ql-nav-label { padding:.2rem .58rem .55rem; color:var(--ql-muted); font-size:.67rem; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
        .ql-navigation-mode { display:none; }
        .ql-mobile-navigation { display:none; }
        .st-key-product_mobile_navigation_close,
        .st-key-product_mobile_navigation_toggle { display:none; }
        .st-key-product_navigation_mode_control { display:flex; justify-content:flex-end; margin:0 0 .45rem; }
        .st-key-product_navigation_mode_control > [data-testid="stElementContainer"] { width:44px !important; }
        .st-key-product_navigation_mode_control [data-testid="stMarkdownContainer"] { display:none; }
        .st-key-product_navigation_mode_control .stButton > button {
            display:flex;
            justify-content:center;
            align-items:center;
            min-height:38px;
            padding:.4rem;
        }
        .st-key-product_compact_navigation { display:none; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_expanded_navigation { display:none; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_compact_navigation { display:block; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .ql-brand { justify-content:center; padding:.15rem 0 .72rem; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .ql-brand > div:last-child { display:none; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_compact_navigation [data-testid="stMarkdownContainer"] { display:none; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_navigation_mode_control .stButton > button,
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_compact_navigation .stButton > button {
            display:flex;
            justify-content:center;
            align-items:center;
            min-height:43px;
            padding:.45rem;
        }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_compact_navigation .stButton { margin:.2rem 0; }
        [data-testid="stSidebar"]:has(.ql-navigation-mode-compact) .st-key-product_compact_navigation [data-testid="stIconMaterial"] { font-size:1.18rem; }
        [data-testid="stSidebar"] [role="radiogroup"] { gap:.23rem; }
        [data-testid="stSidebar"] [role="radiogroup"] label {
            min-height: 45px; padding:.48rem .68rem; border-radius:11px; border:1px solid transparent; transition:background .16s ease,border-color .16s ease,transform .16s ease;
        }
        [data-testid="stSidebar"] [role="radiogroup"] label:hover { background:var(--ql-surface-soft); border-color:var(--ql-line); transform:translateX(2px); }
        [data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background:var(--ql-warm-soft); border-color:rgba(165,83,57,.2); color:var(--ql-warm-dark); font-weight:700; }
        [data-testid="stSidebar"] [role="radiogroup"] label p { font-size:.94rem !important; }
        [data-testid="stSidebar"] [role="radiogroup"] [data-testid="stWidgetLabel"] { display:none; }
        .ql-sidebar-boundary { margin:1.1rem .35rem .25rem; padding:.85rem .8rem; border-top:1px solid var(--ql-line); color:var(--ql-muted); }
        .ql-sidebar-boundary span { color:var(--ql-pine); font-size:.72rem; font-weight:750; letter-spacing:.06em; }
        .ql-sidebar-boundary p { margin:.38rem 0 0; font-size:.72rem !important; line-height:1.55; }
        .ql-nav-utility-label { margin-top:1.1rem; }
        .ql-sidebar-attention { margin:.85rem .35rem .42rem; color:var(--ql-warm-dark); font-size:.74rem; font-weight:750; }
        h1, h2, h3, [data-testid="stHeading"] { font-family:var(--ql-display); color:var(--ql-ink); letter-spacing:0; }
        [data-testid="stHeading"] a[href^="#"],
        [data-testid="stHeading"] [aria-label*="anchor"],
        [data-testid="stHeading"] [aria-label*="Anchor"],
        [data-testid="stHeaderActionElements"],
        h1 [data-testid="stHeaderActionElements"],
        h2 [data-testid="stHeaderActionElements"],
        h3 [data-testid="stHeaderActionElements"] { display:none !important; }
        h1 { font-size:2.15rem !important; line-height:1.18 !important; font-weight:720 !important; }
        h2 { font-size:1.45rem !important; line-height:1.3 !important; font-weight:700 !important; }
        h3 { font-size:1.08rem !important; line-height:1.35 !important; }
        p, li, label, input, textarea, button { font-size:1rem; }
        [data-testid="stCaptionContainer"] { color:var(--ql-muted); font-size:.86rem; }
        .ql-workspace-head { display:grid; grid-template-columns:minmax(0,1fr) 230px; align-items:center; gap:2rem; margin:.25rem 0 1.35rem; padding:1.15rem 0 1.2rem; border-bottom:1px solid var(--ql-line); }
        .ql-workspace-head .ql-eyebrow { margin:0 0 .5rem; color:var(--ql-warm); font-size:.69rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }
        .ql-workspace-head h1 { margin:0; font-size:2.3rem !important; }
        .ql-workspace-head p { max-width:780px; margin:.62rem 0 0; color:var(--ql-ink-soft); font-size:1rem; line-height:1.7; }
        .ql-trace { position:relative; height:92px; overflow:hidden; border:1px solid var(--ql-line); border-radius:18px; background:linear-gradient(145deg,var(--ql-surface),var(--ql-surface-soft)); }
        .ql-trace:before,.ql-trace:after { content:""; position:absolute; inset:18px 20px; border:1px solid rgba(72,100,123,.22); border-radius:50%; transform:rotate(-8deg); }
        .ql-trace:after { inset:30px 44px 16px 28px; border-color:rgba(56,104,88,.24); transform:rotate(13deg); }
        .ql-trace i { position:absolute; left:29%; top:48%; width:9px; height:9px; border-radius:50%; background:var(--ql-warm); box-shadow:0 0 0 9px rgba(165,83,57,.09); animation:qlPulse 3.2s ease-in-out infinite; }
        .ql-trace b { position:absolute; right:22%; top:26%; width:6px; height:6px; border-radius:50%; background:var(--ql-pine); }
        .ql-trace span { position:absolute; right:13px; bottom:9px; color:var(--ql-muted); font-size:.58rem; font-weight:700; letter-spacing:.1em; }
        @keyframes qlPulse { 0%,100%{transform:scale(.9);box-shadow:0 0 0 7px rgba(165,83,57,.06)} 50%{transform:scale(1.08);box-shadow:0 0 0 13px rgba(165,83,57,.11)} }
        [data-testid="stMetric"] { min-width:0; padding:1rem 1rem .92rem; border:1px solid var(--ql-line); border-radius:var(--ql-radius); background:rgba(251,250,246,.85); box-shadow:var(--ql-shadow); }
        [data-testid="stMetricLabel"] p { color:var(--ql-muted); font-size:.79rem !important; font-weight:650; letter-spacing:.02em; }
        [data-testid="stMetricValue"] {
            color:var(--ql-ink); font-family:var(--ql-display); font-size:clamp(1.08rem,1.7vw,1.48rem);
            letter-spacing:0; white-space:nowrap; overflow:visible; font-variant-numeric:tabular-nums;
        }
        [data-testid="stMetricValue"] [data-testid="stMarkdownContainer"],
        [data-testid="stMetricValue"] p {
            max-width:none !important; overflow:visible !important; text-overflow:clip !important;
            white-space:nowrap !important; word-break:normal !important;
        }
        [data-testid="stAlert"] { border-radius:var(--ql-radius); border-width:1px; box-shadow:none; }
        [data-testid="stExpander"] { overflow:hidden; border:1px solid var(--ql-line) !important; border-radius:var(--ql-radius) !important; background:rgba(251,250,246,.72); }
        [data-testid="stExpander"] summary { min-height:50px; font-weight:650; }
        [data-testid="stDataFrame"], [data-testid="stTable"] { max-width:100%; overflow-x:auto; border:1px solid var(--ql-line); border-radius:var(--ql-radius); background:var(--ql-surface); }
        .stButton > button, .stDownloadButton > button { min-height:43px; border-radius:10px; border-color:var(--ql-line-strong); font-weight:650; transition:transform .12s ease,box-shadow .16s ease,border-color .16s ease; }
        .stButton > button:hover, .stDownloadButton > button:hover { border-color:var(--ql-warm); color:var(--ql-warm-dark); transform:translateY(-1px); box-shadow:0 7px 20px rgba(69,55,40,.08); }
        .stButton > button[kind="primary"] { background:var(--ql-warm); border-color:var(--ql-warm); color:white; }
        input, textarea, [data-baseweb="select"] > div { border-radius:10px !important; }
        hr { border-color:var(--ql-line) !important; }
        [data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] * { overflow-wrap:anywhere; word-break:break-word; }
        [data-testid="stHorizontalBlock"], [data-testid="stColumn"] { min-width:0 !important; }
        .ql-help-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:.85rem; margin:.5rem 0 1rem; }
        .ql-help-card { min-width:0; padding:1rem 1.05rem; border:1px solid var(--ql-line); border-radius:var(--ql-radius); background:var(--ql-surface); }
        .ql-help-card span { color:var(--ql-warm); font-size:.68rem; font-weight:800; letter-spacing:.1em; }
        .ql-help-card strong { display:block; margin:.38rem 0; font-size:1rem; }
        .ql-help-card p { margin:0; color:var(--ql-muted); font-size:.82rem; line-height:1.58; }
        .ql-section-title { display:flex; align-items:baseline; justify-content:space-between; gap:1rem; margin:1.1rem 0 .65rem; padding:.1rem 0 .58rem; border-bottom:1px solid var(--ql-line); }
        .ql-section-title span { color:var(--ql-warm); font-size:.67rem; font-weight:800; letter-spacing:.1em; }
        .ql-section-title strong { color:var(--ql-ink); font:700 1.15rem/1.25 var(--ql-display); }
        .ql-section-title em { margin-left:.32rem; color:var(--ql-muted); font:600 .78rem/1 var(--ql-font); font-style:normal; }
        .ql-state-card { display:flex; align-items:flex-start; gap:.9rem; margin:.55rem 0 1rem; padding:1rem 1.08rem; border:1px solid var(--ql-line); border-radius:var(--ql-radius); background:rgba(251,250,246,.88); }
        .ql-state-mark { flex:0 0 34px; display:grid; place-items:center; width:34px; height:34px; border-radius:50%; background:var(--ql-blue-soft); color:var(--ql-blue); font-weight:800; }
        .ql-state-card span { display:block; color:var(--ql-muted); font-size:.65rem; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }
        .ql-state-card strong { display:block; margin:.18rem 0 .2rem; font-size:1rem; }
        .ql-state-card p { margin:0; color:var(--ql-ink-soft); font-size:.86rem; line-height:1.62; }
        .ql-state-loading .ql-state-mark { animation:qlPulse 1.8s ease-in-out infinite; }
        .ql-state-degraded,.ql-state-unavailable { background:#faf5e6; border-color:#e7d7a8; }
        .ql-state-degraded .ql-state-mark,.ql-state-unavailable .ql-state-mark { background:#f1e6c8; color:#94641d; }
        .ql-state-error { background:#faf0ed; border-color:#e4c7c0; }
        .ql-state-error .ql-state-mark { background:#f2ded9; color:#91483e; }
        .ql-state-success .ql-state-mark { background:var(--ql-pine-soft); color:var(--ql-pine); }
        .ql-decision-field { position:relative; display:grid; grid-template-columns:minmax(0,1fr) 150px; align-items:center; gap:1.5rem; overflow:hidden; margin:.2rem 0 .75rem; padding:1.2rem 1.3rem; border:1px solid var(--ql-line); border-radius:var(--ql-radius-lg); background:linear-gradient(135deg,rgba(251,250,246,.94),rgba(246,242,234,.9)); box-shadow:var(--ql-shadow); }
        .ql-decision-field:before { content:""; position:absolute; width:250px; height:250px; right:-80px; top:-130px; border:1px solid rgba(72,100,123,.17); border-radius:50%; }
        .ql-decision-field span { color:var(--ql-warm); font-size:.67rem; font-weight:850; letter-spacing:.13em; }
        .ql-decision-field h2 { margin:.32rem 0 .3rem; font-size:1.45rem !important; }
        .ql-decision-field p { margin:0; color:var(--ql-muted); font-size:.86rem; }
        .ql-field-signal { position:relative; z-index:1; display:grid; place-items:center; height:82px; border-left:1px solid var(--ql-line); }
        .ql-field-signal i,.ql-field-signal b { position:absolute; border:1px solid rgba(56,104,88,.25); border-radius:50%; }
        .ql-field-signal i { width:70px; height:34px; transform:rotate(-16deg); }
        .ql-field-signal b { width:56px; height:50px; border-color:rgba(165,83,57,.2); transform:rotate(19deg); }
        .ql-field-signal em { z-index:1; color:var(--ql-ink); font:700 1.4rem/1 var(--ql-display); font-style:normal; }
        .ql-field-signal small { position:absolute; bottom:0; color:var(--ql-muted); font-size:.56rem; letter-spacing:.1em; }
        .ql-decision-alert { border-color:#dfbdb4; background:linear-gradient(135deg,#fbf5f1,#f4e6e0); }
        .ql-decision-review { border-color:#ded0ab; background:linear-gradient(135deg,#fbf8ee,#f3ecda); }
        .ql-status-rail { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:.55rem; margin:0 0 1.1rem; }
        .ql-status-rail span { padding:.64rem .72rem; border-bottom:1px solid var(--ql-line); color:var(--ql-muted); font-size:.72rem; white-space:nowrap; }
        .ql-status-rail b { color:var(--ql-ink); font-size:.92rem; font-variant-numeric:tabular-nums; }
        .ql-review-outcome { display:grid; grid-template-columns:minmax(220px,.72fr) minmax(0,1.28fr); gap:.65rem 1.35rem; margin:1rem 0 1.25rem; padding:1.05rem 0; border-block:1px solid var(--ql-line); }
        .ql-review-outcome-head { grid-row:span 2; align-self:center; }
        .ql-review-outcome-head span { color:var(--ql-pine); font-size:.67rem; font-weight:850; letter-spacing:.12em; }
        .ql-review-outcome-head strong { display:block; margin:.3rem 0 .24rem; font-size:1.08rem; }
        .ql-review-outcome-head p { margin:0; color:var(--ql-muted); font-size:.76rem; line-height:1.55; }
        .ql-review-flow { display:grid; grid-template-columns:repeat(3,minmax(0,1fr) 18px) minmax(0,1fr); align-items:center; min-width:0; }
        .ql-review-flow div { min-width:0; padding:.22rem .45rem; }
        .ql-review-flow b { display:block; color:var(--ql-ink); font:700 1.34rem/1.1 var(--ql-display); font-variant-numeric:tabular-nums; }
        .ql-review-flow span { display:block; margin-top:.2rem; color:var(--ql-muted); font-size:.68rem; white-space:nowrap; }
        .ql-review-flow i { color:var(--ql-line-strong); font-size:.75rem; font-style:normal; text-align:center; }
        .ql-review-distribution { padding:.2rem .45rem; }
        .ql-review-bar { display:flex; width:100%; height:8px; overflow:hidden; border-radius:4px; background:var(--ql-surface-soft); }
        .ql-review-bar i,.ql-review-bar b,.ql-review-bar em { display:block; height:100%; }
        .ql-review-bar i { background:var(--ql-pine); }
        .ql-review-bar b { background:var(--ql-blue); }
        .ql-review-bar em { background:var(--ql-line-strong); }
        .ql-review-distribution p { display:flex; flex-wrap:wrap; gap:.45rem 1rem; margin:.42rem 0 0; color:var(--ql-muted); font-size:.67rem; }
        .st-key-historical_demo_entry { margin:.1rem 0 1.15rem; padding:.85rem 1rem; border-block:1px solid var(--ql-line); background:rgba(246,242,234,.58); }
        .st-key-historical_demo_entry [data-testid="stHorizontalBlock"] { align-items:center; }
        .ql-demo-entry span,.ql-demo-boundary span { color:var(--ql-warm); font-size:.67rem; font-weight:850; letter-spacing:.12em; }
        .ql-demo-entry h2 { margin:.24rem 0 .2rem; font-size:1.25rem !important; }
        .ql-demo-entry p,.ql-demo-boundary p { margin:0; color:var(--ql-muted); font-size:.82rem; line-height:1.55; }
        .ql-demo-boundary { margin:.85rem 0 1rem; padding:1rem 0; border-block:1px solid var(--ql-line); }
        .ql-demo-boundary strong { display:block; margin:.28rem 0 .24rem; font-size:1.12rem; }
        .ql-demo-steps { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.55rem; margin:.4rem 0 1.25rem; }
        .ql-demo-step { display:flex; align-items:center; gap:.55rem; min-width:0; padding:.6rem .2rem; border-bottom:2px solid var(--ql-line); color:var(--ql-muted); }
        .ql-demo-step b { display:grid; place-items:center; flex:0 0 28px; width:28px; height:28px; border:1px solid var(--ql-line-strong); border-radius:50%; font-size:.68rem; font-variant-numeric:tabular-nums; }
        .ql-demo-step span { overflow:hidden; font-size:.78rem; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
        .ql-demo-step-done { border-color:var(--ql-pine); color:var(--ql-pine); }
        .ql-demo-step-done b { border-color:var(--ql-pine); background:var(--ql-pine-soft); }
        /* The roundtable is a real workspace, not another long report section. */
        .ql-roundtable-stage { position:relative; min-height:480px; overflow:hidden; margin:.3rem 0 1rem; border:1px solid var(--ql-line); border-radius:var(--ql-radius-lg); background:#f8f5ee; box-shadow:var(--ql-shadow); isolation:isolate; }
        .ql-roundtable-stage:before { content:""; position:absolute; inset:18px; z-index:-1; border:1px dashed rgba(72,100,123,.14); border-radius:18px; pointer-events:none; }
        .ql-roundtable-status { position:absolute; top:17px; left:20px; display:flex; align-items:center; gap:.48rem; color:var(--ql-muted); font-size:.74rem; font-weight:750; }
        .ql-roundtable-status span { width:8px; height:8px; border-radius:50%; background:var(--ql-pine); box-shadow:0 0 0 4px rgba(56,104,88,.10); animation:qlListening 2s ease-in-out infinite; }
        .ql-roundtable-table { position:absolute; left:50%; top:50%; display:grid; place-content:center; width:min(42vw,420px); height:154px; transform:translate(-50%,-38%); overflow:hidden; border:2px solid #8e5f42; border-radius:50%; background:#b97a53; box-shadow:inset 0 0 0 12px #d5a178, inset 0 0 0 14px rgba(65,43,31,.22), 0 18px 22px rgba(65,43,31,.14); text-align:center; }
        .ql-roundtable-table:before { content:""; position:absolute; inset:18px; border:1px solid rgba(69,44,31,.42); border-radius:50%; }
        .ql-roundtable-table i { position:absolute; left:50%; top:50%; width:78px; height:78px; transform:translate(-50%,-50%); border:1px solid rgba(255,248,233,.54); border-radius:50%; }
        .ql-roundtable-table b { position:relative; color:#fff6e8; font-size:.83rem; letter-spacing:.12em; }
        .ql-roundtable-table small { position:relative; margin-top:.25rem; color:rgba(255,246,232,.82); font-size:.64rem; letter-spacing:.04em; }
        .ql-roundtable-seat { position:absolute; display:flex; align-items:flex-start; gap:.48rem; width:min(22%,212px); min-width:142px; padding:.48rem; border:1px solid rgba(201,193,179,.9); border-radius:12px; background:rgba(251,250,246,.93); box-shadow:0 8px 20px rgba(56,48,37,.065); animation:qlSeatEnter .34s ease both; }
        .ql-roundtable-seat-0 { left:4%; top:13%; }
        .ql-roundtable-seat-1 { left:38%; top:8%; transform:translateX(-50%); }
        .ql-roundtable-seat-2 { right:4%; top:13%; }
        .ql-roundtable-seat-3 { left:4%; bottom:10%; }
        .ql-roundtable-seat-4 { left:38%; bottom:4%; transform:translateX(-50%); }
        .ql-roundtable-seat-5 { right:4%; bottom:10%; }
        .ql-roundtable-seat-6 { left:22%; top:38%; transform:translateX(-50%); }
        .ql-roundtable-seat-7 { right:22%; top:38%; transform:translateX(50%); }
        .ql-seat-copy { min-width:0; }
        .ql-seat-copy strong { display:block; color:var(--ql-ink); font-size:.77rem; line-height:1.25; }
        .ql-seat-copy small { display:block; margin:.1rem 0 .22rem; color:var(--ql-warm); font-size:.61rem; line-height:1.25; }
        .ql-seat-copy p { display:-webkit-box; overflow:hidden; margin:0; color:var(--ql-ink-soft); font-size:.67rem; line-height:1.42; -webkit-line-clamp:3; -webkit-box-orient:vertical; }
        .ql-chibi { position:relative; flex:0 0 43px; width:43px; height:55px; margin-top:1px; }
        .ql-chibi:before { content:""; position:absolute; left:7px; top:4px; width:29px; height:29px; border:1px solid rgba(45,39,32,.18); border-radius:48% 48% 45% 45%; background:#f0c7a7; }
        .ql-chibi i { position:absolute; z-index:1; left:5px; top:1px; width:33px; height:15px; border-radius:50% 50% 35% 35%; background:#3c3934; }
        .ql-chibi b { position:absolute; left:5px; bottom:1px; width:33px; height:27px; border:1px solid rgba(45,39,32,.18); border-radius:15px 15px 7px 7px; background:var(--ql-blue); }
        .ql-chibi span { position:absolute; z-index:2; left:17px; top:21px; width:3px; height:3px; border-radius:50%; background:#5a4035; box-shadow:9px 0 0 #5a4035; }
        .ql-chibi-1 i,.ql-chibi-4 i { background:#81513f; }
        .ql-chibi-1 b,.ql-chibi-4 b { background:var(--ql-pine); }
        .ql-chibi-2 i,.ql-chibi-5 i { background:#5a4a69; }
        .ql-chibi-2 b,.ql-chibi-5 b { background:var(--ql-ai); }
        .ql-chibi-3 i { background:#2d3134; }
        .ql-chibi-3 b { background:var(--ql-warm); }
        @keyframes qlSeatEnter { from { opacity:0; } to { opacity:1; } }
        @keyframes qlListening { 0%,100% { opacity:.7; } 50% { opacity:1; } }
        /* A context-aware assistant remains available everywhere without forcing a page-level chat load. */
        .st-key-global_ai_assistant { position:fixed; z-index:999; top:78px; right:22px; width:min(355px,calc(100vw - 42px)); max-height:calc(100vh - 100px); overflow:auto; padding:.7rem; border:1px solid var(--ql-line); border-radius:14px; background:rgba(251,250,246,.98); box-shadow:0 18px 42px rgba(56,48,37,.15); }
        .st-key-global_ai_assistant:has(.st-key-open_global_ai_assistant) {
            position:static;
            z-index:auto;
            top:auto;
            right:auto;
            width:auto;
            max-height:none;
            overflow:visible;
            margin:.9rem 0 0;
            padding:0;
            border:0;
            border-radius:0;
            background:transparent;
            box-shadow:none;
        }
        .st-key-global_ai_assistant [data-testid="stVerticalBlock"] { gap:.55rem; }
        .st-key-global_ai_assistant .stForm { margin:0; border:0; padding:0; background:transparent; }
        .st-key-global_ai_assistant textarea { min-height:76px !important; background:#fffdf8; }
        .ql-assistant-message { margin:.25rem 0; padding:.58rem .68rem; border:1px solid var(--ql-line); border-radius:10px; background:#fffdf8; }
        .ql-assistant-message b { display:block; margin-bottom:.22rem; color:var(--ql-ai); font-size:.68rem; }
        .ql-assistant-message p { margin:0; color:var(--ql-ink-soft); font-size:.78rem; line-height:1.5; }
        .ql-assistant-user { border-color:rgba(165,83,57,.22); background:#fcf4ef; }
        .ql-assistant-user b { color:var(--ql-warm-dark); }
        .ql-assistant-action-draft { margin:.35rem 0; padding:.62rem .7rem; border:1px solid rgba(165,83,57,.26); border-radius:10px; background:#fcf4ef; }
        .ql-assistant-action-draft b { display:block; color:var(--ql-warm-dark); font-size:.72rem; }
        .ql-assistant-action-draft p { margin:.2rem 0 0; color:var(--ql-ink-soft); font-size:.76rem; line-height:1.48; }
        .ql-research-loading { display:flex; align-items:center; gap:.85rem; margin:.55rem 0 1rem; padding:.9rem 1rem; border:1px solid rgba(107,100,135,.22); border-radius:12px; background:rgba(231,226,239,.5); color:var(--ql-ink); }
        .ql-research-loading-mark { display:flex; align-items:flex-end; gap:3px; width:29px; height:27px; }
        .ql-research-loading-mark i,.ql-research-loading-mark b,.ql-research-loading-mark em { display:block; width:7px; border-radius:3px; background:var(--ql-ai); animation:qlResearchLoading 1.05s ease-in-out infinite; }
        .ql-research-loading-mark i { height:11px; animation-delay:0s; }
        .ql-research-loading-mark b { height:20px; animation-delay:.14s; background:var(--ql-warm); }
        .ql-research-loading-mark em { height:15px; animation-delay:.28s; background:var(--ql-pine); }
        .ql-research-loading strong { display:block; font-size:.9rem; }
        .ql-research-loading p { margin:.18rem 0 0; color:var(--ql-muted); font-size:.78rem; line-height:1.5; }
        @keyframes qlResearchLoading { 0%,100% { transform:scaleY(.62); opacity:.55; } 50% { transform:scaleY(1); opacity:1; } }
        @media (max-width: 1050px) {
            html { font-size:16px; }
            .main .block-container { padding-left:1.25rem; padding-right:1.25rem; }
            .ql-workspace-head { grid-template-columns:minmax(0,1fr) 180px; }
            .ql-help-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
            .ql-status-rail { grid-template-columns:repeat(3,minmax(0,1fr)); }
            [data-testid="stHorizontalBlock"] { flex-wrap:wrap; gap:.62rem; }
            [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex:1 1 calc(50% - .35rem) !important; width:calc(50% - .35rem) !important;
                min-width:min(100%,240px) !important;
            }
            [data-testid="stMetricValue"] { font-size:1.16rem; }
            .ql-roundtable-stage { min-height:560px; }
            .ql-roundtable-table { width:330px; height:132px; }
            .ql-roundtable-seat { width:190px; }
            .ql-roundtable-seat-1,.ql-roundtable-seat-4 { left:50%; }
            .ql-roundtable-seat-6 { left:14%; }
            .ql-roundtable-seat-7 { right:14%; }
            .st-key-global_ai_assistant { position:static; width:auto; max-height:none; margin:.9rem 0 0; }
        }
        @media (max-width: 700px) {
            html { font-size:16px; }
            [data-testid="stSidebar"]:has(.ql-navigation-mode):not(:has(.ql-mobile-navigation-open)) {
                display:none !important;
            }
            [data-testid="stSidebar"]:has(.ql-mobile-navigation-open) {
                display:block !important;
                position:fixed !important;
                z-index:1002 !important;
                inset:0 auto 0 0 !important;
                width:min(88vw,300px) !important;
                min-width:0 !important;
                max-width:min(88vw,300px) !important;
                box-shadow:18px 0 42px rgba(56,48,37,.16);
            }
            [data-testid="stSidebar"]:has(.ql-mobile-navigation-open) > div:first-child {
                width:min(88vw,300px) !important;
            }
            .st-key-product_mobile_navigation_toggle {
                display:block;
                position:fixed;
                z-index:100000;
                top:.75rem;
                left:.75rem;
            }
            [data-testid="stHeader"],
            [data-testid="stHeader"] * { pointer-events:none !important; }
            .st-key-product_mobile_navigation_toggle .stButton > button {
                width:42px !important;
                min-height:42px;
                padding:.35rem;
                border-radius:12px;
                background:rgba(251,250,246,.96);
                box-shadow:0 8px 22px rgba(56,48,37,.11);
            }
            .st-key-product_mobile_navigation_toggle [data-testid="stMarkdownContainer"] {
                position:absolute !important;
                width:1px !important;
                height:1px !important;
                padding:0 !important;
                margin:-1px !important;
                overflow:hidden !important;
                clip:rect(0,0,0,0) !important;
                white-space:nowrap !important;
                border:0 !important;
            }
            [data-testid="stSidebar"]:has(.ql-mobile-navigation-open) .st-key-product_mobile_navigation_close {
                display:block;
                margin:0 0 .65rem;
            }
            [data-testid="stSidebar"]:has(.ql-mobile-navigation-open) .st-key-product_mobile_navigation_close .stButton > button {
                min-height:40px;
            }
            .main .block-container { width:100%; max-width:100%; padding:.75rem .78rem 2.5rem; }
            .ql-workspace-head { display:block; margin-top:0; padding-top:.6rem; }
            .ql-workspace-head h1 { font-size:2rem !important; }
            .ql-workspace-head p { font-size:.94rem; }
            .ql-trace { display:none; }
            .ql-help-grid { grid-template-columns:1fr; }
            .ql-decision-field { grid-template-columns:1fr; padding:1rem; }
            .ql-field-signal { display:none; }
            .ql-status-rail { grid-template-columns:repeat(2,minmax(0,1fr)); }
            .ql-status-rail span { white-space:normal; }
            .ql-review-outcome { grid-template-columns:1fr; gap:.9rem; }
            .ql-review-outcome-head { grid-row:auto; }
            .ql-review-flow { grid-template-columns:repeat(4,minmax(0,1fr)); }
            .ql-review-flow div { padding:.2rem .15rem; }
            .ql-review-flow span { white-space:normal; }
            .ql-review-flow i { display:none; }
            .ql-review-distribution { padding:.15rem; }
            .st-key-historical_demo_entry { padding:.8rem 0; }
            .ql-demo-steps { grid-template-columns:repeat(2,minmax(0,1fr)); }
            .ql-demo-step span { white-space:normal; }
            [data-testid="stHorizontalBlock"] { flex-wrap:wrap; gap:.55rem; }
            [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex:1 1 100% !important; width:100% !important; min-width:0 !important;
            }
            .stButton > button, .stDownloadButton > button { width:100%; }
            [data-baseweb="tab-list"] { overflow-x:auto; white-space:nowrap; }
            [data-testid="stMetric"] { padding:.82rem .86rem; }
            .ql-roundtable-stage { display:flex; flex-direction:column; min-height:0; gap:.55rem; padding:48px .7rem .75rem; }
            .ql-roundtable-stage:before { inset:9px; }
            .ql-roundtable-status { top:17px; left:17px; }
            .ql-roundtable-table { position:relative; left:auto; top:auto; order:2; width:min(100%,310px); height:116px; margin:.3rem auto; transform:none; }
            .ql-roundtable-seat { position:relative; left:auto !important; right:auto !important; top:auto !important; bottom:auto !important; order:1; width:100%; min-width:0; transform:none !important; }
            .ql-roundtable-seat:nth-of-type(n+6) { order:3; }
        }
        @media (prefers-reduced-motion: reduce) { *,*:before,*:after { animation:none !important; transition:none !important; scroll-behavior:auto !important; } }
        </style>
        """,
        unsafe_allow_html=True,
    )


__all__ = [
    "PRODUCT_CONTEXT_REVISION_KEY",
    "PRODUCT_DETAIL_PAGES",
    "PRODUCT_NAVIGATION_COMPACT_KEY",
    "PRODUCT_MOBILE_NAVIGATION_OPEN_KEY",
    "PRODUCT_PAGES",
    "PRODUCT_PAGE_ICONS",
    "PRODUCT_NAVIGATION_KEY",
    "PRODUCT_PAGE_KEY",
    "PRODUCT_ROUTE_PAGES",
    "PRODUCT_UTILITY_PAGES",
    "RESEARCH_CACHE_KEY",
    "RESEARCH_REQUEST_STATE_KEY",
    "ProductContext",
    "ResearchIdentity",
    "apply_product_theme",
    "bind_product_research_context",
    "cache_research_report",
    "cached_research_report",
    "clear_product_research_context",
    "consume_product_context",
    "context_matches_research",
    "has_previous_research_report",
    "mark_research_failed",
    "mark_research_loading",
    "current_product_page",
    "product_context",
    "product_navigation_page",
    "product_navigation_is_compact",
    "render_page_state",
    "render_product_navigation",
    "research_identity",
    "research_request_state",
    "research_request_key",
    "restore_previous_research_report",
    "set_product_page",
    "set_product_navigation_compact",
    "update_product_selection",
]
