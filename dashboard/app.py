from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard.product_ui import render_product_app

from quantlab.config import Settings
from quantlab.domain import ResearchProvenance
from quantlab.demo import run_demo, run_live_demo
from quantlab.llm import (
    LLM_PROFILES,
    OPENAI_MODEL_OPTIONS,
    OPENAI_ROLE_KEYS,
    REASONING_EFFORT_OPTIONS,
    ROLE_LABELS,
    apply_openai_runtime_config,
    llm_profile,
    provider_configuration_summary,
    run_llm_replay,
)
from quantlab.persistence import (
    DecisionRepository,
    HistoricalReplayRepository,
    RoundtableRepository,
    AShareUniverseRepository,
    StockRankingReplayRepository,
    TerminalRepository,
)
from quantlab.persistence.migrations import ensure_database_initialized
from quantlab.reporting import (
    audit_package_json,
    build_research_audit_package,
    build_stored_audit_package,
    prepare_historical_replay_export,
    render_research_markdown,
    render_historical_replay_markdown,
    research_persistence_context,
)
from quantlab.workflows import (
    STOCK_DISCOVERY_STYLES,
    analyze_symbol,
    build_evidence_summary,
    build_market_radar,
    build_today_brief,
    candidate_tournament_scorecard,
    export_profitability_evidence,
    generate_portfolio_plan,
    learning_status,
    paper_scorecard,
    roundtable_participant_catalog,
    run_candidate_tournament,
    run_adaptive_etf_candidate_lab,
    run_etf_variant_research,
    run_etf_walk_forward,
    run_expert_roundtable,
    run_paper_cycle,
    run_stock_paper_cycle,
    run_stock_ranking_replay,
    run_market_wide_stock_replay,
    refresh_a_share_security_master,
    run_historical_blind_replay,
    run_stock_research_batch,
    recommend_stocks,
    screen_selected_stocks,
    search_stocks,
    settle_candidate_tournaments,
)
from quantlab.workflows.radar import ETF_METADATA

# A股涨跌配色约定：涨=红、跌=绿（与欧美相反）。
# plotly 连续色阶按 [低值 -> 高值] 排列，故低值(跌)用绿、高值(涨)用红。
CN_UPDOWN_SCALE = [
    (0.0, "#0f8a4d"),   # 深绿：大跌
    (0.25, "#5fbf82"),  # 浅绿：小跌
    (0.5, "#f2efe7"),   # 中性：与米色主题呼应
    (0.75, "#e08a6f"),  # 浅红：小涨
    (1.0, "#c0392b"),   # 深红：大涨
]


st.set_page_config(
    page_title="QuantLab",
    page_icon="📈",
    layout="wide",
    # The product owns its expanded and compact navigation states.  Starting
    # with Streamlit's native sidebar collapsed strands the icon rail off-screen.
    initial_sidebar_state="expanded",
)
st.markdown(
    """
    <style>
    .main .block-container {
        max-width: 1600px;
        padding-top: 1rem;
        padding-bottom: 2rem;
    }
    [data-testid="stDataFrame"], [data-testid="stTable"] {
        overflow-x: auto;
    }
    @media (max-width: 900px) {
        .main .block-container {
            max-width: 100%;
            padding-left: 0.75rem;
            padding-right: 0.75rem;
        }
        [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
            gap: 0.5rem;
        }
        [data-testid="column"] {
            flex: 1 1 100% !important;
            width: 100% !important;
            min-width: 0 !important;
        }
        .stButton > button, .stDownloadButton > button {
            width: 100%;
        }
        [data-baseweb="tab-list"] {
            overflow-x: auto;
            white-space: nowrap;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)
base_settings = Settings.load()
experience_mode = st.session_state.get("quantlab_experience_mode", "product")

# The product workspace owns its navigation, settings, and lightweight read
# paths.  Branch before the legacy audit sidebar is built: rendering that
# sidebar first created a second brand/navigation layer and initialized audit
# configuration on every ordinary product-page rerun.
if experience_mode == "product":
    render_product_app(base_settings)
    st.stop()

with st.sidebar:
    st.markdown(
        """
        <div class="ql-brand">
          <div class="ql-brand-mark" aria-hidden="true"><i></i><b></b><span></span></div>
          <div><strong>QuantLab</strong><small>Evidence-led investing</small></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    profile_name = "平衡"
    profile = llm_profile(profile_name)
    default_model = profile["default_model"]
    default_effort = profile["default_effort"]
    role_models = dict(profile["role_models"])
    role_efforts = dict(profile["role_efforts"])
    if experience_mode == "audit":
        st.caption("工程审计视图 · 从专业空间进入")
        if st.button("返回投资工作区", key="return_to_product_workspace", width="stretch"):
            st.session_state["quantlab_experience_mode"] = "product"
            st.rerun()
        st.header("LLM 运行配置")
        profile_name = st.selectbox("配置预设", list(LLM_PROFILES), index=1)
        profile = llm_profile(profile_name)
        default_model = st.selectbox(
            "默认 GPT 模型",
            OPENAI_MODEL_OPTIONS,
            index=OPENAI_MODEL_OPTIONS.index(profile["default_model"]),
            key=f"default_model_{profile_name}",
        )
        default_effort = st.selectbox(
            "默认推理强度",
            REASONING_EFFORT_OPTIONS,
            index=REASONING_EFFORT_OPTIONS.index(profile["default_effort"]),
            key=f"default_effort_{profile_name}",
        )
        custom_model = st.text_input("自定义模型名（可选）", key=f"custom_model_{profile_name}")
        if custom_model.strip():
            default_model = custom_model.strip()
        role_models = dict(profile["role_models"])
        role_efforts = dict(profile["role_efforts"])
        advanced = st.toggle("按 Agent 高级配置", value=False)
        if advanced:
            with st.expander("逐角色模型与推理强度", expanded=True):
                for role in OPENAI_ROLE_KEYS:
                    st.caption(ROLE_LABELS[role])
                    model_col, effort_col = st.columns(2)
                    role_models[role] = model_col.selectbox(
                        "模型",
                        OPENAI_MODEL_OPTIONS,
                        index=OPENAI_MODEL_OPTIONS.index(role_models[role]),
                        key=f"role_model_{profile_name}_{role}",
                        label_visibility="collapsed",
                    )
                    role_efforts[role] = effort_col.selectbox(
                        "推理强度",
                        REASONING_EFFORT_OPTIONS,
                        index=REASONING_EFFORT_OPTIONS.index(role_efforts[role]),
                        key=f"role_effort_{profile_name}_{role}",
                        label_visibility="collapsed",
                    )
        st.divider()
        st.caption("API Key 仅从本地 .env 读取，前端不会显示、回传或写入报告。")
        st.warning("系统仅生成研究与手工下单建议，不连接券商，也不承诺收益。")

settings = apply_openai_runtime_config(
    base_settings,
    default_model=default_model,
    default_effort=default_effort,
    role_models=role_models,
    role_efforts=role_efforts,
)
database_path = settings.resolve(settings.get("system.database_path"))
ensure_database_initialized(database_path)
terminal = TerminalRepository(database_path)
decision_repository = DecisionRepository(database_path)
replay_repository = HistoricalReplayRepository(database_path)
stock_replay_repository = StockRankingReplayRepository(database_path)
universe_repository = AShareUniverseRepository(database_path)
roundtable_repository = RoundtableRepository(database_path)


def render_radar(radar: dict) -> None:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("市场状态", radar["market_regime"])
    col2.metric("风险偏好", radar["risk_appetite"], f"{radar['risk_appetite_score']:+.2f}")
    col3.metric("20日上涨宽度", f"{radar['breadth']['positive_20']:.0%}")
    col4.metric("20日离散度", f"{radar['dispersion_20_pct']:.2f}%")

    frame = pd.DataFrame(radar["instruments"])
    display_columns = [
        "name",
        "symbol",
        "category",
        "price",
        "return_20_pct",
        "return_60_pct",
        "return_120_pct",
        "volatility_20_pct",
        "strength_score",
        "trend",
    ]
    labels = {
        "name": "名称",
        "symbol": "代码",
        "category": "类别",
        "price": "价格",
        "return_20_pct": "20日%",
        "return_60_pct": "60日%",
        "return_120_pct": "120日%",
        "volatility_20_pct": "20日年化波动%",
        "strength_score": "强弱分",
        "trend": "趋势",
    }
    st.dataframe(frame[display_columns].rename(columns=labels), width="stretch", hide_index=True)

    heat = frame.set_index("name")[["return_20_pct", "return_60_pct", "return_120_pct"]]
    heat.columns = ["20日", "60日", "120日"]
    figure = px.imshow(
        heat,
        text_auto=".1f",
        aspect="auto",
        color_continuous_scale=CN_UPDOWN_SCALE,
        color_continuous_midpoint=0,
        labels={"color": "收益率%"},
        title="跨资产动量热力图",
    )
    st.plotly_chart(figure, width="stretch")

    scatter = px.scatter(
        frame,
        x="volatility_20_pct",
        y="return_60_pct",
        size="strength_score",
        color="category",
        hover_name="name",
        text="symbol",
        labels={"volatility_20_pct": "20日年化波动%", "return_60_pct": "60日收益%"},
        title="风险—收益—相对强弱",
    )
    scatter.update_traces(textposition="top center")
    st.plotly_chart(scatter, width="stretch")

    if radar.get("sectors"):
        st.subheader("行业热度快照")
        sectors = pd.DataFrame(radar["sectors"])
        st.dataframe(sectors, width="stretch", hide_index=True)
        sector_figure = px.bar(
            sectors.sort_values("heat_score"),
            x="heat_score",
            y="name",
            orientation="h",
            color="change_pct",
            color_continuous_scale=CN_UPDOWN_SCALE,
            title="行业热度（仅当前快照，不冒充历史信号）",
        )
        st.plotly_chart(sector_figure, width="stretch")

    if radar["degraded_sources"]:
        st.warning("数据降级：\n- " + "\n- ".join(radar["degraded_sources"]))
    st.caption(
        f"数据日期 {radar['as_of']} · 来源 {radar['source']} · "
        f"覆盖 {radar['coverage']['available']}/{radar['coverage']['requested']}"
    )


def render_candidate_tournament(tournament: dict) -> None:
    candidates = pd.DataFrame(tournament["candidates"])
    ranking_columns = [
        "tournament_rank",
        "name",
        "symbol",
        "tournament_score",
        "action",
        "confidence",
        "reviewer_approved",
        "veto_triggered",
        "diversification_status",
    ]
    ranking_labels = {
        "tournament_rank": "排名",
        "name": "名称",
        "symbol": "代码",
        "tournament_score": "擂台分",
        "action": "动作",
        "confidence": "置信度",
        "reviewer_approved": "Reviewer通过",
        "veto_triggered": "触发否决",
        "diversification_status": "组合筛选状态",
    }
    st.subheader("同标准横向排名")
    st.dataframe(
        candidates.reindex(columns=ranking_columns).rename(columns=ranking_labels),
        width="stretch",
        hide_index=True,
    )

    correlation = pd.DataFrame(tournament["correlation_matrix"], dtype=float)
    if not correlation.empty:
        st.subheader("252日收益相关性")
        figure = px.imshow(
            correlation,
            text_auto=".2f",
            zmin=-1,
            zmax=1,
            color_continuous_scale="RdBu_r",
            aspect="auto",
        )
        st.plotly_chart(figure, width="stretch")

    shortlist = tournament["diversified_shortlist"]
    st.subheader("通过复核且去重后的候选")
    if shortlist:
        st.dataframe(
            pd.DataFrame(shortlist).reindex(columns=ranking_columns).rename(columns=ranking_labels),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("本轮没有同时通过 Reviewer、否决闸门、人工复核要求和相关性约束的候选。")

    st.warning("下方 30% 等权组合只用于候选比较和压力测试，不是下单计划。")
    stress = tournament["stress_test"]
    historical = stress["historical_risk"]
    risk_cols = st.columns(4)
    risk_cols[0].metric(
        "年化波动率",
        f"{historical['annualized_volatility']:.2%}"
        if historical["annualized_volatility"] is not None
        else "样本不足",
    )
    risk_cols[1].metric(
        "单日 VaR 95%",
        f"{historical['one_day_var_95_pct']:.2%}"
        if historical["one_day_var_95_pct"] is not None
        else "样本不足",
    )
    risk_cols[2].metric(
        "单日 CVaR 95%",
        f"{historical['one_day_cvar_95_pct']:.2%}"
        if historical["one_day_cvar_95_pct"] is not None
        else "样本不足",
    )
    risk_cols[3].metric(
        "历史最大回撤",
        f"{historical['maximum_historical_drawdown']:.2%}"
        if historical["maximum_historical_drawdown"] is not None
        else "样本不足",
    )
    if historical["missing_symbols"]:
        st.warning("缺少历史风险行情：" + "、".join(historical["missing_symbols"]))

    scenario_frame = pd.DataFrame(stress["scenarios"])
    st.subheader("透明情景冲击")
    st.dataframe(
        scenario_frame.reindex(
            columns=["scenario", "portfolio_return", "pnl_amount", "ending_equity"]
        ).rename(
            columns={
                "scenario": "情景",
                "portfolio_return": "组合冲击收益",
                "pnl_amount": "盈亏金额",
                "ending_equity": "冲击后权益",
            }
        ),
        width="stretch",
        hide_index=True,
    )
    if historical["variance_contribution"]:
        st.subheader("方差风险贡献")
        st.dataframe(
            pd.DataFrame(
                [
                    {"symbol": symbol, "variance_contribution": contribution}
                    for symbol, contribution in historical["variance_contribution"].items()
                ]
            ).rename(columns={"symbol": "代码", "variance_contribution": "风险贡献"}),
            width="stretch",
            hide_index=True,
        )


def render_stock_discovery(output: dict, key_prefix: str) -> None:
    st.caption(
        f"数据日期 {output['as_of']} · 候选池 {output['universe_size']} 只 · "
        f"来源 {output['source']}"
    )
    if output["degraded_sources"]:
        st.warning("数据降级：\n- " + "\n- ".join(output["degraded_sources"]))
    candidates = pd.DataFrame(output["candidates"])
    if candidates.empty:
        st.info("当前条件下没有可展示候选；系统不会为了凑推荐数量制造股票。")
        return
    columns = [
        "screen_rank",
        "name",
        "display_code",
        "industry",
        "screen_score",
        "recommendation_tier",
        "price",
        "return_20_pct",
        "return_60_pct",
        "volatility_20_pct",
        "factor_composite",
        "financial_snapshot_coverage",
        "diversification_status",
    ]
    labels = {
        "screen_rank": "排名",
        "name": "名称",
        "display_code": "代码",
        "industry": "行业",
        "screen_score": "研究优先分",
        "recommendation_tier": "研究层级",
        "price": "价格",
        "return_20_pct": "20日%",
        "return_60_pct": "60日%",
        "volatility_20_pct": "20日年化波动%",
        "factor_composite": "因子综合",
        "financial_snapshot_coverage": "财务快照字段",
        "diversification_status": "去重状态",
    }
    st.dataframe(
        candidates.reindex(columns=columns).rename(columns=labels),
        width="stretch",
        hide_index=True,
    )
    shortlist = output["diversified_shortlist"]
    st.subheader("分散化研究 shortlist")
    if shortlist:
        st.dataframe(
            pd.DataFrame(shortlist).reindex(columns=columns).rename(columns=labels),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("没有候选同时满足基本资格、研究分层与相关性要求。")
    correlation = pd.DataFrame(output["correlation_matrix"], dtype=float)
    if not correlation.empty:
        figure = px.imshow(
            correlation,
            text_auto=".2f",
            zmin=-1,
            zmax=1,
            color_continuous_scale="RdBu_r",
            aspect="auto",
            title="候选股票252日收益相关性",
        )
        st.plotly_chart(figure, width="stretch", key=f"{key_prefix}_stock_corr")
    with st.expander("查看每只股票的评分依据与风险"):
        for item in output["candidates"]:
            st.markdown(
                f"**{item.get('name', item['symbol'])} ({item['symbol']}) · "
                f"{item.get('screen_score', 0):.1f}分**"
            )
            st.json(
                {
                    "score_trace": item.get("score_trace", {}),
                    "reasons": item.get("reasons", []),
                    "risks": item.get("risks", []),
                    "styles": item.get("style_sources", []),
                    "full_research_required": item.get("full_research_required", True),
                }
            )
    st.warning("这里的“推荐”表示优先研究候选，不是买入建议或订单。")


def render_stock_ranking_replay(output: dict, key_prefix: str) -> None:
    cols = st.columns(4)
    cols[0].metric("有效回合", output["completed_episodes"])
    cols[1].metric("证据等级", output["evidence_status"])
    cols[2].metric(
        "系统第一名累计收益",
        f"{output['metrics']['system_top_rank']['total_return']:.2%}",
    )
    cols[3].metric(
        "排名IC均值",
        f"{output['ranking_metrics']['mean_rank_information_coefficient']:.3f}"
        if output["ranking_metrics"]["mean_rank_information_coefficient"] is not None
        else "样本不足",
    )
    metric_frame = pd.DataFrame.from_dict(output["metrics"], orient="index").reset_index(
        names="组合"
    )
    st.subheader("同成交规则组合比较")
    st.dataframe(
        metric_frame.reindex(
            columns=[
                "组合",
                "episodes_with_trades",
                "total_return",
                "average_episode_return",
                "episode_win_rate",
                "max_drawdown",
                "final_equity",
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    comparison_frame = pd.DataFrame(output["paired_comparisons"].values())
    st.subheader("系统策略相对对应基准的配对统计")
    st.dataframe(comparison_frame, width="stretch", hide_index=True)
    if output.get("strategy_admission"):
        admission = output["strategy_admission"]
        if admission.get("passed"):
            st.success(f"策略准入通过：{admission.get('preferred_variant')}")
        else:
            st.error("策略准入未通过：仅限研究，不应晋级为实盘推荐。")
        st.json(admission)
    if output.get("survivorship_audit"):
        survivorship = output["survivorship_audit"]
        st.subheader("幸存者偏差与历史状态审计")
        audit_cols = st.columns(3)
        audit_cols[0].metric(
            "最终退市样本观测",
            survivorship.get("sampled_eventual_delisted_observations", 0),
        )
        audit_cols[1].metric(
            "持有期内退市",
            survivorship.get("delisted_within_horizon_observations", 0),
        )
        audit_cols[2].metric(
            "历史ST状态排除",
            survivorship.get("historical_st_exclusions", 0),
        )
        st.json(output.get("evidence_qualification", {}))
    episode_rows = []
    for episode in output["episodes"]:
        episode_rows.append(
            {
                "signal_date": episode["signal_date"],
                "top_ranked_symbol": episode["top_ranked_symbol"],
                "simple_momentum_symbol": episode["simple_momentum_symbol"],
                "system_return": episode["trades"]["system_top_rank"]["net_return"],
                "momentum_return": episode["trades"]["simple_momentum"]["net_return"],
                "pool_return": episode["trades"]["pool_equal_weight"]["net_return"],
                "benchmark_return": episode["trades"]["benchmark_hs300"]["net_return"],
                "diversified_return": episode["trades"]["system_diversified_top_k"][
                    "net_return"
                ],
                "same_exposure_benchmark_return": episode["trades"].get(
                    "benchmark_hs300_multi_name", {}
                ).get("net_return"),
                "rank_ic": episode["rank_information_coefficient"],
            }
        )
    with st.expander("查看逐回合点时结果"):
        st.dataframe(episode_rows, width="stretch", hide_index=True)
    if output["degraded_sources"]:
        st.warning("数据降级：\n- " + "\n- ".join(output["degraded_sources"]))
    st.caption(output["claim_boundary"])
    st.download_button(
        "下载A股排名回放JSON",
        audit_package_json(output).encode("utf-8"),
        file_name=f"stock-ranking-replay-{output.get('replay_id', 'unsaved')}.json",
        mime="application/json",
        key=f"{key_prefix}_download",
    )


def render_research(package: dict, key_prefix: str) -> None:
    decision = package["decision"]
    reviewer = package["agent_reports"].get("reviewer", {})
    council = package["agent_reports"].get("council", {})
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("最终动作", decision["action"])
    col2.metric("决策置信度", f"{decision['confidence']:.1%}")
    col3.metric("目标权重", f"{decision['target_weight']:.1%}")
    col4.metric("Reviewer", reviewer.get("status", "unknown"))

    if decision.get("requires_human_review"):
        st.warning("Reviewer 或确定性风控要求人工复核；目标仓位已按策略强制处理。")
    degraded = package["data"].get("degraded_sources", [])
    if degraded:
        st.warning("数据降级：\n- " + "\n- ".join(degraded))

    decision_tab, council_tab, forecast_tab, trace_tab, audit_tab = st.tabs(
        ["决策卡", "Agent 委员会", "概率预测", "可复算轨迹", "模型与审计"]
    )
    with decision_tab:
        st.json(decision)
        st.subheader("Reviewer 复核")
        st.json(reviewer)
        if package.get("factor_report"):
            with st.expander("量化因子报告"):
                st.json(package["factor_report"])
        if package.get("financial_report"):
            valuation = package["financial_report"].get("valuation") or {}
            if valuation:
                st.subheader("确定性保守估值区间")
                valuation_cols = st.columns(4)
                valuation_cols[0].metric("估值状态", valuation.get("status", "unavailable"))
                valuation_cols[1].metric(
                    "保守下沿",
                    f"¥{valuation['lower_value']:.2f}"
                    if valuation.get("lower_value") is not None
                    else "数据不足",
                )
                valuation_cols[2].metric(
                    "中枢价值",
                    f"¥{valuation['fair_value']:.2f}"
                    if valuation.get("fair_value") is not None
                    else "数据不足",
                )
                valuation_cols[3].metric(
                    "安全边际",
                    f"{valuation['margin_of_safety_pct']:.1f}%"
                    if valuation.get("margin_of_safety_pct") is not None
                    else "无法计算",
                )
            with st.expander("财务质量报告"):
                st.json(package["financial_report"])

    with council_tab:
        st.write(council.get("summary", ""))
        opinions = pd.DataFrame(council.get("opinions", []))
        if not opinions.empty:
            columns = [
                "role",
                "perspective",
                "stance",
                "score",
                "confidence",
                "weight",
                "mode",
                "veto",
            ]
            st.dataframe(opinions[columns], width="stretch", hide_index=True)
            selected_role = st.selectbox(
                "查看专家完整依据",
                opinions["role"].tolist(),
                key=f"{key_prefix}_opinion_{package['run_id']}",
            )
            st.json(opinions[opinions["role"] == selected_role].iloc[0].to_dict())

    with forecast_tab:
        forecasts = pd.DataFrame(package["forecasts"])
        if not forecasts.empty:
            forecast_columns = [
                "horizon_days",
                "up_probability",
                "flat_probability",
                "down_probability",
                "expected_return_pct",
                "lower_return_pct",
                "upper_return_pct",
                "statistical_weight",
                "model_provider",
                "model",
            ]
            for column in forecast_columns:
                if column not in forecasts:
                    forecasts[column] = None
            probability_frame = forecasts[
                ["horizon_days", "up_probability", "flat_probability", "down_probability"]
            ].melt("horizon_days", var_name="direction", value_name="probability")
            probability_figure = px.bar(
                probability_frame,
                x="horizon_days",
                y="probability",
                color="direction",
                barmode="stack",
                range_y=[0, 1],
                title="5日 / 20日方向概率",
            )
            st.plotly_chart(probability_figure, width="stretch")
            st.dataframe(
                forecasts[forecast_columns],
                width="stretch",
                hide_index=True,
            )

    with trace_tab:
        st.json(package["decision_trace"])

    with audit_tab:
        st.subheader("LLM 实际路由与用量")
        st.json(package["llm_audit"])
        st.subheader("执行审计链")
        st.dataframe(package["audit_log"], width="stretch", hide_index=True)

    markdown = render_research_markdown(package)
    json_text = audit_package_json(package)
    left, right = st.columns(2)
    left.download_button(
        "下载 Markdown 研究报告",
        markdown.encode("utf-8"),
        file_name=f"quantlab-{package['symbol']}-{package['as_of']}.md",
        mime="text/markdown",
        key=f"{key_prefix}_download_md_{package['run_id']}",
    )
    right.download_button(
        "下载 JSON 审计包",
        json_text.encode("utf-8"),
        file_name=f"quantlab-{package['symbol']}-{package['as_of']}.json",
        mime="application/json",
        key=f"{key_prefix}_download_json_{package['run_id']}",
    )


def render_roundtable(session: dict, key_prefix: str) -> None:
    synthesis = session["synthesis"]
    metric_cols = st.columns(4)
    metric_cols[0].metric("标的", session["symbol"])
    metric_cols[1].metric("参与专家", len(session["participants"]))
    metric_cols[2].metric("讨论轮数", session["rounds"])
    metric_cols[3].metric("运行状态", session["status"])
    st.info(
        "圆桌会议属于探索研究层：只提供启发、分歧和待验证问题，"
        "不会修改正式决策、仓位、风控限制或订单。"
    )

    summary_tab, transcript_tab, evidence_tab, audit_tab = st.tabs(
        ["主持人总结", "逐轮发言", "证据缺口", "运行审计"]
    )
    with summary_tab:
        st.subheader("主持人总结")
        st.write(synthesis["summary"])
        st.caption(f"与原决策关系：{synthesis['decision_relevance']}")
        left, right = st.columns(2)
        with left:
            st.markdown("#### 形成的共识")
            for item in synthesis.get("consensus_points", []):
                st.write(f"- {item}")
            st.markdown("#### 最强看多论证")
            for item in synthesis.get("strongest_bull_case", []):
                st.write(f"- {item}")
        with right:
            st.markdown("#### 尚未解决的分歧")
            for item in synthesis.get("unresolved_disagreements", []):
                st.write(f"- {item}")
            st.markdown("#### 最强看空论证")
            for item in synthesis.get("strongest_bear_case", []):
                st.write(f"- {item}")
        if synthesis.get("questions_for_user"):
            st.markdown("#### 建议用户继续思考的问题")
            for item in synthesis["questions_for_user"]:
                st.write(f"- {item}")
        if synthesis.get("recommended_next_steps"):
            st.markdown("#### 下一步研究动作")
            for item in synthesis["recommended_next_steps"]:
                st.write(f"- {item}")

    with transcript_tab:
        labels = session.get("participant_labels", {})
        for round_number in range(1, int(session["rounds"]) + 1):
            st.subheader(f"第 {round_number} 轮")
            for turn in session["turns"]:
                if int(turn["round_number"]) != round_number:
                    continue
                label = labels.get(turn["participant"], turn["participant_label"])
                title = f"{label} · {turn['stance']} · 置信度 {float(turn['confidence']):.0%}"
                with st.expander(title, expanded=round_number == 1):
                    st.write(turn["statement"])
                    if turn.get("agreements"):
                        st.write("赞同：", turn["agreements"])
                    if turn.get("challenges"):
                        st.write("质疑：", turn["challenges"])
                    if turn.get("evidence_refs"):
                        st.write("证据引用：", turn["evidence_refs"])
                    if turn.get("questions"):
                        st.write("追问：", turn["questions"])
                    if turn.get("changed_view"):
                        st.caption("该角色在本轮明确修正了观点。")

    with evidence_tab:
        st.markdown("#### 综合证据缺口")
        for item in synthesis.get("evidence_gaps", []):
            st.write(f"- {item}")
        with st.expander("查看圆桌使用的冻结研究快照"):
            st.json(session.get("source_snapshot", {}))

    with audit_tab:
        audit_frame = pd.DataFrame(session.get("audit_log", []))
        if not audit_frame.empty:
            st.dataframe(audit_frame, width="stretch", hide_index=True)
        with st.expander("LLM 路由与调用审计"):
            st.json(session.get("llm_audit", {}))


def render_paper_scorecard(scorecard: dict, key_prefix: str) -> None:
    accounts = pd.DataFrame(scorecard.get("accounts", []))
    if accounts.empty:
        st.info("模拟盘尚未开始。首次运行只会冻结信号并生成次日待成交单。")
        return
    display_columns = [
        "label",
        "policy",
        "latest_equity",
        "total_return",
        "annualized_return",
        "sharpe",
        "max_drawdown",
        "turnover_count",
        "snapshots",
        "pending_orders",
    ]
    for column in display_columns:
        if column not in accounts:
            accounts[column] = None
    st.dataframe(accounts[display_columns], width="stretch", hide_index=True)
    curves = []
    labels = {item["account_id"]: item["label"] for item in scorecard["accounts"]}
    for account_id, points in scorecard.get("curves", {}).items():
        for point in points:
            curves.append(
                {
                    "date": point["date"],
                    "equity": point["equity"],
                    "account": labels.get(account_id, account_id),
                }
            )
    if curves:
        curve_frame = pd.DataFrame(curves)
        curve_figure = px.line(
            curve_frame,
            x="date",
            y="equity",
            color="account",
            markers=True,
            title="不可回写的前瞻模拟净值",
        )
        st.plotly_chart(curve_figure, width="stretch", key=f"{key_prefix}_paper_curve")
    latest_run = scorecard.get("latest_run")
    if latest_run:
        st.caption(
            f"最近周期：{latest_run['as_of']} · {latest_run['status']} · "
            "信号在收盘冻结，成交使用之后首个可用开盘价"
        )


def render_evidence(evidence: dict) -> None:
    assessment = evidence.get("profitability_assessment", {})
    st.subheader("盈利能力证据等级")
    grade_col, score_col, admission_col = st.columns(3)
    grade_col.metric("证据等级", assessment.get("grade", "missing"))
    score_col.metric("证据得分", f"{assessment.get('score', 0)}/100")
    admission_col.metric("策略准入", "通过" if assessment.get("admission_passed") else "未通过")
    if assessment.get("dimensions"):
        st.dataframe(
            [
                {
                    "dimension": name,
                    "points": item.get("points"),
                    "maximum": item.get("maximum"),
                    "passed": item.get("passed"),
                }
                for name, item in assessment["dimensions"].items()
            ],
            width="stretch",
            hide_index=True,
        )
    if assessment.get("blockers"):
        st.warning("仍需补齐：" + "、".join(assessment["blockers"]))
    st.caption(assessment.get("claim_boundary", ""))
    recommendation = evidence.get("evidence_first_portfolio", {})
    if recommendation:
        st.subheader("证据优先可投资政策")
        selected = recommendation.get("selected_metrics", {})
        policy_cols = st.columns(4)
        policy_cols[0].metric("默认政策", recommendation.get("selected_policy", "missing"))
        if recommendation.get("metric_scope") == "production_protocol_history":
            policy_cols[1].metric("同协议历史收益", f"{selected.get('total_return', 0):.2%}")
            policy_cols[2].metric("历史 Sharpe", f"{selected.get('sharpe', 0):.3f}")
            policy_cols[3].metric("最大回撤", f"{selected.get('max_drawdown', 0):.2%}")
        else:
            policy_cols[1].metric("成本后 OOS 收益", f"{selected.get('compounded_return', 0):.2%}")
            policy_cols[2].metric("平均 Sharpe", f"{selected.get('mean_sharpe', 0):.3f}")
            policy_cols[3].metric("正收益折比例", f"{selected.get('positive_fold_rate', 0):.1%}")
        st.write(recommendation.get("reason", ""))
        st.caption(recommendation.get("claim_boundary", ""))
    production_core = evidence.get("production_core_validation", {})
    if production_core.get("status") != "missing":
        st.subheader("ETF 核心生产协议证据")
        st.json(production_core)
    strategy = evidence["strategy_validation"]
    st.subheader("历史策略证据")
    st.write(strategy.get("statement", ""))
    if strategy.get("selected_oos"):
        strategy_col, benchmark_col = st.columns(2)
        strategy_col.json(strategy["selected_oos"])
        benchmark_col.json(strategy.get("benchmark_oos", {}))
        if strategy.get("relative_to_benchmarks"):
            st.subheader("相对基准增量")
            st.dataframe(
                [
                    {"benchmark": name, **metrics}
                    for name, metrics in strategy["relative_to_benchmarks"].items()
                ],
                width="stretch",
                hide_index=True,
            )
        if strategy.get("admission"):
            st.subheader("策略准入结论")
            st.json(strategy["admission"])
        if strategy.get("research_candidates"):
            st.subheader("仅供下一阶段验证的候选配置")
            st.warning(strategy["candidate_warning"])
            st.dataframe(strategy["research_candidates"], width="stretch", hide_index=True)
    candidate = evidence.get("adaptive_strategy_candidate", {})
    st.subheader("自适应 ETF 研究候选")
    st.write(candidate.get("statement", ""))
    if candidate.get("status") != "missing":
        selected = candidate.get("selected_candidate", {})
        holdout = candidate.get("holdout", {})
        metrics = holdout.get("selected_candidate_metrics", {})
        row = st.columns(4)
        row[0].metric("开发期冠军", selected.get("name", "unknown"))
        row[1].metric("留出收益", f"{metrics.get('total_return', 0):.2%}")
        row[2].metric("留出 Sharpe", f"{metrics.get('sharpe', 0):.2f}")
        row[3].metric("留出最大回撤", f"{metrics.get('max_drawdown', 0):.2%}")
        st.json(
            {
                "status": candidate.get("status"),
                "research_only": candidate.get("research_only"),
                "formal_strategy_changed": candidate.get("formal_strategy_changed"),
                "relative_to_benchmarks": holdout.get("relative_to_benchmarks"),
                "admission": holdout.get("admission"),
            }
        )
        st.caption(candidate.get("claim_boundary", ""))
    v2_diagnostic = evidence.get("adaptive_v2_diagnostic", {})
    st.subheader("Adaptive ETF V2 回顾性诊断")
    st.write(v2_diagnostic.get("statement", ""))
    if v2_diagnostic.get("status") != "missing":
        v2_metrics = v2_diagnostic.get("metrics", {})
        strategy_metrics = v2_metrics.get("strategy", {})
        v2_row = st.columns(4)
        v2_row[0].metric("V2 收益", f"{strategy_metrics.get('total_return', 0):.2%}")
        v2_row[1].metric("V2 Sharpe", f"{strategy_metrics.get('sharpe', 0):.2f}")
        v2_row[2].metric("V2 最大回撤", f"{strategy_metrics.get('max_drawdown', 0):.2%}")
        v2_row[3].metric("V2 成交次数", f"{strategy_metrics.get('turnover_count', 0):.0f}")
        st.json(
            {
                "period": v2_diagnostic.get("period"),
                "relative_to_equal_weight": v2_diagnostic.get("relative_to_equal_weight"),
                "two_x_cost": v2_metrics.get("two_x_cost"),
                "research_only": v2_diagnostic.get("research_only"),
            }
        )
        st.caption(v2_diagnostic.get("claim_boundary", ""))
    robustness_audit = evidence.get("strategy_robustness_audit", {})
    st.subheader("ETF 多轮稳健性审查")
    if robustness_audit.get("status") != "missing":
        v2_stability = robustness_audit.get("stability", {}).get("adaptive_v2_full", {})
        audit_row = st.columns(4)
        audit_row[0].metric("滚动收益胜率", f"{v2_stability.get('rolling_return_win_rate', 0):.1%}")
        audit_row[1].metric(
            "滚动Sharpe胜率", f"{v2_stability.get('rolling_sharpe_win_rate', 0):.1%}"
        )
        audit_row[2].metric(
            "滚动回撤胜率", f"{v2_stability.get('rolling_drawdown_win_rate', 0):.1%}"
        )
        bootstrap_range = robustness_audit.get("adaptive_v2_repeated_bootstrap", {}).get(
            "probability_alpha_positive_range", [0, 0]
        )
        audit_row[3].metric("Alpha为正概率", f"{bootstrap_range[0]:.1%}–{bootstrap_range[-1]:.1%}")
        st.json(robustness_audit.get("diagnosis", {}))
    else:
        st.info(robustness_audit.get("statement", ""))
    v3_candidate = evidence.get("adaptive_v3_candidate", {})
    st.subheader("Adaptive ETF V3 候选")
    if v3_candidate.get("status") != "missing":
        st.json(
            {
                "status": v3_candidate.get("status"),
                "v3_vs_v2": v3_candidate.get("v3_vs_v2"),
                "exploratory_screen": v3_candidate.get("exploratory_screen"),
                "claim_boundary": v3_candidate.get("claim_boundary"),
            }
        )
    else:
        st.info(v3_candidate.get("statement", ""))
    stock_v3 = evidence.get("a_share_strategy_v3", {})
    st.subheader("A 股市场状态策略 V3 冻结验证")
    if stock_v3.get("status") != "missing":
        validation = stock_v3.get("validation_result", {})
        stock_row = st.columns(5)
        stock_row[0].metric("验证收益", f"{validation.get('total_return', 0):.2%}")
        stock_row[1].metric(
            "同仓位沪深300", f"{validation.get('benchmark_total_return', 0):.2%}"
        )
        stock_row[2].metric("Rank IC", f"{validation.get('mean_rank_ic', 0):.3f}")
        stock_row[3].metric("最大回撤", f"{validation.get('max_drawdown', 0):.2%}")
        stock_row[4].metric(
            "超额为正概率",
            f"{validation.get('paired_comparison', {}).get('probability_mean_excess_positive', 0):.1%}",
        )
        if stock_v3.get("locked_holdout_ready"):
            st.success("V3 验证通过，可以冻结后打开一次锁定留出。")
        else:
            st.warning(
                "V3 在冻结验证上取得正收益和正超额，但统计置信度未达预注册门槛；"
                "A 股策略保持研究候选，不进入默认订单预算。"
            )
        st.json(validation.get("admission", {}))
        st.caption(stock_v3.get("claim_boundary", ""))
    else:
        st.info(stock_v3.get("statement", ""))
    st.subheader("前瞻概率消融")
    st.caption("只使用已经到期的真实线上预测；样本不足时明确显示 collecting。")
    for item in evidence["probability_ablation"]:
        with st.expander(
            f"{item['asset_scope']} · {item['horizon_days']}日 · {item['status']}",
            expanded=True,
        ):
            st.write(item["statement"])
            rows = [{"variant": name, **metrics} for name, metrics in item["variants"].items()]
            st.dataframe(rows, width="stretch", hide_index=True)
            st.json(item["comparisons"])
    st.subheader("多 Agent 候选排名前瞻证据")
    tournament_scorecard = evidence["candidate_tournament_scorecard"]
    if tournament_scorecard["tournaments"]:
        st.dataframe(
            pd.DataFrame.from_dict(tournament_scorecard["horizons"], orient="index").reset_index(
                names="horizon_days"
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(tournament_scorecard["claim_boundary"])
    else:
        st.info("尚无已保存并到期的候选擂台；先积累前瞻排名记录。")
    st.subheader("前瞻影子账户")
    render_paper_scorecard(evidence["prospective_paper_scorecard"], "evidence")


def render_historical_replay(replay: dict, key_prefix: str) -> None:
    metrics = replay["metrics"]
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("回放数量", replay["completed_episodes"])
    col2.metric("完整系统累计收益", f"{metrics['full_system']['total_return']:.2%}")
    col3.metric("纯策略累计收益", f"{metrics['strategy_only']['total_return']:.2%}")
    col4.metric(
        "系统相对策略",
        f"{metrics['full_minus_strategy_total_return']:+.2%}",
    )
    if replay["evidence_status"] == "illustrative":
        st.warning("回放数量很少，只能作为演示性证据，不能证明未来盈利。")
    st.subheader("防泄漏与盲测契约")
    st.json(
        {
            "selection_rule": replay["selection_rule"],
            "blinding": replay["blinding"],
            "statistical_model_contract": replay["statistical_model_contract"],
            "execution_contract": replay["execution_contract"],
            "llm_validation": replay["llm_validation"],
        }
    )
    st.subheader("逐次假下单")
    episode_rows = []
    for item in replay["episodes"]:
        episode_rows.append(
            {
                "episode": item["episode"],
                "signal_date": item["actual_as_of"],
                "symbol": item["actual_symbol"],
                "action": item["decision"]["action"],
                "confidence": item["decision"]["confidence"],
                "strategy_return": item["strategy_trade"]["net_return"],
                "full_system_return": item["full_system_trade"]["net_return"],
                "benchmark_return": item["benchmark_trade"]["net_return"],
                "gate_effect": item["gate_effect"],
                "actual_outcome": item["outcome"],
            }
        )
    st.dataframe(episode_rows, width="stretch", hide_index=True)
    st.subheader("概率消融")
    st.json(metrics["forecast_ablation"])
    gate_metrics = metrics.get("decision_gate_counterfactuals", {})
    if gate_metrics:
        st.subheader("ETF 决策闸门反事实审查")
        st.caption("候选闸门复用同一批盲测预测和成交，只用于研究；正式决策规则没有自动改变。")
        st.dataframe(
            [{"policy": name, **value} for name, value in gate_metrics.items()],
            width="stretch",
            hide_index=True,
        )
    st.subheader("组合对照")
    st.dataframe(
        [
            {"variant": name, **value}
            for name, value in (
                ("strategy_only", metrics["strategy_only"]),
                ("full_system", metrics["full_system"]),
                ("hs300_same_risk_budget", metrics["hs300_same_risk_budget"]),
            )
        ],
        width="stretch",
        hide_index=True,
    )
    markdown = render_historical_replay_markdown(replay)
    left, right = st.columns(2)
    left.download_button(
        "下载历史盲测报告",
        markdown.encode("utf-8"),
        file_name=f"historical-replay-{replay.get('replay_id', 'unsaved')}.md",
        mime="text/markdown",
        key=f"{key_prefix}_replay_md",
    )
    right.download_button(
        "下载历史盲测 JSON",
        audit_package_json(prepare_historical_replay_export(replay)).encode("utf-8"),
        file_name=f"historical-replay-{replay.get('replay_id', 'unsaved')}.json",
        mime="application/json",
        key=f"{key_prefix}_replay_json",
    )

st.title("QuantLab")
st.caption("Evidence Trace · 高级工程与审计工作区")
st.subheader("工程审计视图")
st.caption("面向模型、实验、证据评分、Worker 与调试检查；日常投资能力统一留在“专业空间”。")

(
    today_tab,
    radar_tab,
    stocks_tab,
    tournament_tab,
    research_tab,
    roundtable_tab,
    plan_tab,
    paper_tab,
    evidence_tab,
    replay_tab,
    overview_tab,
    learning_tab,
    llm_tab,
    audit_tab,
    demo_tab,
) = st.tabs(
    [
        "今日决策",
        "市场雷达",
        "股票发现",
        "候选擂台",
        "多 Agent 研究",
        "专家圆桌",
        "手工下单计划",
        "模拟盘",
        "证据中心",
        "历史盲测",
        "组合账本",
        "学习状态",
        "LLM 配置",
        "审计报告",
        "一键 Demo",
    ]
)

with today_tab:
    st.subheader("今日投资决策中心")
    st.write("把复杂 Agent 输出压缩成今天需要执行、复核或保持不动的事项。")
    today_as_of = st.date_input("决策中心截止日期", value=date.today(), key="today_as_of")
    if st.button("刷新今日决策中心", type="primary"):
        try:
            with st.spinner("汇总市场、研究、组合、模拟盘和证据状态..."):
                st.session_state["today_brief"] = build_today_brief(settings, today_as_of)
        except Exception as exc:
            st.error(f"今日决策中心失败：{exc}")
    brief = st.session_state.get("today_brief")
    if brief:
        headline = brief["headline"]
        row1 = st.columns(4)
        row1[0].metric("市场状态", headline["market_regime"])
        row1[1].metric("风险偏好", headline["risk_appetite"])
        row1[2].metric("建议总暴露", f"{headline['suggested_total_exposure']:.0%}")
        loss_amount = headline["estimated_maximum_loss_amount"]
        row1[3].metric(
            "估计计划最大损失",
            f"¥{loss_amount:,.0f}" if loss_amount is not None else "待补充",
        )
        row2 = st.columns(3)
        row2[0].metric("新增买入", headline["new_buy_count"])
        row2[1].metric("减仓/卖出", headline["reduce_count"])
        row2[2].metric("人工复核", headline["review_count"])
        st.subheader("当前强弱领先")
        st.dataframe(brief["top_opportunities"], width="stretch", hide_index=True)
        st.subheader("最近决策卡")
        st.dataframe(brief["decision_cards"], width="stretch", hide_index=True)
        current_plan = brief["current_plan"]
        st.subheader("今日手工执行清单")
        if current_plan["available"]:
            st.dataframe(current_plan["orders"], width="stretch", hide_index=True)
        else:
            st.info("今天还没有生成组合计划。")
        if current_plan["warnings"]:
            st.warning("\n- ".join(current_plan["warnings"]))
        if brief.get("paper_portfolio"):
            paper = brief["paper_portfolio"]
            paper_cols = st.columns(3)
            paper_cols[0].metric("完整系统模拟净值", f"¥{paper['latest_equity']:,.2f}")
            paper_cols[1].metric("模拟累计收益", f"{paper['total_return']:.2%}")
            paper_cols[2].metric("模拟待成交单", paper["pending_orders"])
        st.subheader("下一步")
        for action in brief["next_actions"]:
            st.write(f"- {action}")
        if brief["data_quality"]["degraded_sources"]:
            st.warning("\n- ".join(brief["data_quality"]["degraded_sources"]))

with radar_tab:
    st.subheader("跨资产与行业强弱雷达")
    st.write("所有排名均由真实行情点时计算；行业数据是可选快照，失败时会显式降级。")
    radar_as_of = st.date_input("雷达截止日期", value=date.today(), key="radar_as_of")
    include_sectors = st.checkbox("同时获取行业热度快照（可能较慢）", value=False)
    if st.button("刷新真实市场雷达", type="primary"):
        try:
            with st.spinner("获取 ETF 行情并计算跨资产强弱..."):
                st.session_state["market_radar"] = build_market_radar(
                    settings, radar_as_of, include_sectors
                )
        except Exception as exc:
            st.error(f"市场雷达失败：{exc}")
    if st.session_state.get("market_radar"):
        render_radar(st.session_state["market_radar"])

with stocks_tab:
    st.subheader("A股发现、自选池与批量会诊")
    st.write(
        "用户可以输入自己的股票池，也可以让系统从五种透明风格中发现候选。"
        "快速筛选不调用 LLM；只有明确选中的股票才进入投资大师与多 Agent 深度会诊。"
    )
    selected_stock_tab, recommended_stock_tab, stock_evidence_tab, deep_stock_tab = st.tabs(
        ["我的股票池", "系统推荐", "历史排名证据", "批量深度会诊"]
    )

    with selected_stock_tab:
        search_col, search_button_col = st.columns([4, 1])
        stock_keyword = search_col.text_input(
            "按代码或名称搜索股票",
            placeholder="例如：贵州茅台、600519",
            key="stock_search_keyword",
        )
        if search_button_col.button("搜索", key="stock_search_button"):
            try:
                st.session_state["stock_search_results"] = search_stocks(
                    settings,
                    stock_keyword,
                )["results"]
            except Exception as exc:
                st.error(f"股票搜索失败：{exc}")
        search_results = st.session_state.get("stock_search_results", [])
        selected_search_symbols = st.multiselect(
            "从搜索结果加入本次股票池",
            [item["symbol"] for item in search_results],
            format_func=lambda symbol: next(
                f"{item['name']} ({item['display_code']})"
                for item in search_results
                if item["symbol"] == symbol
            ),
            key="selected_search_symbols",
        )
        stock_codes = st.text_area(
            "股票代码（逗号、空格或换行分隔，最多20只）",
            value="600519, 000858, 300750",
            key="selected_stock_codes",
        )
        watchlist = terminal.list_watchlist()
        include_watchlist = st.checkbox(
            f"同时包含当前自选池（{len(watchlist)}只）",
            value=False,
            key="include_watchlist_in_screen",
        )
        selected_as_of = st.date_input(
            "快速筛选截止日期",
            value=date.today(),
            key="selected_stock_as_of",
        )
        screen_col1, screen_col2 = st.columns(2)
        selected_top_n = screen_col1.slider(
            "展示前N只",
            1,
            20,
            10,
            key="selected_stock_top_n",
        )
        selected_max_corr = screen_col2.slider(
            "股票最大正相关阈值",
            0.50,
            0.95,
            0.85,
            0.05,
            key="selected_stock_max_corr",
        )
        if st.button("快速分析我的股票池", type="primary", key="screen_selected_stocks"):
            try:
                combined = stock_codes
                if selected_search_symbols:
                    combined += "," + ",".join(selected_search_symbols)
                if include_watchlist and watchlist:
                    combined += "," + ",".join(item["symbol"] for item in watchlist)
                selected_metadata = {
                    item["symbol"]: {
                        "name": item.get("name", item["symbol"]),
                        "industry": item.get("industry", ""),
                    }
                    for item in search_results
                    if item["symbol"] in selected_search_symbols
                }
                selected_metadata.update(
                    {
                        item["symbol"]: {"name": item.get("name") or item["symbol"]}
                        for item in watchlist
                    }
                )
                with st.spinner("批量获取行情并计算多层动量、趋势、流动性和相关性..."):
                    st.session_state["selected_stock_screen"] = screen_selected_stocks(
                        settings,
                        combined,
                        selected_as_of,
                        top_n=selected_top_n,
                        max_correlation=selected_max_corr,
                        save=True,
                        metadata=selected_metadata,
                    )
            except Exception as exc:
                st.error(f"股票池筛选失败：{exc}")
        selected_output = st.session_state.get("selected_stock_screen")
        if selected_output:
            render_stock_discovery(selected_output, "selected")
            watch_group = st.text_input(
                "保存到自选池分组",
                value="研究候选",
                key="selected_watch_group",
            )
            if st.button("将本次候选加入自选池", key="save_selected_watchlist"):
                for item in selected_output["candidates"]:
                    terminal.upsert_watchlist(
                        item["symbol"],
                        item.get("name", item["symbol"]),
                        watch_group.strip() or "研究候选",
                        f"快速筛选分={item.get('screen_score', 0):.2f}",
                    )
                st.success(f"已加入 {len(selected_output['candidates'])} 只股票。")
        if watchlist:
            with st.expander("查看当前自选池"):
                st.dataframe(watchlist, width="stretch", hide_index=True)

    with recommended_stock_tab:
        style_labels = {key: value["label"] for key, value in STOCK_DISCOVERY_STYLES.items()}
        selected_styles = st.multiselect(
            "候选发现风格",
            list(STOCK_DISCOVERY_STYLES),
            default=list(STOCK_DISCOVERY_STYLES),
            format_func=lambda key: style_labels[key],
            key="stock_recommend_styles",
        )
        recommend_col1, recommend_col2, recommend_col3 = st.columns(3)
        recommendation_pool = recommend_col1.slider(
            "预筛候选上限",
            5,
            50,
            20,
            5,
            key="stock_recommend_pool",
        )
        recommendation_top_n = recommend_col2.slider(
            "返回前N只",
            1,
            min(20, recommendation_pool),
            min(10, recommendation_pool),
            key="stock_recommend_top_n",
        )
        recommendation_corr = recommend_col3.slider(
            "最大正相关阈值",
            0.50,
            0.95,
            0.85,
            0.05,
            key="stock_recommend_corr",
        )
        st.caption("系统推荐使用当前免费市场快照，不调用 LLM，也不会在筛选为空时补造股票。")
        if st.button("运行系统股票推荐", type="primary", key="run_stock_recommendation"):
            try:
                with st.spinner("并行扫描多种风格，再用统一因子引擎重排和相关性去重..."):
                    st.session_state["recommended_stock_screen"] = recommend_stocks(
                        settings,
                        styles=selected_styles,
                        candidate_limit=recommendation_pool,
                        top_n=recommendation_top_n,
                        max_correlation=recommendation_corr,
                        save=True,
                    )
            except Exception as exc:
                st.error(f"系统股票推荐失败：{exc}")
        recommended_output = st.session_state.get("recommended_stock_screen")
        if recommended_output:
            render_stock_discovery(recommended_output, "recommended")

    with stock_evidence_tab:
        st.write(
            "冻结一组股票，用每个历史时点当时已经存在的数据重新排名，并与简单动量、"
            "股票池等权和沪深300同预算比较。"
        )
        default_stock_universe = ", ".join(
            settings.get(
                "strategies.stock_evidence.default_universe",
                ["sh600519", "sz000858", "sz300750"],
            )
        )
        evidence_symbols = st.text_area(
            "固定回放股票池（2–20只）",
            value=default_stock_universe,
            key="stock_evidence_symbols",
        )
        evidence_date_cols = st.columns(2)
        evidence_start = evidence_date_cols[0].date_input(
            "A股回放开始",
            value=date(2024, 1, 1),
            key="stock_evidence_start",
        )
        evidence_end = evidence_date_cols[1].date_input(
            "A股回放结束",
            value=date(2025, 12, 31),
            key="stock_evidence_end",
        )
        evidence_cols = st.columns(4)
        evidence_horizon = evidence_cols[0].selectbox(
            "结算周期",
            [20, 5],
            format_func=lambda value: f"{value}个交易日",
            key="stock_evidence_horizon",
        )
        evidence_episodes = evidence_cols[1].slider(
            "不重叠回合",
            3,
            60,
            12,
            key="stock_evidence_episodes",
        )
        evidence_top_k = evidence_cols[2].slider(
            "分散Top-K",
            1,
            5,
            int(settings.get("strategies.stock_evidence.top_k", 3)),
            key="stock_evidence_top_k",
        )
        evidence_corr = evidence_cols[3].slider(
            "最大相关性",
            0.50,
            0.95,
            float(settings.get("strategies.stock_evidence.max_correlation", 0.85)),
            0.05,
            key="stock_evidence_corr",
        )
        st.caption(
            "该实验会记录股票学习样本，但固定当前股票池仍有选择偏差，样本默认不允许自动激活模型。"
        )
        if st.button("运行A股点时排名回放", type="primary", key="run_stock_evidence"):
            try:
                with st.spinner("逐个历史时点重建排名、模拟次日成交并计算配对统计..."):
                    st.session_state["stock_ranking_replay"] = run_stock_ranking_replay(
                        settings,
                        evidence_symbols,
                        evidence_start,
                        evidence_end,
                        horizon_days=evidence_horizon,
                        episodes=evidence_episodes,
                        top_k=evidence_top_k,
                        max_correlation=evidence_corr,
                        save=True,
                    )
            except Exception as exc:
                st.error(f"A股排名回放失败：{exc}")
        if st.session_state.get("stock_ranking_replay"):
            render_stock_ranking_replay(
                st.session_state["stock_ranking_replay"], "new_stock_replay"
            )
        previous_stock_replays = stock_replay_repository.list(10)
        if previous_stock_replays:
            with st.expander("查看已保存的A股排名回放"):
                st.dataframe(previous_stock_replays, width="stretch", hide_index=True)
                selected_stock_replay_id = st.selectbox(
                    "载入回放编号",
                    [item["id"] for item in previous_stock_replays],
                    key="stored_stock_replay_id",
                )
                if st.button("载入A股历史回放", key="load_stock_replay"):
                    stored = stock_replay_repository.get(selected_stock_replay_id)
                    if stored:
                        st.session_state["stock_ranking_replay"] = stored["payload"]

        st.divider()
        st.subheader("全市场点时分层回放")
        st.write(
            "从每个历史日期真实存在的全部A股中，按沪主板、深主板、科创板和创业板"
            "进行确定性分层抽样；退市股票在退市前仍可能进入样本，ST和停牌状态使用历史日数据。"
        )
        latest_master = universe_repository.latest_master_build()
        master_cols = st.columns([2, 1])
        master_cols[0].json(latest_master or {"status": "尚未构建证券主数据"})
        if master_cols[1].button("刷新上市/退市主数据", key="refresh_stock_master"):
            try:
                with st.spinner("从沪深交易所刷新上市、退市和生效日期..."):
                    st.session_state["stock_master_refresh"] = refresh_a_share_security_master(
                        settings
                    )
                st.success("A股证券主数据已刷新。")
            except Exception as exc:
                st.error(f"证券主数据刷新失败：{exc}")
        market_dates = st.columns(2)
        market_replay_start = market_dates[0].date_input(
            "全市场回放开始",
            value=date(2024, 1, 1),
            key="market_replay_start",
        )
        market_replay_end = market_dates[1].date_input(
            "全市场回放结束",
            value=date(2025, 12, 31),
            key="market_replay_end",
        )
        market_cols = st.columns(4)
        market_horizon = market_cols[0].selectbox(
            "全市场周期",
            [5, 20],
            key="market_replay_horizon",
        )
        market_episodes = market_cols[1].slider(
            "全市场回合",
            3,
            60,
            12,
            key="market_replay_episodes",
        )
        market_sample_size = market_cols[2].slider(
            "每期分层样本",
            12,
            120,
            int(settings.get("strategies.stock_market_replay.sample_size", 60)),
            4,
            key="market_replay_sample_size",
        )
        market_top_k = market_cols[3].slider(
            "全市场Top-K",
            1,
            5,
            3,
            key="market_replay_top_k",
        )
        st.warning(
            "首次运行需要抓取历史市场快照和样本股票日线，耗时明显长于固定池回放；"
            "已经保存的日期快照会直接复用。"
        )
        if st.button("运行全市场点时回放", type="primary", key="run_market_stock_replay"):
            try:
                progress_bar = st.progress(0.0, text="准备历史市场快照...")

                def update_market_progress(item):
                    progress_bar.progress(
                        item["completed"] / item["requested"],
                        text=(
                            f"{item['completed']}/{item['requested']} · "
                            f"{item['signal_date']} · 全市场{item['full_market_securities']}只 · "
                            f"合格{item['eligible_candidates']}只"
                        ),
                    )

                with st.spinner("重建历史市场、获取退市股行情并执行点时排名..."):
                    st.session_state["stock_ranking_replay"] = run_market_wide_stock_replay(
                        settings,
                        market_replay_start,
                        market_replay_end,
                        horizon_days=market_horizon,
                        episodes=market_episodes,
                        sample_size=market_sample_size,
                        top_k=market_top_k,
                        save=True,
                        progress_callback=update_market_progress,
                    )
                progress_bar.empty()
                st.success("全市场点时回放完成。")
            except Exception as exc:
                st.error(f"全市场点时回放失败：{exc}")
        if universe_repository.snapshot_dates(10):
            with st.expander("已缓存的历史A股市场快照"):
                st.dataframe(
                    universe_repository.snapshot_dates(10),
                    width="stretch",
                    hide_index=True,
                )

    with deep_stock_tab:
        candidate_options = []
        candidate_names = {}
        for key in ("selected_stock_screen", "recommended_stock_screen"):
            for item in st.session_state.get(key, {}).get("candidates", []):
                if item["symbol"] not in candidate_options:
                    candidate_options.append(item["symbol"])
                candidate_names[item["symbol"]] = item.get("name", item["symbol"])
        for item in terminal.list_watchlist():
            if item["symbol"] not in candidate_options:
                candidate_options.append(item["symbol"])
            candidate_names[item["symbol"]] = item.get("name") or item["symbol"]
        deep_symbols = st.multiselect(
            "选择1–5只股票运行完整会诊",
            candidate_options,
            format_func=lambda symbol: f"{candidate_names.get(symbol, symbol)} ({symbol})",
            key="deep_stock_symbols",
        )
        deep_as_of = st.date_input(
            "深度会诊截止日期",
            value=date.today(),
            key="deep_stock_as_of",
        )
        deep_include_events = st.checkbox(
            "采集近期免费新闻和公告",
            value=True,
            key="deep_stock_events",
        )
        estimated_calls = len(deep_symbols) * (18 if deep_include_events else 17)
        st.caption(
            f"预计真实 LLM 角色调用约 {estimated_calls} 次；每只股票包含战术委员会、"
            "Buffett、Munger、Graham、Fisher、Lynch、双周期预测和 Reviewer。"
        )
        if st.button("运行批量深度会诊", type="primary", key="run_deep_stock_batch"):
            try:
                with st.spinner("正在逐只执行财务质量、投资大师、多空辩论、概率预测和审核..."):
                    st.session_state["deep_stock_research"] = run_stock_research_batch(
                        settings,
                        deep_symbols,
                        deep_as_of,
                        include_events=deep_include_events,
                        save=True,
                    )
            except Exception as exc:
                st.error(f"批量深度会诊失败：{exc}")
        deep_output = st.session_state.get("deep_stock_research")
        if deep_output:
            st.dataframe(deep_output["summaries"], width="stretch", hide_index=True)
            if deep_output["failures"]:
                st.warning(
                    "部分股票会诊失败：\n- "
                    + "\n- ".join(
                        f"{item['symbol']}: {item['error']}" for item in deep_output["failures"]
                    )
                )
            if deep_output["analyses"]:
                package_by_symbol = {
                    package["symbol"]: package for package in deep_output["analyses"]
                }
                selected_package_symbol = st.selectbox(
                    "查看完整会诊报告",
                    list(package_by_symbol),
                    format_func=lambda symbol: f"{candidate_names.get(symbol, symbol)} ({symbol})",
                    key="deep_stock_report_symbol",
                )
                render_research(
                    package_by_symbol[selected_package_symbol],
                    f"deep_stock_{selected_package_symbol}",
                )

    discovery_history = terminal.stock_discovery_runs(10)
    if discovery_history:
        with st.expander("最近股票发现与批量会诊记录"):
            st.dataframe(discovery_history, width="stretch", hide_index=True)

with tournament_tab:
    st.subheader("多标的多 Agent 候选擂台")
    st.write(
        "让多个领先 ETF 接受同一套因子、概率预测、专家委员会和 Reviewer，"
        "再按统一分数横向比较，并用收益相关性去除重复暴露。"
    )
    tournament_as_of = st.date_input("擂台截止日期", value=date.today(), key="tournament_as_of")
    tournament_col1, tournament_col2, tournament_col3 = st.columns(3)
    candidate_limit = tournament_col1.slider("参赛候选数", 2, 4, 2)
    shortlist_size = tournament_col2.slider(
        "入围数量", 1, min(3, candidate_limit), min(2, candidate_limit)
    )
    max_correlation = tournament_col3.slider("最大正相关阈值", 0.50, 0.95, 0.80, step=0.05)
    st.caption(
        f"预计真实 LLM 角色调用约 {candidate_limit * 11} 次。每个候选使用相同委员会配置，"
        "单个候选失败不会中断整场擂台。"
    )
    if st.button("运行候选擂台", type="primary"):
        try:
            with st.spinner("正在执行多候选因子研究、Agent 委员会、横向排名和组合压力测试..."):
                st.session_state["candidate_tournament"] = run_candidate_tournament(
                    settings,
                    tournament_as_of,
                    candidate_limit=candidate_limit,
                    shortlist_size=shortlist_size,
                    max_correlation=max_correlation,
                    save=True,
                )
        except Exception as exc:
            st.error(f"候选擂台失败：{exc}")
    stored_tournament = st.session_state.get("candidate_tournament")
    if stored_tournament:
        render_candidate_tournament(stored_tournament)
    st.divider()
    settlement_col, scorecard_col = st.columns([1, 2])
    if settlement_col.button("结算已到期擂台"):
        try:
            with st.spinner("正在用未来5/20个交易日的真实行情回填擂台结果..."):
                st.session_state["tournament_settlement"] = settle_candidate_tournaments(
                    settings,
                    date.today(),
                )
        except Exception as exc:
            st.error(f"擂台结算失败：{exc}")
    if st.session_state.get("tournament_settlement"):
        settlement = st.session_state["tournament_settlement"]
        settlement_col.json(
            {
                "settled": settlement["settled"],
                "pending": settlement["pending"],
                "not_comparable": settlement["not_comparable"],
                "degraded_sources": settlement["degraded_sources"],
            }
        )
    tournament_scorecard = candidate_tournament_scorecard(settings)
    if tournament_scorecard["tournaments"]:
        scorecard_col.subheader("擂台真实结果成绩单")
        scorecard_col.dataframe(
            pd.DataFrame.from_dict(tournament_scorecard["horizons"], orient="index").reset_index(
                names="horizon_days"
            ),
            width="stretch",
            hide_index=True,
        )
        scorecard_col.caption(tournament_scorecard["claim_boundary"])
    else:
        scorecard_col.info("运行并保存候选擂台后，5/20个交易日到期即可形成真实排名成绩单。")

with research_tab:
    st.subheader("真实 ETF / A 股多 Agent 研究")
    asset_choice = st.radio("研究对象", ["ETF", "A股"], horizontal=True)
    research_as_of = st.date_input("研究截止日期", value=date.today(), key="research_as_of")
    if asset_choice == "ETF":
        universe = list(settings.get("strategies.etf_rotation.universe"))
        symbol = st.selectbox(
            "ETF",
            universe,
            format_func=lambda item: f"{ETF_METADATA.get(item, {}).get('name', item)} ({item})",
        )
        asset_type = "etf"
        include_events_for_research = False
        st.caption("ETF 委员会侧重技术、动量、风险、宏观和概率融合。")
    else:
        symbol = st.text_input("A 股代码", value="sh600519").strip()
        asset_type = "stock"
        include_events_for_research = st.checkbox("采集近期免费新闻与公告", value=True)
        st.caption("A 股委员会会增加财务质量闸门，以及 Buffett、Munger、Graham、Fisher、Lynch。")
    if st.button("运行真实多 Agent 研究", type="primary"):
        try:
            with st.spinner("因子、数据闸门、专家委员会、概率预测与 Reviewer 正在运行..."):
                research_output = analyze_symbol(
                    settings,
                    symbol,
                    research_as_of,
                    asset_type=asset_type,
                    include_events=include_events_for_research,
                )
                decision_repository.save(
                    research_output["decision_run"],
                    research_persistence_context(research_output),
                    provenance=ResearchProvenance(
                        origin="user_interactive_research",
                        requested_as_of=research_as_of,
                        evidence_stage="research_only",
                    ),
                )
                st.session_state["research_package"] = build_research_audit_package(research_output)
        except Exception as exc:
            st.error(f"研究失败：{exc}")
    if st.session_state.get("research_package"):
        render_research(st.session_state["research_package"], "research")

with roundtable_tab:
    st.subheader("可选投资大师与专家圆桌会议")
    st.write(
        "选择一份已经完成并冻结证据的研究报告，再邀请不同投资大师、战术专家和多空角色"
        "进行多轮交锋。第二轮起，每个角色必须回应上一轮最强论点并说明是否修正观点。"
    )
    recent_research = decision_repository.recent(50)
    if not recent_research:
        st.info("请先在“多 Agent 研究”或“批量深度会诊”中保存至少一份研究报告。")
    else:
        research_by_id = {item["run_id"]: item for item in recent_research}
        source_run_id = st.selectbox(
            "圆桌依据的冻结研究报告",
            list(research_by_id),
            format_func=lambda run_id: (
                f"{research_by_id[run_id]['symbol']} · {research_by_id[run_id]['as_of']} · "
                f"{research_by_id[run_id]['action']} · {run_id[:8]}"
            ),
            key="roundtable_source_run",
        )
        participant_catalog = roundtable_participant_catalog()
        participant_by_key = {item["key"]: item for item in participant_catalog}
        default_participants = ["buffett", "munger", "graham", "fisher", "lynch"]
        selected_participants = st.multiselect(
            "邀请专家（2–8人）",
            list(participant_by_key),
            default=default_participants,
            format_func=lambda key: (
                f"{participant_by_key[key]['label']} · {participant_by_key[key]['perspective']}"
            ),
            key="roundtable_participants",
        )
        roundtable_topic = st.text_area(
            "本次讨论主题",
            value="这份研究最可能错在哪里？当前价格下，最值得继续验证的三个问题是什么？",
            max_chars=1_000,
            key="roundtable_topic",
        )
        roundtable_rounds = st.slider("讨论轮数", 1, 3, 2, key="roundtable_rounds")
        st.caption(
            f"预计真实 LLM 调用约 {len(selected_participants) * roundtable_rounds + 1} 次；"
            "同一轮并行发言，避免发言顺序造成系统性偏置。"
        )
        if st.button("召开专家圆桌", type="primary", key="run_roundtable"):
            if len(selected_participants) < 2:
                st.error("至少选择两位不同专家。")
            else:
                try:
                    with st.spinner("专家正在阅读冻结证据、逐轮交锋并生成主持人总结..."):
                        st.session_state["roundtable_session"] = run_expert_roundtable(
                            settings,
                            source_run_id,
                            selected_participants,
                            roundtable_topic,
                            rounds=roundtable_rounds,
                            save=True,
                        )
                except Exception as exc:
                    st.error(f"圆桌会议失败：{exc}")

    recent_roundtables = roundtable_repository.recent(20)
    if recent_roundtables:
        st.divider()
        session_by_id = {item["session_id"]: item for item in recent_roundtables}
        selected_session_id = st.selectbox(
            "查看历史圆桌",
            [""] + list(session_by_id),
            format_func=lambda session_id: (
                "选择历史记录"
                if not session_id
                else (
                    f"{session_by_id[session_id]['symbol']} · "
                    f"{session_by_id[session_id]['topic'][:40]} · {session_id[:8]}"
                )
            ),
            key="roundtable_history_id",
        )
        if selected_session_id and st.button("加载历史圆桌", key="load_roundtable"):
            st.session_state["roundtable_session"] = roundtable_repository.get(selected_session_id)
    if st.session_state.get("roundtable_session"):
        st.divider()
        render_roundtable(st.session_state["roundtable_session"], "roundtable")

with plan_tab:
    st.subheader("三策略统一组合与手工下单清单")
    plan_policy_cols = st.columns(2)
    etf_plan_policy = plan_policy_cols[0].selectbox(
        "ETF 组合政策",
        ["evidence_first", "equal_weight_core", "momentum_rotation"],
        format_func=lambda value: {
            "evidence_first": "证据优先（推荐）",
            "equal_weight_core": "多资产 ETF 等权核心",
            "momentum_rotation": "主动动量轮动（研究）",
        }[value],
    )
    allow_unvalidated_stock = plan_policy_cols[1].checkbox(
        "展示未准入 A 股研究复核清单",
        value=False,
        help=(
            "V3 虽有正收益和正超额，但统计置信度未达门槛；该选项只展示研究复核候选，"
            "不会生成新增 actionable 订单。"
        ),
    )
    reversal_limit = st.slider("股票反转候选数量", 0, 20, 5)
    check_stock_risks = st.checkbox("执行股票风险与财务质量检查", value=True)
    if st.button("生成并保存组合计划", type="primary"):
        try:
            with st.spinner("扫描 ETF、A 股反转与可转债，并执行组合风险约束..."):
                st.session_state["latest_plan"] = generate_portfolio_plan(
                    settings,
                    reversal_limit=reversal_limit,
                    check_stock_risks=check_stock_risks,
                    etf_policy=etf_plan_policy,
                    allow_unvalidated_stock=allow_unvalidated_stock,
                )
        except Exception as exc:
            st.error(f"组合计划失败：{exc}")
    latest_plan = st.session_state.get("latest_plan") or terminal.latest_portfolio_plan()
    if latest_plan:
        plan = latest_plan["plan"]
        regime_col, exposure_col, warning_col = st.columns(3)
        regime_col.metric("市场状态", plan["market_regime"])
        exposure_col.metric("目标总暴露", f"{sum(plan['target_weights'].values()):.1%}")
        warning_col.metric("风险提示数", len(plan["warnings"]))
        st.subheader("动态策略预算")
        st.json(plan["strategy_budgets"])
        if latest_plan.get("portfolio_policy"):
            st.subheader("证据驱动政策选择")
            st.json(latest_plan["portfolio_policy"])
        st.subheader("可人工执行")
        st.dataframe(plan["orders"], width="stretch", hide_index=True)
        st.subheader("否决与复核")
        st.dataframe(plan["blocked_candidates"], width="stretch", hide_index=True)
        if plan["warnings"]:
            st.warning("\n- ".join(["风险提示"] + plan["warnings"]))
        if plan["degraded_sources"]:
            st.warning("\n- ".join(["数据降级"] + plan["degraded_sources"]))
    else:
        st.info("尚未生成组合计划。")

with paper_tab:
    st.subheader("10 万元前瞻模拟盘")
    st.write(
        "同时跟踪沪深300、ETF 等权、纯轮动和完整系统。首次运行生成次日待成交单，"
        "后续运行才会按首个可用开盘价成交。"
    )
    paper_as_of = st.date_input("模拟盘截止日期", value=date.today(), key="paper_as_of")
    run_research_for_paper = st.checkbox(
        "为领先 ETF 同时运行当日多 Agent 研究（会产生真实 LLM 调用）",
        value=False,
    )
    if st.button("运行模拟盘每日周期", type="primary"):
        try:
            with st.spinner("盯市、撮合旧订单、冻结新信号并保存净值快照..."):
                paper_output = run_paper_cycle(
                    settings,
                    paper_as_of,
                    run_research=run_research_for_paper,
                    research_limit=1,
                )
                st.session_state["paper_output"] = paper_output
        except Exception as exc:
            st.error(f"模拟盘周期失败：{exc}")
    paper_output = st.session_state.get("paper_output")
    if paper_output:
        if paper_output["fills"]:
            st.subheader("本次成交")
            st.dataframe(paper_output["fills"], width="stretch", hide_index=True)
        st.subheader("次日待成交")
        st.dataframe(paper_output["queued_orders"], width="stretch", hide_index=True)
        if paper_output["warnings"]:
            st.warning("\n- ".join(paper_output["warnings"]))
        render_paper_scorecard(paper_output["scorecard"], "paper_output")
    else:
        render_paper_scorecard(paper_scorecard(settings), "paper_stored")

    st.divider()
    st.subheader("A股三账户前瞻影子盘")
    st.write(
        "并行跟踪固定股票池等权、系统排名Top-N和多Agent审核账户；"
        "执行整手、T+1、停牌/一字涨跌停与股票交易成本。"
    )
    stock_paper_symbols = st.text_area(
        "A股模拟研究池",
        value=", ".join(
            settings.get(
                "strategies.stock_evidence.default_universe",
                ["sh600519", "sz000858", "sz300750"],
            )
        ),
        key="stock_paper_symbols",
    )
    stock_paper_cols = st.columns(3)
    stock_paper_top_n = stock_paper_cols[0].slider(
        "影子账户Top-N", 1, 5, 3, key="stock_paper_top_n"
    )
    stock_paper_corr = stock_paper_cols[1].slider(
        "影子账户最大相关性",
        0.50,
        0.95,
        0.85,
        0.05,
        key="stock_paper_corr",
    )
    stock_paper_research = stock_paper_cols[2].checkbox(
        "为Top候选运行多Agent",
        value=False,
        key="stock_paper_research",
    )
    if st.button("运行A股影子盘周期", type="primary", key="run_stock_paper"):
        try:
            with st.spinner("撮合A股旧订单、生成排名目标并冻结次日订单..."):
                st.session_state["stock_paper_output"] = run_stock_paper_cycle(
                    settings,
                    stock_paper_symbols,
                    paper_as_of,
                    top_n=stock_paper_top_n,
                    max_correlation=stock_paper_corr,
                    run_research=stock_paper_research,
                    research_limit=min(2, stock_paper_top_n),
                )
        except Exception as exc:
            st.error(f"A股影子盘失败：{exc}")
    stock_paper_output = st.session_state.get("stock_paper_output")
    if stock_paper_output:
        if stock_paper_output["fills"]:
            st.subheader("本次A股模拟成交")
            st.dataframe(stock_paper_output["fills"], width="stretch", hide_index=True)
        st.subheader("A股次日待成交")
        st.dataframe(stock_paper_output["queued_orders"], width="stretch", hide_index=True)
        if stock_paper_output["warnings"]:
            st.warning("\n- ".join(stock_paper_output["warnings"]))

with evidence_tab:
    st.subheader("赚钱能力与增量价值证据")
    st.write("历史回测、统计模型验证和未来实盘跟踪分开展示，避免混用。")
    with st.expander("运行严谨 ETF 样本外实验", expanded=False):
        st.caption("使用带隔离期的滚动样本外验证、配对块自助法、多重试验修正和 2 倍成本压力测试。")
        evidence_start = st.date_input("实验开始日期", value=date(2016, 1, 1), key="evidence_start")
        evidence_end = st.date_input("实验结束日期", value=date.today(), key="evidence_end")
        evidence_train = st.number_input(
            "训练交易日", min_value=120, max_value=1500, value=756, step=20
        )
        evidence_test = st.number_input(
            "测试交易日", min_value=20, max_value=500, value=252, step=20
        )
        if st.button("运行并保存严谨实验", type="primary", key="run_rigorous_evidence"):
            try:
                with st.spinner("正在执行滚动样本外、过拟合诊断和成本压力测试..."):
                    st.session_state["rigorous_validation"] = run_etf_walk_forward(
                        settings,
                        evidence_start,
                        evidence_end,
                        int(evidence_train),
                        int(evidence_test),
                        save=True,
                    )
                    export_profitability_evidence(settings)
                st.success("实验与盈利能力证据报告已经保存。")
            except Exception as exc:
                st.error(f"严谨实验失败：{exc}")
    with st.expander("运行 ETF 策略版本探索比较", expanded=False):
        st.caption(
            "不调用 LLM；比较所选策略、ETF 等权和 2 倍成本。自适应版本只作回顾性探索，"
            "不会改变策略准入或真实资金预算。"
        )
        variant_columns = st.columns(3)
        variant_name = variant_columns[0].selectbox(
            "策略版本",
            ["adaptive_v3", "adaptive_v2", "adaptive_v1", "legacy"],
            key="etf_variant_research_name",
        )
        variant_start = variant_columns[1].date_input(
            "比较开始",
            value=date(2023, 1, 3),
            key="etf_variant_research_start",
        )
        variant_end = variant_columns[2].date_input(
            "比较结束",
            value=date.today(),
            key="etf_variant_research_end",
        )
        if st.button("运行策略版本比较", key="run_etf_variant_research"):
            try:
                with st.spinner("正在运行策略、等权基准和双倍成本比较..."):
                    st.session_state["etf_variant_research"] = run_etf_variant_research(
                        settings,
                        variant_start,
                        variant_end,
                        variant_name,
                        True,
                    )
            except Exception as exc:
                st.error(f"策略版本比较失败：{exc}")
        variant_output = st.session_state.get("etf_variant_research")
        if variant_output:
            metrics = variant_output["metrics"]
            rows = [{"portfolio": name, **values} for name, values in metrics.items()]
            st.dataframe(rows, width="stretch", hide_index=True)
            st.json(
                {
                    "relative_to_equal_weight": variant_output["relative_to_equal_weight"],
                    "claim_boundary": variant_output["claim_boundary"],
                }
            )
    with st.expander("运行预注册自适应 ETF 候选实验", expanded=False):
        st.caption(
            "按 STRATEGY_RESEARCH_PROTOCOL.md 固定的四个候选运行开发期筛选，"
            "只对开发期冠军打开一次锁定历史留出。结果不会自动替换现役策略。"
        )
        if st.button("运行候选实验室", key="run_adaptive_etf_lab"):
            try:
                with st.spinner("正在运行四候选开发期筛选与锁定留出验证..."):
                    st.session_state["adaptive_etf_lab"] = run_adaptive_etf_candidate_lab(
                        settings, save=True
                    )
                st.success("候选实验完成，结果已进入证据中心。")
            except Exception as exc:
                st.error(f"候选实验失败：{exc}")
    render_evidence(build_evidence_summary(settings))

with replay_tab:
    st.subheader("匿名化历史盲测与假下单")
    st.write(
        "系统自动选择非重叠历史日期，隐藏真实标的和日期后调用多 Agent，"
        "再按 T+1 开盘和真实未来价格结算。"
    )
    replay_start = st.date_input("回放开始", value=date(2025, 1, 1), key="replay_start")
    replay_end = st.date_input("回放结束", value=date(2025, 6, 30), key="replay_end")
    replay_horizon = st.selectbox("持有/预测周期", [20, 5], format_func=lambda x: f"{x}个交易日")
    replay_episodes = st.slider("盲测回合", 1, 12, 2)
    st.caption(
        f"预计真实 LLM 调用约 {replay_episodes * 11} 次；统计模型会按每个历史日期重新做点时训练。"
        "达到30回合 measured 门槛建议通过 CLI 在更长区间分批执行，并先确认模型费用。"
    )
    if st.button("运行历史盲测", type="primary"):
        try:
            with st.spinner("历史快照、点时训练、多 Agent 盲测和假成交正在运行..."):
                st.session_state["historical_replay"] = run_historical_blind_replay(
                    settings,
                    replay_start,
                    replay_end,
                    horizon_days=replay_horizon,
                    episodes=replay_episodes,
                    save=True,
                )
        except Exception as exc:
            st.error(f"历史盲测失败：{exc}")
    if st.session_state.get("historical_replay"):
        render_historical_replay(st.session_state["historical_replay"], "new_replay")
    previous_replays = replay_repository.list(10)
    if previous_replays:
        st.subheader("历史回放记录")
        replay_ids = [item["id"] for item in previous_replays]
        selected_replay = st.selectbox(
            "查看已保存回放",
            replay_ids,
            format_func=lambda replay_id: next(
                f"#{item['id']} · {item['start_date']}~{item['end_date']} · "
                f"{item['episodes']}回合 · {item['status']}"
                for item in previous_replays
                if item["id"] == replay_id
            ),
        )
        stored_replay = replay_repository.get(selected_replay)
        if stored_replay and not (
            st.session_state.get("historical_replay", {}).get("replay_id") == selected_replay
        ):
            render_historical_replay(stored_replay["payload"], "stored_replay")

with overview_tab:
    overview = terminal.portfolio_overview(settings.get("system.initial_capital"))
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("权益", f"¥{overview['equity']:,.2f}")
    col2.metric("现金", f"¥{overview['cash']:,.2f}")
    col3.metric("持仓市值", f"¥{overview['market_value']:,.2f}")
    col4.metric("持仓数", len(overview["positions"]))
    st.dataframe(overview["positions"], width="stretch", hide_index=True)
    st.caption(overview["pricing_warning"])

with learning_tab:
    status = learning_status(settings)
    st.subheader("冠军—挑战者学习闭环")
    st.json(status.get("champion_challenger_history", []))
    st.subheader("训练数据清单与防泄漏契约")
    st.dataframe(status.get("dataset_manifests", []), width="stretch", hide_index=True)
    st.subheader("预测错误归因统计")
    st.json(status.get("attribution_summary", {}))
    st.subheader("激活的时间序列概率模型")
    st.json(status["active_models"])
    st.subheader("样本与训练状态")
    st.json(status["sample_counts"])
    if status.get("drift"):
        st.subheader("漂移监控")
        st.json(status["drift"])
    st.info("新预测在第 5 / 20 个交易日回填真实结果后，会进入下一轮训练与准入评估。")

with llm_tab:
    st.subheader("本次前端运行配置")
    st.json(provider_configuration_summary(settings.section("llm")))
    st.caption("真实 Provider 回放会产生 API 调用和费用，但不会保存 Key 或完整提示词。")
    smoke_col, committee_col = st.columns(2)
    if smoke_col.button("运行两例 LLM 安全回放"):
        try:
            with st.spinner("执行结构化输出、延迟和 token 测试..."):
                replay = run_llm_replay(settings, suite="smoke", runs=1, save=True)
            st.json(replay["summary"])
            st.dataframe(replay["results"], width="stretch", hide_index=True)
        except Exception as exc:
            st.error(f"LLM 回放失败：{exc}")
    if committee_col.button("运行委员会级 LLM 回放"):
        try:
            with st.spinner("验证风险否决、概率预测和最终审核..."):
                replay = run_llm_replay(settings, suite="committee", runs=1, save=True)
            st.json(replay["summary"])
            st.dataframe(replay["results"], width="stretch", hide_index=True)
        except Exception as exc:
            st.error(f"LLM 回放失败：{exc}")
    evaluations = terminal.llm_evaluations(10)
    if evaluations:
        st.subheader("最近回放")
        st.json(
            [
                {
                    "evaluation_id": item["evaluation_id"],
                    "suite": item["suite"],
                    "summary": item["summary"],
                    "created_at": item["created_at"],
                }
                for item in evaluations
            ]
        )

with audit_tab:
    st.subheader("已持久化决策与报告导出")
    recent = decision_repository.recent(50)
    if recent:
        choices = {item["run_id"]: item for item in recent}
        run_id = st.selectbox(
            "选择研究记录",
            list(choices),
            format_func=lambda item: (
                f"{choices[item]['as_of']} · {choices[item]['symbol']} · "
                f"{choices[item]['action']} · {item[:8]}"
            ),
        )
        record = decision_repository.get(run_id)
        if record:
            stored_package = build_stored_audit_package(record)
            render_research(stored_package, "audit")
    else:
        st.info("尚无已保存的研究记录。先在“多 Agent 研究”或“一键 Demo”运行一次。")

with demo_tab:
    st.subheader("黑客松一键演示")
    st.write(
        "真实链路：市场雷达 → 选择强势 ETF → 多 Agent 委员会 → 5/20日预测 → Reviewer → 审计报告。"
    )
    demo_sector = st.checkbox("Demo 同时获取行业快照", value=False, key="demo_sector")
    if st.button("运行真实数据一键 Demo", type="primary"):
        try:
            with st.spinner("正在运行真实行情与多 Agent 全链路；高质量模型可能需要数分钟..."):
                live_output = run_live_demo(settings, include_sectors=demo_sector)
                st.session_state["live_demo"] = live_output
        except Exception as exc:
            st.error(f"真实 Demo 失败：{exc}")
    if st.session_state.get("live_demo"):
        live_output = st.session_state["live_demo"]
        st.success(
            f"真实链路完成，自动选择 {live_output['selected_candidate']['name']} "
            f"({live_output['selected_candidate']['symbol']})"
        )
        render_radar(live_output["radar"])
        render_research(live_output["audit_package"], "demo")

    with st.expander("离线合成数据回退演示"):
        st.caption("只用于无网络时验证软件链路，界面会明确标注，不冒充真实行情。")
        if st.button("运行离线 Demo"):
            with st.spinner("回测与 Mock/已配置 LLM 分析中..."):
                offline = run_demo(settings)
            st.json(offline["backtest"].metrics)
            st.json(offline["decision_run"].decision.model_dump(mode="json"))

st.divider()
st.caption(
    "风险声明：本系统仅用于研究和辅助决策，不构成投资建议。历史回测、模型预测和 Agent 结论均不能保证未来收益。"
)
