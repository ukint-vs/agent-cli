#!/bin/bash
# Paper trade autoresearch S4 across 4 coins overnight.
# Real mainnet market data, simulated execution. No real orders placed.
#
# Usage: nohup ./scripts/run_autoresearch_paper.sh > paper_trading.log 2>&1 &
# Check: tail -f paper_trading.log
# Stop:  kill $(cat /tmp/autoresearch_paper.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_live"

cd "$BASE_DIR"

# Resolve key from keychain into env (workaround for list_keys bug)
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli" -a "0x67117f4fb25a0346039afde63b8b796a93c098c8" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
        echo "Loaded key from macOS Keychain"
        export HL_TESTNET=false
    else
        echo "ERROR: No HL_PRIVATE_KEY found. Set it or import to keychain."
        exit 1
    fi
fi

COINS="eth xrp doge sol"
PIDS=()

echo "$(date): Starting paper trading — 4 coins, mainnet data, simulated fills"
echo "Coins: $COINS"
echo ""

for coin in $COINS; do
    config="$CONFIG_DIR/autoresearch_${coin}.yaml"
    if [ ! -f "$config" ]; then
        echo "  ERROR: config not found: $config"
        continue
    fi
    echo "  $(date): Starting $coin (paper)..."
    uv run hl run autoresearch --config "$config" --mainnet --paper &
    PIDS+=($!)
    sleep 3
done

# Save PIDs for easy cleanup
echo "${PIDS[*]}" > /tmp/autoresearch_paper.pids

echo ""
echo "$(date): All 4 coins running in paper mode."
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_paper.pids)"
echo "Stop with: kill \$(cat /tmp/autoresearch_paper.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_paper.pids; exit 0' INT TERM
wait
