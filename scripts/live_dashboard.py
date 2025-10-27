#!/usr/bin/env python3
import argparse, csv, json, time
from pathlib import Path
from flask import Flask, jsonify, request, Response, send_from_directory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC_DIR = ROOT / "static"

app = Flask(__name__, static_folder=None)

@app.get("/static/<path:filename>")
def _static(filename):
    return send_from_directory(STATIC_DIR, filename)


# ---- Paths (override with --csv via argparse when launching) ----
CSV_DEFAULT = "data/MES_live_1m.csv"
OOS_LOG = Path("logs/oos_log.csv")
TRADES_CSV = Path("logs/backtest_trades.csv")
SUMMARY_JSON = Path("logs/summary.json")

# ---- Helpers ----
def load_bars(csv_path, max_rows=500):
    rows = []
    p = Path(csv_path)
    if not p.exists():
        return rows
    with p.open("r", newline="") as f:
        r = csv.reader(f)
        for row in r:
            if len(row) >= 5:
                # ts, open, high, low, close, (volume?)
                rows.append(row[:6])
    return rows[-max_rows:] if max_rows and len(rows) > max_rows else rows

def last_bar_age(csv_path):
    rows = load_bars(csv_path, max_rows=1)
    if not rows:
        return {"ts": None, "ageSec": None}
    ts = rows[-1][0]
    try:
        age = max(0, int(time.time() - Path(csv_path).stat().st_mtime))
    except Exception:
        age = None
    return {"ts": ts, "ageSec": age}

def load_oos_last(n=100):
    if not OOS_LOG.exists():
        return {"rows": [], "last": None}
    rows = []
    with OOS_LOG.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(line.split(","))
    tail = rows[-n:] if len(rows) > n else rows
    last = rows[-1] if rows else None

    def asfloat(x):
        try: return float(x)
        except: return None

    last_metrics = None
    if last and len(last) >= 12:
        last_metrics = {
            "ts": last[0],
            "trades": int(float(last[4])) if last[4] else 0,
            "win": asfloat(last[5]),
            "pf": asfloat(last[6]),
            "exp": asfloat(last[7]),
            "netPnL": asfloat(last[8]),
            "fees": asfloat(last[9]),
            "netRet": asfloat(last[10]),
            "mdd": asfloat(last[11]),
        }
    return {"rows": tail, "last": last_metrics}

def load_trades(limit=5000):
    if not TRADES_CSV.exists():
        return []
    rows = []
    with TRADES_CSV.open("r", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(row)
    return rows[-limit:]

def build_equity_from_trades(rows):
    equity = []
    e = 0.0
    for row in rows:
        if (row.get("action") or "").upper() == "EXIT":
            pnl = None
            for key in ("pnl","PnL","pnl_net","net_pnl","profit"):
                if key in row and row[key]:
                    try:
                        pnl = float(row[key]); break
                    except:
                        pass
            if pnl is None:
                try:
                    pnl = float(row.get("R") or 0.0)
                except:
                    pnl = 0.0
            e += pnl
            equity.append(e)
    dd = []
    peak = float("-inf")
    for v in equity:
        peak = max(peak, v)
        dd.append(0.0 if peak <= 0 else (peak - v))
    return equity[-300:], dd[-300:]

# ---- API ----
@app.get("/data")
def data():
    csv_path = request.args.get("csv", app.config.get("CSV_DEFAULT", CSV_DEFAULT))
    max_rows = int(request.args.get("n", "500"))
    rows = load_bars(csv_path, max_rows)
    ts = [r[0] for r in rows]
    close = [float(r[4]) for r in rows]
    vol = []
    for r in rows:
        try: vol.append(float(r[5]))
        except: vol.append(0.0)
    return jsonify({"ts": ts, "close": close, "volume": vol})

@app.get("/meta")
def meta():
    csv_path = request.args.get("csv", app.config.get("CSV_DEFAULT", CSV_DEFAULT))
    hb = last_bar_age(csv_path)
    oos = load_oos_last()
    return jsonify({"lastBar": hb, "oosLast": oos["last"], "csvPath": csv_path})

@app.get("/trades")
def trades():
    rows = load_trades()
    entries, exits = [], []
    for r in rows[-600:]:
        ts = r.get("ts") or r.get("time") or r.get("timestamp")
        act = (r.get("action") or "").upper()
        side = (r.get("side") or "").upper()
        price = r.get("price") or r.get("fill_price") or r.get("avg_price")
        try: price = float(price) if price else None
        except: price = None
        item = {"ts": ts, "price": price, "side": side}
        if act == "ENTRY": entries.append(item)
        elif act == "EXIT": exits.append(item)
    equity, dd = build_equity_from_trades(rows)
    return jsonify({"entries": entries, "exits": exits, "equity": equity, "dd": dd})

@app.get("/oos_trend")
def oos_trend():
    n = int(request.args.get("n", "30"))
    data = load_oos_last(n=n)
    ts, pf, win = [], [], []
    for row in data["rows"]:
        if len(row) >= 12:
            ts.append(row[0])
            try: pf.append(float(row[6]))
            except: pf.append(None)
            try: win.append(float(row[5]))
            except: win.append(None)
    return jsonify({"ts": ts, "pf": pf, "win": win})

# ---- UI ----
@app.get("/")
def index():
    # Pure HTML, no f-string braces required
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Tradebot Live Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { font-family: -apple-system, system-ui, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 1rem; }
    .grid { display: grid; grid-template-columns: 2fr 1fr; gap: 1rem; }
    .row { display: grid; grid-template-columns: 1fr; gap: 1rem; }
    @media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
    canvas { width: 100%; height: 360px; }
    .card { border: 1px solid #e5e7eb; border-radius: 12px; padding: 12px; }
    .kvs span { display:inline-block; min-width: 110px; }
  </style>
</head>
<body>
  <h2>Tradebot Live Dashboard</h2>
  <p id="csvInfo"></p>

  <div class="grid">
    <div class="row">
      <div class="card"><h3>Close Price (with Entries/Exits)</h3><canvas id="price"></canvas></div>
      <div class="card"><h3>Volume</h3><canvas id="volume"></canvas></div>
      <div class="card"><h3>Equity / Drawdown (from exits)</h3><canvas id="equity"></canvas></div>
    </div>
    <div class="row">
      <div class="card">
        <h3>Heartbeat & OOS</h3>
        <div class="kvs" id="metaBox">Loading…</div>
        <div id="nextTick" style="opacity:.7;margin-top:6px;font-size:.9rem"></div>
      </div>
      <div class="card"><h3>OOS Trend (last 30)</h3><canvas id="oosTrend"></canvas></div>
    </div>
  </div>

  <script src="/static/dashboard.js"></script>
</body>
</html>"""
    return Response(html, mimetype="text/html")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=CSV_DEFAULT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5055)
    args = ap.parse_args()
    app.config["CSV_DEFAULT"] = args.csv
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()
