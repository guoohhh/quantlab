from __future__ import annotations

import os
from datetime import date
from types import SimpleNamespace

import streamlit as st

from dashboard import product_ui
from quantlab.config import Settings
from quantlab.persistence.migrations import ensure_database_initialized


def fake_search(_settings: Settings, keyword: str, limit: int = 20) -> dict:
    del limit
    return {
        "keyword": keyword,
        "results": [
            {
                "symbol": "sh510300",
                "display_code": "510300",
                "name": "沪深300ETF",
                "industry": "宽基指数",
                "market": "SH",
            }
        ],
        "rejected_codes": [],
        "source": "frontend_e2e_fixture",
    }


def fake_analyze(_settings: Settings, symbol: str, requested_as_of: date) -> dict:
    decision = SimpleNamespace(
        symbol=symbol,
        as_of=requested_as_of,
        action="watch",
        confidence=0.68,
        model_dump=lambda mode=None: {
            "symbol": symbol,
            "as_of": requested_as_of.isoformat(),
            "action": "watch",
            "confidence": 0.68,
            "target_weight": 0.06,
            "supporting_evidence": [
                "指数覆盖面广且流动性在当前测试证据中可用；该描述只用于验证长文本、证据层级与浏览器交互，不构成买入结论。"
            ],
            "opposing_evidence": [
                "资金口径与宏观证据并不完整，组合中已经存在多类权益暴露，因此不能把单一趋势信号直接转换为确定交易动作。"
            ],
            "invalidation_conditions": [
                "行情新鲜度、交易状态、组合集中度或研究身份任一项发生变化时，必须重新运行后端交易前检查。"
            ],
        },
    )
    run = SimpleNamespace(
        run_id="frontend-e2e-research-run",
        decision=decision,
        reports={},
        forecasts=[],
        decision_trace={},
        audit_log=[],
        llm_audit={},
        learning_features={},
        learning_context={},
    )
    report = SimpleNamespace(
        model_dump=lambda mode=None: {
            "symbol": symbol,
            "as_of": requested_as_of.isoformat(),
            "regime": "sideways",
            "regime_confidence": 0.57,
            "factors": [],
            "composite_score": 0.12,
            "multi_timeframe": {},
            "pullback_reversal": {},
            "data_points": 160,
            "warnings": [],
        }
    )
    context = SimpleNamespace(
        model_dump=lambda mode=None: {
            "schema_version": "context-v1",
            "symbol": symbol,
            "as_of": requested_as_of.isoformat(),
            "quality_score": 0.81,
            "fingerprint": "frontend-e2e-context-fingerprint",
            "blocks": [],
        }
    )
    return {
        "decision_run": run,
        "report": report,
        "financial_report": None,
        "analysis_context_pack": context,
        "context_committee": None,
        "as_of": requested_as_of,
        "source": "frontend_e2e_fixture",
        "bars": 160,
        "price": 4.238,
        "degraded_sources": ["capital_flow_partial"],
        "financial_degraded_sources": [],
        "event_degraded_sources": [],
        "price_history": {},
    }


database_path = os.environ["QUANTLAB_DATABASE_PATH"]
settings = Settings.load().with_overrides(
    {
        "system": {
            "database_path": database_path,
            "data_dir": str(os.path.join(os.path.dirname(database_path), "data")),
            "test_mode": True,
        },
        "runtime": {
            "demo_directory": str(os.path.join(os.path.dirname(database_path), "demo")),
        },
        "llm": {"provider": "mock", "allow_mock_fallback": True},
    }
)
ensure_database_initialized(settings.resolve(settings.get("system.database_path")))
st.set_page_config(page_title="QuantLab frontend e2e", page_icon="📈", layout="wide")
with st.sidebar:
    st.markdown(
        """
        <div class="ql-brand">
          <div class="ql-brand-mark" aria-hidden="true"><i></i><b></b><span></span></div>
          <div><strong>QuantLab</strong><small>Isolated browser acceptance</small></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
original_search = product_ui.search_stocks
original_analyze = product_ui.analyze_symbol
try:
    product_ui.search_stocks = fake_search
    product_ui.analyze_symbol = fake_analyze
    product_ui.render_product_app(settings)
finally:
    product_ui.search_stocks = original_search
    product_ui.analyze_symbol = original_analyze
