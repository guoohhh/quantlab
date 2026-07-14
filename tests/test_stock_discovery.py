from datetime import date, timedelta
from math import sin

import pytest

from quantlab.config import Settings
from quantlab.persistence import TerminalRepository
from quantlab.workflows import stock_discovery as discovery_module
from quantlab.workflows.stock_discovery import (
    _diversified_shortlist,
    normalize_stock_symbol,
    parse_stock_symbols,
    recommend_stocks,
    run_stock_research_batch,
    screen_selected_stocks,
    search_stocks,
)


def _settings(tmp_path):
    return Settings(
        values={
            "system": {
                "database_path": "quantlab.db",
                "data_dir": "data",
            }
        },
        root=tmp_path,
    )


def _bars(series: dict[str, list[float]]) -> list[dict]:
    output = []
    start = date(2025, 1, 1)
    for symbol, returns in series.items():
        price = 20.0
        for index, daily_return in enumerate(returns):
            previous = price
            price *= 1 + daily_return
            output.append(
                {
                    "symbol": symbol,
                    "date": start + timedelta(days=index),
                    "open": previous,
                    "high": max(previous, price) * 1.01,
                    "low": min(previous, price) * 0.99,
                    "close": price,
                    "adjusted_close": price,
                    "volume": 5_000_000,
                    "amount": 100_000_000,
                }
            )
    return output


class FakeSearchProvider:
    name = "fake-search"

    def search_stocks(self, keyword, limit):
        return {
            "data": [
                {"code": "600519.SH", "name": "贵州茅台", "industry": "白酒"},
                {"code": "000858.SZ", "name": "五粮液", "industry": "白酒"},
                {"code": "430001.BJ", "name": "北交所样本"},
            ]
        }


class FakeScreener:
    name = "fake-screener"

    def filter(self, expression, limit, orderby, ascending):
        if "DividendRatioTTM" in expression:
            raise RuntimeError("dividend feed unavailable")
        if "RevenueGrowRate" in expression:
            return [
                {
                    "code": "000858.SZ",
                    "name": "五粮液",
                    "ROETTM": 24,
                    "PE_TTM": 18,
                    "DebtRatio": 30,
                    "RevenueGrowRate": 20,
                    "NetProfitGrowRate": 18,
                },
                {
                    "code": "300750.SZ",
                    "name": "宁德时代",
                    "ROETTM": 20,
                    "PE_TTM": 24,
                    "DebtRatio": 45,
                    "RevenueGrowRate": 25,
                    "NetProfitGrowRate": 22,
                },
            ]
        return [
            {
                "code": "600519.SH",
                "name": "贵州茅台",
                "ROETTM": 30,
                "PE_TTM": 20,
                "DebtRatio": 20,
                "RevenueGrowRate": 15,
                "NetProfitGrowRate": 15,
            },
            {
                "code": "000858.SZ",
                "name": "五粮液",
                "ROETTM": 24,
                "PE_TTM": 18,
                "DebtRatio": 30,
                "RevenueGrowRate": 20,
                "NetProfitGrowRate": 18,
            },
        ]


def test_stock_symbol_normalization_and_batch_parsing():
    assert normalize_stock_symbol("600519") == "sh600519"
    assert normalize_stock_symbol("600519.SH") == "sh600519"
    assert normalize_stock_symbol("SZ000858") == "sz000858"
    assert parse_stock_symbols("600519, 000858；600519.SH") == ["sh600519", "sz000858"]
    with pytest.raises(ValueError, match="unsupported"):
        normalize_stock_symbol("430001.BJ")
    with pytest.raises(ValueError, match="at most 2"):
        parse_stock_symbols(["600519", "000858", "300750"], maximum=2)


def test_stock_search_normalizes_results_and_rejects_unsupported_markets(tmp_path):
    output = search_stocks(
        _settings(tmp_path),
        "白酒",
        provider=FakeSearchProvider(),
    )

    assert [item["symbol"] for item in output["results"]] == ["sh600519", "sz000858"]
    assert output["results"][0]["industry"] == "白酒"
    assert output["rejected_codes"] == ["430001.BJ"]


def test_selected_stock_screen_ranks_with_factor_financial_and_liquidity_data(tmp_path):
    first = [0.0015 + 0.006 * sin(index / 5) for index in range(300)]
    second = [0.0005 + 0.005 * sin(index / 7) for index in range(300)]
    short = [0.001] * 80
    metadata = {
        "sh600519": {
            "name": "贵州茅台",
            "industry": "白酒",
            "ROETTM": 30,
            "PE_TTM": 20,
            "DebtRatio": 20,
            "RevenueGrowRate": 15,
            "NetProfitGrowRate": 15,
        },
        "sz000858": {
            "name": "五粮液",
            "industry": "白酒",
            "ROETTM": 20,
            "PE_TTM": 25,
            "DebtRatio": 35,
        },
    }

    output = screen_selected_stocks(
        _settings(tmp_path),
        ["600519", "000858", "300750"],
        date(2025, 10, 27),
        bars=_bars({"sh600519": first, "sz000858": second, "sz300750": short}),
        metadata=metadata,
        save=False,
    )

    assert output["candidates"][0]["symbol"] == "sh600519"
    assert output["candidates"][0]["financial_snapshot_coverage"] == 5
    assert output["candidates"][0]["screen_score"] > 50
    insufficient = next(item for item in output["candidates"] if item["symbol"] == "sz300750")
    assert insufficient["status"] == "insufficient_history"
    assert output["recommendation_boundary"].startswith("screening results")


def test_stock_shortlist_deduplicates_positive_but_not_negative_correlation():
    candidates = [
        {"symbol": "A", "eligible": True, "recommendation_tier": "research_first"},
        {"symbol": "B", "eligible": True, "recommendation_tier": "watch"},
        {"symbol": "C", "eligible": True, "recommendation_tier": "watch"},
        {"symbol": "D", "eligible": False, "recommendation_tier": "avoid"},
    ]
    correlation = {
        "A": {"A": 1.0, "B": 0.95, "C": -0.8, "D": 0.0},
        "B": {"A": 0.95, "B": 1.0, "C": -0.7, "D": 0.0},
        "C": {"A": -0.8, "B": -0.7, "C": 1.0, "D": 0.0},
        "D": {"A": 0.0, "B": 0.0, "C": 0.0, "D": 1.0},
    }

    selected = _diversified_shortlist(candidates, correlation, 0.85)

    assert [item["symbol"] for item in selected] == ["A", "C"]
    assert candidates[1]["diversification_status"] == "excluded_high_correlation:A"
    assert candidates[3]["diversification_status"] == "not_eligible"


def test_stock_screen_penalizes_parabolic_high_volatility_moves(tmp_path):
    returns = [0.08 if index % 2 == 0 else -0.02 for index in range(300)]

    output = screen_selected_stocks(
        _settings(tmp_path),
        ["600519"],
        date(2025, 10, 27),
        bars=_bars({"sh600519": returns}),
        metadata={
            "sh600519": {
                "name": "高波动样本",
                "ROETTM": 30,
                "PE_TTM": 15,
                "DebtRatio": 20,
            }
        },
        save=False,
    )

    candidate = output["candidates"][0]
    assert candidate["score_trace"]["risk_adjustment"] < -0.5
    assert any("volatility is high" in item for item in candidate["risks"])
    assert any("overextended" in item for item in candidate["risks"])


def test_system_recommendation_merges_styles_and_discloses_partial_failure(tmp_path):
    returns = {
        "sh600519": [0.0015 + 0.005 * sin(index / 5) for index in range(300)],
        "sz000858": [0.001 + 0.004 * sin(index / 6) for index in range(300)],
        "sz300750": [0.0008 + 0.007 * sin(index / 4) for index in range(300)],
    }

    output = recommend_stocks(
        _settings(tmp_path),
        date(2025, 10, 27),
        styles=["momentum_quality", "growth_quality", "high_dividend"],
        candidate_limit=5,
        top_n=3,
        save=False,
        screener=FakeScreener(),
        bars=_bars(returns),
    )

    assert output["run_type"] == "system_recommendation"
    assert output["universe_size"] == 3
    wuliangye = next(item for item in output["candidates"] if item["symbol"] == "sz000858")
    assert set(wuliangye["style_sources"]) == {"momentum_quality", "growth_quality"}
    assert "high_dividend stock discovery failed" in output["degraded_sources"][0]


def test_system_recommendation_does_not_manufacture_empty_results(tmp_path):
    class EmptyScreener:
        name = "empty"

        def filter(self, *args, **kwargs):
            return []

    output = recommend_stocks(
        _settings(tmp_path),
        date.today(),
        styles=["value_quality"],
        candidate_limit=5,
        top_n=3,
        save=False,
        screener=EmptyScreener(),
    )

    assert output["candidates"] == []
    assert output["diversified_shortlist"] == []
    assert output["degraded_sources"] == ["no stocks matched the selected styles"]
    with pytest.raises(ValueError, match="current market snapshot"):
        recommend_stocks(_settings(tmp_path), date(2025, 1, 1), save=False)


def test_stock_discovery_persistence_round_trip(tmp_path):
    settings = _settings(tmp_path)
    repository = TerminalRepository(tmp_path / "quantlab.db")
    discovery_id = repository.save_stock_discovery(
        "user_selected",
        date(2026, 7, 13),
        {"candidates": [{"symbol": "sh600519"}], "diversified_shortlist": []},
    )

    assert repository.stock_discovery_runs(1)[0]["candidate_count"] == 1
    assert repository.stock_discovery(discovery_id)["run_type"] == "user_selected"
    assert repository.stock_discovery(999_999) is None
    assert settings.resolve("quantlab.db") == tmp_path / "quantlab.db"


def test_stock_research_batch_isolates_one_symbol_failure(tmp_path, monkeypatch):
    def analyzer(settings, symbol, as_of, **kwargs):
        if symbol == "sz000858":
            raise RuntimeError("financial endpoint unavailable")
        return {"symbol": symbol}

    monkeypatch.setattr(
        discovery_module,
        "build_research_audit_package",
        lambda research: {
            "run_id": "run-1",
            "symbol": research["symbol"],
            "as_of": "2026-07-13",
            "decision": {
                "action": "watch",
                "confidence": 0.6,
                "requires_human_review": False,
            },
            "agent_reports": {"reviewer": {"approved": True}},
        },
    )

    output = run_stock_research_batch(
        _settings(tmp_path),
        ["600519", "000858"],
        date(2026, 7, 13),
        save=False,
        analyzer=analyzer,
    )

    assert len(output["analyses"]) == 1
    assert output["summaries"][0]["reviewer_approved"] is True
    assert output["failures"][0]["symbol"] == "sz000858"
    assert output["summaries"][1]["action"] == "review_required"
    assert output["estimated_llm_role_calls"] == 36
