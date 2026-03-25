#!/bin/bash
# Launch autoresearch live adapter on Hyperliquid mainnet.
# $300 USDC, 3x leverage, 4 coins (ETH, XRP, DOGE, SOL).
# Wraps auto-researchtrading/strategy.py — always runs latest champion.
#
# Usage: nohup ./scripts/run_autoresearch_live.sh > live_trading.log 2>&1 &
# Check: tail -f live_trading.log
# Stop:  kill $(cat /tmp/autoresearch_live.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_live"

cd "$BASE_DIR"

# Resolve key from keychain (address: 0x72C6A2682DEB6960D8544ebe77B5508C920aBbFE)
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli-live" -a "0x72C6A2682DEB6960D8544ebe77B5508C920aBbFE" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
    else
        echo "ERROR: No HL_PRIVATE_KEY found. Import with:"
        echo "  security add-generic-password -s agent-cli-live -a 0x72C6A2682DEB6960D8544ebe77B5508C920aBbFE -w YOUR_KEY"
        exit 1
    fi
fi
export HL_TESTNET=false

COINS="eth xrp doge sol"
PIDS=()

echo "$(date): Starting autoresearch LIVE — 4 coins, mainnet, real orders"
echo "Coins: $COINS"
echo ""

for coin in $COINS; do
    config="$CONFIG_DIR/autoresearch_${coin}.yaml"
    if [ ! -f "$config" ]; then
        echo "  ERROR: config not found: $config"
        continue
    fi
    echo "  $(date): Starting $coin..."
    uv run hl run autoresearch --config "$config" --mainnet &
    PIDS+=($!)
    sleep 3
done

# Save PIDs for easy cleanup
echo "${PIDS[*]}" > /tmp/autoresearch_live.pids

echo ""
echo "$(date): All ${#PIDS[@]} coins running on MAINNET."
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_live.pids)"
echo "Stop with: kill \$(cat /tmp/autoresearch_live.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_live.pids; exit 0' INT TERM
wait
