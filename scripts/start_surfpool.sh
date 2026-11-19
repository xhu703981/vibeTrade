#!/usr/bin/env bash
# Start two Surfpool mainnet forks: main 8899 (trading + guard profiling) and scratch 8999 (buy-then-sell tests, resettable).
# --slot-time 400 matches the mainnet slot pace and limits fork clock drift (the guard still aligns with surfnet_timeTravel).
# The datasource RPC is passed via SURFPOOL_DATASOURCE_RPC_URL so it never shows up on the command line / in ps.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${SOLANA_RPC_URL:?SOLANA_RPC_URL is not set}"
# if only a Helius API key (UUID) is given, build the URL
if [[ "$SOLANA_RPC_URL" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
  SOLANA_RPC_URL="https://mainnet.helius-rpc.com/?api-key=$SOLANA_RPC_URL"
fi
# Memory: every listener poll profiles a sell transaction (surfnet_profileTransaction). With Surfpool's defaults
# (instruction profiling on, 200 profiles kept) the main fork grew ~6-7 MB/s while a position was held and reached
# ~15 GB after a long session. We only read transaction-level before/after account states, so per-instruction
# profiling is off and few profiles are kept. Override with SURFPOOL_MEM_FLAGS="" to get the old behaviour.
SURFPOOL_MEM_FLAGS="${SURFPOOL_MEM_FLAGS---disable-instruction-profiling --max-profiles 16}"
SURFPOOL_BIN="${SURFPOOL_BIN:-$(command -v surfpool || echo "$HOME/.local/bin/surfpool")}"
mkdir -p logs

start_one() {  # name port ws_port
  local name=$1 port=$2 ws=$3
  if curl -s -m 2 -X POST -H 'content-type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' "http://127.0.0.1:$port" | grep -q result; then
    echo "[$name] already running on :$port"; return
  fi
  mkdir -p ".surfpool/$name"
  ( cd ".surfpool/$name" || exit 1
    SURFPOOL_DATASOURCE_RPC_URL="$SOLANA_RPC_URL" nohup "$SURFPOOL_BIN" start \
      --port "$port" --ws-port "$ws" --no-tui --no-studio --no-deploy --yes \
      --airdrop-amount 0 --slot-time 400 --surfnet-id "$name" --log-path ../../logs/"$name" $SURFPOOL_MEM_FLAGS \
      > "../../logs/$name.out" 2>&1 &
    echo $! > pid )
  for _ in $(seq 1 60); do
    if curl -s -m 2 -X POST -H 'content-type: application/json' \
         -d '{"jsonrpc":"2.0","id":1,"method":"getHealth"}' "http://127.0.0.1:$port" | grep -q result; then
      echo "[$name] ready at http://127.0.0.1:$port (pid $(cat .surfpool/$name/pid))"; return
    fi
    sleep 1
  done
  echo "[$name] startup timed out, see logs/$name.out" >&2; exit 1
}

start_one main 8899 8900
start_one scratch 8999 9000
