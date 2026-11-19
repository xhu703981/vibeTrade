#!/usr/bin/env bash
# One-command local start: Surfpool forks (8899/8999) -> fund the temporary fork wallet -> backend :8000 -> frontend :5173 (Vite proxies /api to the backend).
#
#   scripts/dev.sh            rehearsal mode: discovery replays the latest runs/m3_*.json, research uses the cache only (no real money at all)
#   scripts/dev.sh --lan      also open the UI to phones on the same Wi-Fi (add --https for the phone microphone: self-signed HTTPS)
#   scripts/dev.sh live       live mode: live DexScreener discovery + real-money x402 research (Base wallet, capped by the mandate's research budget)
#   scripts/dev.sh mainnet    MAINNET REAL FUNDS: like live, and approved cards send real Solana mainnet swaps (type MAINNET; $5/trade ceiling)
#
# Trading only ever happens on the local Surfpool fork (temporary wallet), never mainnet. Ctrl-C stops backend and frontend (Surfpool keeps running; stop it with scripts/stop_surfpool.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=rehearsal; LAN=0; HTTPS=0
for a in "$@"; do
  case "$a" in
    --lan) LAN=1 ;;
    --https) HTTPS=1 ;;
    live|rehearsal|mainnet) MODE="$a" ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done
VITE_HOST=127.0.0.1; [ "$LAN" = 1 ] && VITE_HOST=0.0.0.0
SCHEME=http; [ "$HTTPS" = 1 ] && SCHEME=https && export VIBE_HTTPS=1
PY="${PY:-.venv/bin/python}"
mkdir -p logs

if [ "$MODE" = "mainnet" ]; then
  export VT_NETWORK=mainnet VIBE_DISCOVERY=live VIBE_RESEARCH="${VIBE_RESEARCH:-live}"
  echo "!!! MAINNET REAL FUNDS: every approved card sends a real Solana mainnet swap; ceiling \$${VT_MAINNET_MAX_TRADE_USD:-5} per trade; research=$VIBE_RESEARCH"
  read -r -p "Continue with real money? Type MAINNET: " ans
  [ "$ans" = "MAINNET" ] || { echo "cancelled"; exit 1; }
elif [ "$MODE" = "live" ]; then
  export VT_NETWORK=fork
  export VIBE_DISCOVERY=live VIBE_RESEARCH=live
  echo "WARNING live mode: paid research spends real USDC from the Base research wallet (at most \$0.08 per coin, never more than the mandate research budget)."
  read -r -p "Continue? Type yes: " ans
  [ "$ans" = "yes" ] || { echo "cancelled"; exit 1; }
else
  export VT_NETWORK=fork
  export VIBE_DISCOVERY="${VIBE_DISCOVERY:-replay}" VIBE_RESEARCH="${VIBE_RESEARCH:-cache}"
fi
export VIBE_UI_BUTTONS="${VIBE_UI_BUTTONS:-1}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # same as dev.ps1: UTF-8 files and logs regardless of locale
echo "mode: discovery=$VIBE_DISCOVERY research=$VIBE_RESEARCH ui_buttons=$VIBE_UI_BUTTONS"

scripts/start_surfpool.sh
if [ "$VT_NETWORK" = "mainnet" ]; then
  "$PY" scripts/mainnet_check.py || { echo "mainnet preflight failed: not starting (nothing was sent)"; exit 1; }
else
  "$PY" scripts/fund_wallets.py --sol 2 --usdc 50
fi

# Fork memory watchdog: warn (never kill: the fork holds the positions) when a Surfpool fork gets big.
# Restart the forks between long sessions with scripts/stop_surfpool.sh (positions on the fork are lost).
WARN_MB="${SURFPOOL_RSS_WARN_MB:-6000}"
watchdog() {
  local warned=0
  while sleep 60; do
    for n in main scratch; do
      local pid; pid=$(cat ".surfpool/$n/pid" 2>/dev/null) || continue
      local mb; mb=$(awk '/VmRSS/ {print int($2/1024)}' "/proc/$pid/status" 2>/dev/null) || continue
      [ -n "$mb" ] || continue
      echo "$(date '+%F %T') $n ${mb}MB" >> logs/fork_memory.log
      if [ "$mb" -gt "$WARN_MB" ] && [ "$warned" -eq 0 ]; then
        echo "WARNING: Surfpool fork '$n' uses ${mb} MB (limit ${WARN_MB} MB). Sell open positions, then restart: scripts/stop_surfpool.sh && scripts/dev.sh" >&2
        warned=1
      fi
    done
  done
}

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

watchdog &
pids+=($!)

"$PY" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 > logs/backend.out 2>&1 &
pids+=($!)
for _ in $(seq 1 30); do curl -s -m 1 http://127.0.0.1:8000/state >/dev/null && break; sleep 0.5; done
curl -s -m 2 http://127.0.0.1:8000/state >/dev/null || { echo "backend did not start, see logs/backend.out"; exit 1; }
echo "backend ready at http://127.0.0.1:8000 (log: logs/backend.out)"

( cd frontend && { { [ -d node_modules ] && [ ! package-lock.json -nt node_modules/.package-lock.json ]; } || npm install --no-audit --no-fund; } && exec npx vite --host "$VITE_HOST" --port 5173 ) &
pids+=($!)
echo "frontend: open $SCHEME://127.0.0.1:5173 (Chrome / Edge, allow the microphone)"
if [ "$LAN" = 1 ]; then
  for ip in $(hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null); do echo "phone (same Wi-Fi): $SCHEME://$ip:5173"; done
  [ "$HTTPS" = 1 ] || echo "note: phones only allow the microphone on https:// pages; add --https for voice on a phone (text chat works either way)"
fi
wait
