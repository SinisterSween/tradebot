#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."  # repo root

# --- Config (override with env) ---
CSV="${CSV:-data/MES_live_1m.csv}"
OOS_LOG="${OOS_LOG:-logs/oos_log.csv}"
SHADOW_LOG="${SHADOW_LOG:-logs/shadow.out}"
OOS_LOOP_LOG="${OOS_LOOP_LOG:-logs/oos_loop.out}"
STALE_SEC="${STALE_SEC:-1800}"  # 30 min default; set to 1200 to match delayed staleness gate
STRICT="${STRICT:-0}"           # 1 = nonzero exit on failures

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "✅ %s\n" "$*"; }
warn() { printf "⚠️  %s\n" "$*" >&2; }
err()  { printf "❌ %s\n" "$*" >&2; }

have_proc() {
  pgrep -fl "$1" >/dev/null 2>&1
}

last_csv_ts() {
  # prints ISO timestamp from last non-empty line of CSV
  awk -F',' 'NF>1{last=$1} END{print last}' "${CSV}" 2>/dev/null || true
}

age_since_ts() {
  # Args: ISO ts string (e.g., 2025-10-17 20:55:00+00:00)
  # Prints age in seconds (integer). Uses python for robust parsing.
  python - <<'PY' "$1" 2>/dev/null || echo ""
import sys, datetime as dt
s = sys.argv[1]
try:
    # Normalize: allow "+00:00" tzinfo
    t = dt.datetime.fromisoformat(s.replace("Z","+00:00"))
    now = dt.datetime.now(dt.timezone.utc)
    print(int((now - t).total_seconds()))
except Exception:
    print("")
PY
}

hrule() { printf "\n%s\n" "----------------------------------------"; }

EXIT=0
bold "Tradebot Health Snapshot"

# --- Processes ---
hrule
bold "Processes"
if have_proc "shadow_mes_delayed.py"; then ok "Feeder running (shadow_mes_delayed.py)"; else err "Feeder NOT running"; EXIT=1; fi
if have_proc "run_oos_loop.py"; then ok "OOS loop running (run_oos_loop.py)"; else warn "OOS loop NOT running"; fi

# --- Feeder logs ---
hrule
bold "Feeder log tail (${SHADOW_LOG})"
if [[ -s "${SHADOW_LOG}" ]]; then
  tail -n 30 "${SHADOW_LOG}" || true
else
  warn "No feeder log found at ${SHADOW_LOG}"
fi

# --- CSV freshness ---
hrule
bold "CSV freshness (${CSV})"
if [[ -s "${CSV}" ]]; then
  TS="$(last_csv_ts)"
  if [[ -n "${TS}" ]]; then
    AGE="$(age_since_ts "${TS}")"
    if [[ -n "${AGE}" ]]; then
      MIN=$(( AGE / 60 ))
      echo "Last bar: ${TS}  (age: ${AGE}s ~ ${MIN}m)"
      if (( AGE > STALE_SEC )); then
        err "CSV is stale (> ${STALE_SEC}s)."
        EXIT=1
      else
        ok "CSV freshness OK (<= ${STALE_SEC}s)."
      fi
    else
      warn "Could not parse last timestamp from CSV."
    fi
  else
    warn "CSV exists but no valid rows found."
  fi
else
  err "CSV file missing or empty: ${CSV}"
  EXIT=1
fi

# --- OOS loop log + last summary row ---
hrule
bold "OOS loop log tail (${OOS_LOOP_LOG})"
if [[ -s "${OOS_LOOP_LOG}" ]]; then
  tail -n 30 "${OOS_LOOP_LOG}" || true
else
  warn "No OOS loop log found at ${OOS_LOOP_LOG}"
fi

hrule
bold "Last OOS summary row (${OOS_LOG})"
if [[ -s "${OOS_LOG}" ]]; then
  tail -n 1 "${OOS_LOG}" || true
else
  warn "No OOS summary file found at ${OOS_LOG}"
fi

# --- Quick NOOP heartbeat check ---
hrule
bold "Feeder NOOP heartbeats (closed market indicator)"
if [[ -f "${SHADOW_LOG}" ]]; then
  COUNT=$(grep -c "NOOP" "${SHADOW_LOG}" || true)
  echo "NOOP count in feeder log: ${COUNT}"
fi

# --- Exit behavior ---
if [[ "${STRICT}" == "1" ]]; then
  if (( EXIT != 0 )); then
    err "Health check failed (STRICT=1)."
    exit 1
  fi
fi

ok "Health check complete."
exit 0
