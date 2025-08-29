from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

@dataclass
class QueuedOrder:
    due_bar: int
    order: object  # your trader.engine.utils.Order

class BarDelay:
    """
    Simple N-bar execution delay. Call submit(bar_idx, order) when you 'place',
    then at each bar call due(bar_idx) to get orders that should execute now.
    """
    def __init__(self, bars: int = 1):
        self.bars = max(0, int(bars))
        self._q: Deque[QueuedOrder] = deque()

    def submit(self, bar_idx: int, order: object) -> None:
        if self.bars <= 0:
            # Immediate execution path is handled by your engine; nothing to queue.
            return
        self._q.append(QueuedOrder(due_bar=bar_idx + self.bars, order=order))

    def due(self, bar_idx: int) -> List[object]:
        out: List[object] = []
        while self._q and self._q[0].due_bar <= bar_idx:
            out.append(self._q.popleft().order)
        return out
