from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from quantlab.agents.schemas import CouncilReport, ExpertOpinion


@dataclass(frozen=True)
class AgentRoleSpec:
    name: str
    perspective: str
    weight: float
    mode: str
    instruction: str


TACTICAL_ROLES = {
    "technical": AgentRoleSpec(
        "technical",
        "price structure and multi-timeframe trend",
        1.3,
        "vote",
        "Evaluate daily/weekly/monthly trend, support, volatility and price-volume confirmation. "
        "Do not infer business quality or valuation.",
    ),
    "momentum": AgentRoleSpec(
        "momentum",
        "momentum quality and continuation",
        1.5,
        "vote",
        "Prioritize momentum acceleration, path quality, asymmetric volume and pullback-reversal "
        "conditions. Strong recent return with a noisy path is lower quality.",
    ),
    "value_veto": AgentRoleSpec(
        "value_veto",
        "extreme valuation and solvency veto",
        0.6,
        "veto_only",
        "Use Buffett and Graham principles only as an extreme-risk veto: owner earnings, balance-sheet "
        "strength, cash conversion and margin of safety. Missing valuation data cannot become approval.",
    ),
    "risk": AgentRoleSpec(
        "risk",
        "capital preservation and trade feasibility",
        1.0,
        "veto_only",
        "Check drawdown, liquidity, volatility, concentration, event risk, degraded data and invalidation. "
        "Veto when loss cannot be bounded or evidence is materially unreliable.",
    ),
    "macro": AgentRoleSpec(
        "macro",
        "market regime and liquidity",
        0.7,
        "vote",
        "Assess whether trend, range, bear or high-volatility regime supports this strategy. "
        "Do not invent macro releases that are not supplied.",
    ),
}


MASTER_ROLES = {
    "buffett": AgentRoleSpec(
        "buffett",
        "moat, owner earnings and management integrity",
        1.0,
        "strategic",
        "Assess understandable business economics, durable moat, pricing power, owner earnings, "
        "management integrity and margin of safety. Integrity is a hard concern.",
    ),
    "munger": AgentRoleSpec(
        "munger",
        "inversion, incentives and failure paths",
        1.0,
        "strategic",
        "Invert the thesis. Identify incentives, cognitive traps, accounting fragility and every plausible "
        "path to permanent capital loss. Ask why an intelligent skeptic refuses the investment.",
    ),
    "graham": AgentRoleSpec(
        "graham",
        "financial strength and margin of safety",
        0.9,
        "strategic",
        "Focus on earnings stability, liquid assets, leverage, dilution and conservative intrinsic value. "
        "Do not reward growth without a measurable margin of safety.",
    ),
    "fisher": AgentRoleSpec(
        "fisher",
        "quality growth and management execution",
        0.9,
        "strategic",
        "Evaluate long runway, R&D productivity, margin durability, sales organization, management depth "
        "and insider alignment. Distinguish durable growth from temporary acceleration.",
    ),
    "lynch": AgentRoleSpec(
        "lynch",
        "growth at a reasonable price",
        0.8,
        "strategic",
        "Classify the company type, test earnings growth against valuation, balance-sheet strength and cash "
        "generation. Prefer an explainable story backed by numbers.",
    ),
}


def route_roles(asset_type: str, has_fundamentals: bool = True) -> list[AgentRoleSpec]:
    if asset_type == "etf":
        return [
            TACTICAL_ROLES["technical"],
            TACTICAL_ROLES["momentum"],
            replace(
                TACTICAL_ROLES["value_veto"],
                perspective="ETF structure, valuation extension and permanent-loss scenarios",
                mode="strategic",
                instruction=(
                    "For an ETF, assess holdings concentration, leverage, tracking, closure, premium or "
                    "discount and valuation extension only when supplied. Missing company fundamentals or "
                    "look-through valuation lowers confidence but is not a veto and must not be treated as "
                    "proof of permanent loss. Provide a signed strategic score, not an execution veto."
                ),
            ),
            replace(
                TACTICAL_ROLES["risk"],
                perspective="ETF market risk, drawdown and portfolio fit",
                mode="strategic",
                instruction=(
                    "Assess volatility, drawdown, concentration, regime and implementation uncertainty. "
                    "The raw strategy target is advisory; use the supplied maximum final weight when present. "
                    "Missing microstructure evidence is unassessed risk, not a reason to veto a research signal. "
                    "Provide a signed strategic score; deterministic execution and portfolio engines enforce "
                    "the final hard limits."
                ),
            ),
            TACTICAL_ROLES["macro"],
        ]
    if asset_type == "convertible_bond":
        return [TACTICAL_ROLES[name] for name in ("technical", "value_veto", "risk", "macro")]
    roles = list(TACTICAL_ROLES.values())
    if has_fundamentals:
        roles.extend(MASTER_ROLES.values())
    return roles


def aggregate_council(opinions: list[ExpertOpinion], market_regime: str) -> CouncilReport:
    tactical = [item for item in opinions if item.mode == "vote"]
    strategic = [item for item in opinions if item.mode == "strategic"]
    tactical_denominator = sum(item.weight * item.confidence for item in tactical) or 1.0
    tactical_score = (
        sum(item.score * item.weight * item.confidence for item in tactical) / tactical_denominator
    )
    strategic_score = None
    if strategic:
        strategic_denominator = sum(item.weight * item.confidence for item in strategic) or 1.0
        strategic_score = (
            sum(item.score * item.weight * item.confidence for item in strategic)
            / strategic_denominator
        )
    combined = (
        tactical_score
        if strategic_score is None
        else 0.65 * tactical_score + 0.35 * strategic_score
    )
    veto_roles = [item.role for item in opinions if item.mode == "veto_only" and item.veto]
    by_role = {item.role: item for item in opinions}
    momentum = by_role.get("momentum")
    technical = by_role.get("technical")
    sync = bool(
        momentum
        and technical
        and abs(momentum.score) > 0.1
        and abs(technical.score) > 0.1
        and np.sign(momentum.score) == np.sign(technical.score)
    )
    return CouncilReport(
        tactical_score=float(np.clip(tactical_score, -1, 1)),
        strategic_score=(
            float(np.clip(strategic_score, -1, 1)) if strategic_score is not None else None
        ),
        combined_score=float(np.clip(combined, -1, 1)),
        veto_triggered=bool(veto_roles),
        veto_roles=veto_roles,
        momentum_tech_sync=sync,
        market_regime=market_regime,
        opinions=opinions,
        summary=(
            f"tactical={tactical_score:.3f}; strategic={strategic_score}; "
            f"veto={','.join(veto_roles) if veto_roles else 'none'}"
        ),
    )
