def market_slippage(price: float, side: str, tick_size: float, bps: float | None = None) -> float:
    """Apply slippage. If bps is given, use it; otherwise default to half-tick."""
    if bps is not None:
        adj = price * (bps / 10_000.0)
    else:
        adj = tick_size * 0.5
    return price + (adj if side == "BUY" else -adj)