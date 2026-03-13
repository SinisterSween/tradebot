from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Protocol
from pathlib import Path
import pandas as pd

from trader.engine.backtest import prepare_bars


@dataclass
class FeatureSnapshot:
    symbol: str
    fdf: pd.DataFrame        # full features df (tail)
    bar: pd.Series           # latest row (fdf.iloc[-1])


class MarketDataProvider(Protocol):
    async def get_features(self, symbol: str) -> Optional[FeatureSnapshot]:
        ...
