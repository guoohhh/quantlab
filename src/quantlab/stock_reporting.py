from __future__ import annotations

from typing import Any


def render_stock_ranking_replay_markdown(replay: dict[str, Any]) -> str:
    metrics = replay["metrics"]
    comparisons = replay["paired_comparisons"]
    market_wide = replay.get("universe_scope") == "historical_market_snapshot_stratified_sample"
    lines = [
        "# A股全市场点时分层回放" if market_wide else "# A股固定池点时排名回放",
        "",
        f"- 回放编号：{replay.get('replay_id', 'unsaved')}",
        f"- 区间：{replay['requested_range']['start']} 至 {replay['requested_range']['end']}",
        f"- 周期：{replay['horizon_days']} 个交易日",
        f"- 有效回合：{replay['completed_episodes']}",
        f"- 证据等级：{replay['evidence_status']}",
        f"- {'分层样本协议' if market_wide else '固定股票池'}哈希：`{replay['universe_hash']}`",
        "",
        "## 组合结果",
        "",
        "| 组合 | 累计收益 | 单回合胜率 | 最大回撤 | 参与率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in metrics.items():
        lines.append(
            f"| {name} | {item['total_return']:.2%} | {item['episode_win_rate']:.2%} | "
            f"{item['max_drawdown']:.2%} | {item['participation_rate']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 配对超额统计",
            "",
            "| 系统 | 基准 | 平均超额 | 超额为正比例 | bootstrap均值为正概率 | 90%区间 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in comparisons.values():
        interval = item["bootstrap_90pct_interval"]
        lines.append(
            f"| {item['system']} | {item['baseline']} | {item['mean_excess_return']:.3%} | "
            f"{item['positive_excess_rate']:.2%} | "
            f"{item['probability_mean_excess_positive']:.2%} | "
            f"[{interval[0]:.3%}, {interval[1]:.3%}] |"
        )
    qualification = replay["evidence_qualification"]
    if market_wide:
        survivorship = replay.get("survivorship_audit", {})
        admission = replay.get("strategy_admission", {})
        lines.extend(
            [
                "",
                "## 幸存者偏差审计",
                "",
                f"- 历史市场快照：{len(replay.get('snapshot_audits', []))} 个",
                f"- 样本中的最终退市观测："
                f"{survivorship.get('sampled_eventual_delisted_observations', 0)}",
                f"- 持有期内退市观测："
                f"{survivorship.get('delisted_within_horizon_observations', 0)}",
                f"- 历史ST日状态排除：{survivorship.get('historical_st_exclusions', 0)}",
                "",
                "## 策略准入",
                "",
                f"- 状态：{admission.get('status', 'not_evaluated')}",
                f"- 是否通过：{admission.get('passed', False)}",
                f"- 推荐模式：{admission.get('preferred_variant')}",
                f"- 部署建议：{admission.get('deployment_recommendation', 'research only')}",
            ]
        )
    lines.extend(
        [
            "",
            "## 证据契约",
            "",
            f"- 选点规则：{replay['selection_rule']}",
            f"- 成交规则：{replay['execution_contract']}",
            f"- {'点时市场证据' if market_wide else '固定池声明'}是否合格："
            f"{qualification['qualified']}",
            f"- 合格范围：{qualification['qualification_scope']}",
            f"- 是否具备全市场点时股票池："
            f"{qualification['point_in_time_market_universe_available']}",
            f"- 学习样本：{replay['learning_samples']['count']} 条，"
            f"training_eligible={replay['learning_samples']['training_eligible']}",
            "",
            "## 声明边界",
            "",
            replay["claim_boundary"],
            "",
        ]
    )
    return "\n".join(lines)
