#!/bin/bash
# Paper trading for autoresearch $2K strategy.
# Real market data, simulated execution. Validate before going live.
#
# Usage: nohup ./scripts/run_autoresearch_2k_paper.sh > 2k_paper.log 2>&1 &
# Check: tail -f 2k_paper.log
# Stop:  kill $(cat /tmp/autoresearch_2k_paper.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_2k"

cd "$BASE_DIR"

export AUTORESEARCH_PATH="$HOME/autoagent-hl"

# Paper mode still needs a key for market data
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli-live" -a "0x72C6A2682DEB6960D8544ebe77B5508C920aBbFE" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
    else
        echo "ERROR: No HL_PRIVATE_KEY found."
        exit 1
    fi
fi
export HL_TESTNET=false

COINS="btc eth sol xrp doge"
PIDS=()

echo "$(date): Starting autoresearch $2K PAPER — 5 coins, real data, simulated fills"
echo "Strategy: $AUTORESEARCH_PATH/strategy.py"
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

echo "${PIDS[*]}" > /tmp/autoresearch_2k_paper.pids

echo ""
echo "$(date): Paper trading active. PIDs: ${PIDS[*]}"
echo "Stop with: kill \$(cat /tmp/autoresearch_2k_paper.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_2k_paper.pids; exit 0' INT TERM
wait
