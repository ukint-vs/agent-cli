#!/bin/bash
# Analyze 5m strategy trading logs.
# Usage: ./scripts/analyze_5m.sh

LOG_DIR="$(cd "$(dirname "$0")/.." && pwd)/logs"
COINS="sol sui link"

echo "=== 5m Strategy Trading Analysis ==="
echo ""

# Per-coin summary
for coin in $COINS; do
    LOG="$LOG_DIR/5m_${coin}.log"
    [ ! -f "$LOG" ] && continue

    FILLS=$(grep "Filled" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    WINS=$(grep "WIN" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    LOSSES=$(grep "LOSS" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    RPNL=$(grep "rPnL=" "$LOG" 2>/dev/null | tail -1 | sed 's/.*rPnL=//;s/ .*//' | sed 's/\x1b\[[0-9;]*m//g')
    POS=$(grep "pos=" "$LOG" 2>/dev/null | tail -1 | sed 's/.*pos=//;s/ .*//' | sed 's/\x1b\[[0-9;]*m//g')

    COIN_UPPER=$(echo $coin | tr 'a-z' 'A-Z')
    echo "  $COIN_UPPER: $FILLS fills | W:$WINS L:$LOSSES | rPnL=$RPNL | pos=$POS"
done

echo ""
echo "--- Recent fills (last 20) ---"
for coin in $COINS; do
    LOG="$LOG_DIR/5m_${coin}.log"
    [ ! -f "$LOG" ] && continue
    COIN_UPPER=$(echo $coin | tr 'a-z' 'A-Z')
    grep "Filled" "$LOG" 2>/dev/null | tail -5 | while read line; do
        TS=$(echo "$line" | grep -o '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]' | head -1)
        DETAIL=$(echo "$line" | sed 's/.*Filled //')
        echo "  $TS $COIN_UPPER $DETAIL"
    done
done

echo ""
echo "--- Wins/Losses ---"
TOTAL_W=0
TOTAL_L=0
for coin in $COINS; do
    LOG="$LOG_DIR/5m_${coin}.log"
    [ ! -f "$LOG" ] && continue
    W=$(grep "WIN" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    L=$(grep "LOSS" "$LOG" 2>/dev/null | wc -l | tr -d ' ')
    TOTAL_W=$((TOTAL_W + W))
    TOTAL_L=$((TOTAL_L + L))
done
TOTAL=$((TOTAL_W + TOTAL_L))
if [ "$TOTAL" -gt 0 ]; then
    WR=$(echo "scale=1; $TOTAL_W * 100 / $TOTAL" | bc)
    echo "  Total: $TOTAL trades, $TOTAL_W wins, $TOTAL_L losses, WR: ${WR}%"
else
    echo "  No completed round trips yet"
fi

echo ""
echo "--- Risk status ---"
for coin in $COINS; do
    LOG="$LOG_DIR/5m_${coin}.log"
    [ ! -f "$LOG" ] && continue
    COIN_UPPER=$(echo $coin | tr 'a-z' 'A-Z')
    RISK=$(grep "Risk:" "$LOG" 2>/dev/null | tail -1 | sed 's/.*Risk: //' | sed 's/\x1b\[[0-9;]*m//g')
    [ -n "$RISK" ] && echo "  $COIN_UPPER: $RISK"
done

echo ""
echo "--- Errors (if any) ---"
ERRS=0
for coin in $COINS; do
    LOG="$LOG_DIR/5m_${coin}.log"
    [ ! -f "$LOG" ] && continue
    grep -i "error\|fatal\|traceback\|exception" "$LOG" 2>/dev/null | grep -iv "yex\|no funds" | tail -5
    ERRS=$((ERRS + $(grep -ic "error\|fatal\|traceback\|exception" "$LOG" 2>/dev/null | grep -iv "yex" | head -1 || echo 0)))
done
[ "$ERRS" -eq 0 ] && echo "  None"
