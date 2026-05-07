#!/bin/bash
# Launch autoresearch $2K strategy on Hyperliquid mainnet.
# $1,000 USDC, 5x leverage, 5 coins (BTC, ETH, SOL, XRP, DOGE).
# Wraps autoagent-hl/strategy.py (Sonnet-tuned champion).
#
# Backtest: Sharpe 19.9, 92.5% WR, 1.4% DD. Mar'26 test: +35% in 35 days.
#
# Usage: nohup ./scripts/run_autoresearch_2k.sh > 2k_trading.log 2>&1 &
# Check: tail -f 2k_trading.log
# Stop:  kill $(cat /tmp/autoresearch_2k.pids)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="$BASE_DIR/configs/autoresearch_2k"

cd "$BASE_DIR"

# Point adapter at autoagent-hl strategy (Sonnet-tuned for $2K)
export AUTORESEARCH_PATH="$HOME/autoagent-hl"
export BUILDER_FEE_TENTHS_BPS=0

# Resolve key from keychain
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli-live" -a "0x72C6A2682DEB6960D8544ebe77B5508C920aBbFE" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
    else
        echo "ERROR: No HL_PRIVATE_KEY found. Set HL_PRIVATE_KEY or import to keychain:"
        echo "  security add-generic-password -s agent-cli-live -a <YOUR_ADDRESS> -w <YOUR_KEY>"
        exit 1
    fi
fi
export HL_TESTNET=false

COINS="btc eth sol xrp doge"
PIDS=()

echo "$(date): Starting autoresearch $2K — 5 coins, mainnet, 5x leverage"
echo "Strategy: $AUTORESEARCH_PATH/strategy.py"
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
echo "${PIDS[*]}" > /tmp/autoresearch_2k.pids

echo ""
echo "$(date): All ${#PIDS[@]} coins running on MAINNET."
echo "PIDs: ${PIDS[*]} (saved to /tmp/autoresearch_2k.pids)"
echo "Stop with: kill \$(cat /tmp/autoresearch_2k.pids)"
echo ""

trap 'echo "$(date): Stopping..."; kill "${PIDS[@]}" 2>/dev/null; rm -f /tmp/autoresearch_2k.pids; exit 0' INT TERM
wait
