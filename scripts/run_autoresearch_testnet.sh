#!/bin/bash
# Run autoresearch on testnet — real orders against testnet.
# $600 USDyP, 4 coins (ETH, XRP, DOGE, SOL).
#
# Usage: nohup ./scripts/run_autoresearch_testnet.sh > testnet_trading.log 2>&1 &
# Check: tail -f testnet_trading.log
# Stop:  kill $(cat /tmp/autoresearch_testnet.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_testnet"

cd "$BASE_DIR"

# Resolve key from keychain
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli" -a "0x67117f4fb25a0346039afde63b8b796a93c098c8" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
        export HL_TESTNET=true
    else
        echo "ERROR: No HL_PRIVATE_KEY found. Set it or import to keychain."
        exit 1
    fi
fi

COINS="eth btc doge sol"  # XRP not available on testnet
PIDS=()

echo "$(date): Starting autoresearch on TESTNET — 4 coins, real orders"
echo "Coins: $COINS"
echo ""

for coin in $COINS; do
    config="$CONFIG_DIR/autoresearch_${coin}.yaml"
    if [ ! -f "$config" ]; then
        echo "  ERROR: config not found: $config"
        continue
    fi
    echo "  $(date): Starting $coin..."
    uv run hl run autoresearch --config "$config" &
    PIDS+=($!)
    sleep 3
done

# Save PIDs for easy cleanup
echo "${PIDS[*]}" > /tmp/autoresearch_testnet.pids

echo ""
echo "$(date): All 4 coins running on testnet."
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_testnet.pids)"
echo "Stop with: kill \$(cat /tmp/autoresearch_testnet.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_testnet.pids; exit 0' INT TERM
wait
