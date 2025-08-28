def per_side_cost(commission_per_contract: float, exchange_fees_per_contract: float, qty: int) -> float:
    return (commission_per_contract + exchange_fees_per_contract) * qty
