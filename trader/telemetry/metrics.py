from prometheus_client import start_http_server, Counter, Gauge, Histogram
orders_submitted = Counter("orders_submitted_total", "Total orders submitted", ["side"])
orders_filled = Counter("orders_filled_total", "Total orders filled", ["side", "reason"])
pnl_realized = Gauge("pnl_realized_dollars", "Realized PnL in USD (IB reported)")
pnl_unrealized = Gauge("pnl_unrealized_dollars", "Unrealized PnL in USD (IB reported)")
open_positions = Gauge("open_positions", "Open positions count")
latency_ms = Histogram("latency_ms", "Order round-trip latency (ms)", buckets=(10,25,50,100,200,400,800,1600,3200))
data_age_seconds = Gauge("data_age_seconds", "Age of latest received bar in seconds")
def start_metrics_server(host: str, port: int):
    start_http_server(port, addr=host)
