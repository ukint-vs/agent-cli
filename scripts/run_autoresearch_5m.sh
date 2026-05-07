#!/bin/bash
# Launch NATIVE 5m strategy on Hyperliquid mainnet.
# $1,000 USDC, 10x leverage, 5 coins (BTC, ETH, SOL, XRP, DOGE).
#
# Logs: logs/5m_<coin>.log per coin + logs/5m_combined.log for all
# Analyze: cat logs/5m_*.log | grep Filled
#
# Usage: ./scripts/run_autoresearch_5m.sh
# Check: tail -f logs/5m_combined.log
# Stop:  kill $(cat /tmp/autoresearch_5m.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_5m"
LOG_DIR="$BASE_DIR/logs"

cd "$BASE_DIR"
mkdir -p "$LOG_DIR"

export AUTORESEARCH_PATH="$HOME/autoagent-hl"
export BUILDER_FEE_TENTHS_BPS=0

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

COINS="sol sui link"
PIDS=()
COMBINED="$LOG_DIR/5m_combined.log"

echo "$(date): Starting NATIVE 5m strategy — 5 coins, mainnet, 10x leverage" | tee "$COMBINED"
echo "Strategy: $AUTORESEARCH_PATH/strategy_5m.py" | tee -a "$COMBINED"
echo "Logs: $LOG_DIR/5m_<coin>.log" | tee -a "$COMBINED"
echo "" | tee -a "$COMBINED"

for coin in $COINS; do
    config="$CONFIG_DIR/autoresearch_${coin}.yaml"
    if [ ! -f "$config" ]; then
        echo "  ERROR: config not found: $config" | tee -a "$COMBINED"
        continue
    fi
    COIN_LOG="$LOG_DIR/5m_${coin}.log"
    echo "  $(date): Starting $coin → $COIN_LOG" | tee -a "$COMBINED"
    uv run hl run autoresearch_5m --config "$config" --mainnet > >(tee -a "$COIN_LOG" "$COMBINED") 2>&1 &
    PIDS+=($!)
    sleep 3
done

echo "${PIDS[*]}" > /tmp/autoresearch_5m.pids

echo "" | tee -a "$COMBINED"
echo "$(date): All ${#PIDS[@]} coins running on MAINNET (native 5m, 10x)." | tee -a "$COMBINED"
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_5m.pids)" | tee -a "$COMBINED"
echo "Stop with: kill \$(cat /tmp/autoresearch_5m.pids)" | tee -a "$COMBINED"
echo "" | tee -a "$COMBINED"

trap 'echo "$(date): Stopping..." | tee -a "$COMBINED"; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_5m.pids; exit 0' INT TERM
wait
