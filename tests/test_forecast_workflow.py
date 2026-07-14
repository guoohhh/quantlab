import asyncio
from datetime import date, timedelta

import pytest

from quantlab.agents import MultiAgentDecisionSystem, ResearchContext
from quantlab.domain.models import Bar
from quantlab.llm import MockLLMProvider
from quantlab.persistence import DecisionRepository
from quantlab.workflows import forecast as forecast_workflow


class FakeProvider:
    name = "fake"

    def __init__(self, *args, **kwargs):
        self.last_degraded_from = []

    def bars(self, symbols, start, end):
        return [
            Bar(
                symbol=symbols[0],
                date=start + timedelta(days=index),
                open=100 + index,
                high=101 + index,
                low=99 + index,
                close=100 + index,
            )
            for index in range(21)
        ]


class PassthroughCache:
    def __init__(self, wrapped, *args, **kwargs):
        self.wrapped = wrapped

    def bars(self, symbols, start, end):
        return self.wrapped.bars(symbols, start, end)


def test_settle_forecasts_uses_trading_bar_horizon(settings, monkeypatch):
    run = asyncio.run(
        MultiAgentDecisionSystem(MockLLMProvider()).run(
            ResearchContext(symbol="sh510300", as_of=date(2026, 1, 2), price=100)
        )
    )
    repository = DecisionRepository(settings.resolve(settings.get("system.database_path")))
    repository.save(run)
    monkeypatch.setattr(forecast_workflow, "FallbackProvider", FakeProvider)
    monkeypatch.setattr(forecast_workflow, "CachedProvider", PassthroughCache)

    output = forecast_workflow.settle_forecasts(settings, date(2026, 2, 1))

    assert len(output["settled"]) == 2
    assert output["settled"][0]["realized_return_pct"] == pytest.approx(5.0)
