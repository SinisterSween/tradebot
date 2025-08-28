def market_slippage(price: float, side: str, tick_size: float) -> float:
    adj = tick_size * 0.5
    return price + (adj if side == "BUY" else -adj)
