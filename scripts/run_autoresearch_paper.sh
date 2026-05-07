#!/bin/bash
# Paper trade autoresearch across 4 coins on TESTNET.
# Uses testnet prices, simulated fills. No real orders.
#
# Usage: nohup ./scripts/run_autoresearch_paper.sh > paper_trading.log 2>&1 &
# Check: tail -f paper_trading.log
# Stop:  kill $(cat /tmp/autoresearch_paper.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_live"

cd "$BASE_DIR"

# Resolve key from keychain
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli" -a "0x67117f4fb25a0346039afde63b8b796a93c098c8" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
    else
        echo "ERROR: No HL_PRIVATE_KEY found."
        exit 1
    fi
fi

# Paper mode uses mainnet data but simulated fills — safe to run against mainnet
export HL_TESTNET=false

COINS="ETH XRP DOGE SOL"
PIDS=()

echo "$(date): Starting paper trading — 4 coins, MAINNET data, simulated fills"
echo "Coins: $COINS"
echo ""

for coin in $COINS; do
    coin_lower=$(echo "$coin" | tr '[:upper:]' '[:lower:]')
    config="$CONFIG_DIR/autoresearch_${coin_lower}.yaml"
    if [ ! -f "$config" ]; then
        echo "  ERROR: config not found: $config"
        continue
    fi
    echo "  $(date): Starting $coin (paper, mainnet data)..."
    uv run hl run autoresearch -i "${coin}-PERP" --config "$config" --paper &
    PIDS+=($!)
    sleep 3
done

echo "${PIDS[*]}" > /tmp/autoresearch_paper.pids

echo ""
echo "$(date): All 4 coins running in paper mode (testnet)."
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_paper.pids)"
echo "Stop with: kill \$(cat /tmp/autoresearch_paper.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_paper.pids; exit 0' INT TERM
wait
