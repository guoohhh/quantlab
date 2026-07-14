from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

from quantlab.domain.models import StrategySignal


class Strategy(ABC):
    name: str

    @abstractmethod
    def generate(self, as_of: date, data: pd.DataFrame, **context) -> list[StrategySignal]: ...


def rank_to_unit(values: pd.Series, ascending: bool = True) -> pd.Series:
    if len(values) <= 1:
        return pd.Series([1.0] * len(values), index=values.index)
    rank = values.rank(pct=True, ascending=ascending)
    return rank * 2 - 1
