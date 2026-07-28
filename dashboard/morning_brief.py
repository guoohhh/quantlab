"""AI morning brief card for the 今日 page.

Collects the account snapshot, pending items and data freshness (each lookup
fail-closed) and asks the configured LLM for a short Chinese brief.  When the
provider is mock/unconfigured or the call fails, the card simply does not
render — the deterministic numbers stay visible in their own sections.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import streamlit as st
from pydantic import BaseModel


class _BriefReply(BaseModel):
    brief: str = ""


def _collect_facts(settings: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    path = settings.resolve(settings.get("system.database_path"))

    try:
        from quantlab.workflows.simulator import user_simulator_repository

        repository = user_simulator_repository(settings)
        accounts = repository.accounts(include_closed=False)
        if accounts:
            overview = repository.overview(accounts[0]["account_id"])
            facts["account"] = {
                "name": accounts[0]["name"],
                "equity": float(overview["equity"]),
                "today_pnl": float(overview["today_pnl"]),
                "positions": len(overview.get("positions") or []),
            }
            facts["pending_orders"] = len(
                repository.orders(accounts[0]["account_id"], status="submitted")
            )
    except Exception:
        pass

    try:
        from quantlab.persistence import Round9Repository

        tasks = Round9Repository(path).decision_tasks(status="open", limit=20)
        facts["open_tasks"] = len(tasks)
        facts["critical_tasks"] = sum(t.get("severity") == "critical" for t in tasks)
    except Exception:
        pass

    try:
        from quantlab.persistence import NotificationRepository

        unread = NotificationRepository(path).list(unread_only=True, limit=50)
        facts["unread_alerts"] = sum(
            item.get("severity") in {"warning", "critical"} for item in unread
        )
    except Exception:
        pass

    try:
        from quantlab.persistence import DecisionRepository

        index = DecisionRepository(path).research_page(page=1, page_size=1)
        records = list(index.get("records") or index.get("items") or [])
        if records:
            latest = records[0]
            facts["latest_research"] = {
                "symbol": latest.get("symbol"),
                "action": latest.get("action"),
                "as_of": latest.get("effective_as_of"),
            }
    except Exception:
        pass

    return facts


def _generate_brief(settings: Any) -> str:
    import asyncio

    from quantlab.llm import build_provider
    from quantlab.workflows.chat import await_with_provider_close

    facts = _collect_facts(settings)
    if not facts:
        return ""
    provider = build_provider(settings.section("llm"))
    system = (
        "你是 QuantLab 的投研晨报助手。根据给出的账户与市场事实，写一段 90 字以内的"
        "中文晨报：先概括，再点出最需要注意的一两件事。语气克制、具体，"
        "不承诺收益，不编造事实里没有的数据。"
    )
    prompt = f"今天是 {date.today().isoformat()}。事实：{facts}"

    async def call() -> _BriefReply:
        return await provider.structured(system, prompt, _BriefReply)

    try:
        answer = asyncio.run(await_with_provider_close(provider, call()))
    except Exception:
        return ""
    return str(getattr(answer, "brief", "") or "").strip()


def render_morning_brief(settings: Any) -> None:
    """Render the morning brief card (skips silently when unavailable)."""

    if str(settings.get("llm.provider", "mock")).strip().lower() == "mock":
        return
    cache_key = f"morning_brief_{date.today().isoformat()}"
    brief = st.session_state.get(cache_key)
    if brief is None:
        brief = _generate_brief(settings)
        st.session_state[cache_key] = brief
    if not brief:
        return
    st.markdown(
        '<div class="ql-brief-card">'
        f'<div class="ql-brief-eyebrow">AI 晨报 · {date.today().month}月{date.today().day}日</div>'
        f"<p>{brief}</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    if st.button("重新生成晨报", key="refresh_morning_brief", icon=":material/refresh:"):
        st.session_state.pop(cache_key, None)
        st.rerun()
