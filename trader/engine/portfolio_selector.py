from dataclasses import dataclass
from typing import Optional, Dict, Any, List

@dataclass
class Candidate:
    symbol: str
    action: Optional[str]   # "LONG"/"SHORT"/None
    score: float
    meta: Dict[str, Any]

class PortfolioSelector:
    def choose(self, candidates: List[Candidate]) -> Optional[Candidate]:
        cands = [c for c in candidates if c.action and c.score > 0]
        if not cands:
            return None
        cands.sort(key=lambda c: c.score, reverse=True)
        return cands[0]
