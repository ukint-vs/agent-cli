#!/bin/bash
# Transfer USDC from Spot → Perps on mainnet.
# Usage: ./scripts/transfer_spot_to_perps.sh [amount]
# Default: transfers all available spot USDC.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
cd "$BASE_DIR"

AMOUNT="${1:-all}"

# Resolve key
if [ -z "$HL_PRIVATE_KEY" ]; then
    KEY=$(security find-generic-password -s "agent-cli" -a "0x67117f4fb25a0346039afde63b8b796a93c098c8" -w 2>/dev/null || true)
    if [ -n "$KEY" ]; then
        export HL_PRIVATE_KEY="$KEY"
    else
        echo "ERROR: No HL_PRIVATE_KEY found."
        exit 1
    fi
fi

uv run python3 -c "
import os, sys
sys.path.insert(0, '.')

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

pk = os.environ['HL_PRIVATE_KEY']
wallet = Account.from_key(pk)
info = Info(constants.MAINNET_API_URL, skip_ws=True)
exchange = Exchange(wallet, constants.MAINNET_API_URL)

# Get spot balance
state = info.user_state(wallet.address)
spot_balances = info.spot_user_state(wallet.address).get('balances', [])
usdc_spot = 0.0
for b in spot_balances:
    if b.get('coin') == 'USDC':
        usdc_spot = float(b.get('total', 0))
        break

perp_equity = float(state.get('marginSummary', {}).get('accountValue', 0))

print(f'Address:    {wallet.address}')
print(f'Spot USDC:  \${usdc_spot:.2f}')
print(f'Perp value: \${perp_equity:.2f}')

amount = '${AMOUNT}'
if amount == 'all':
    transfer_amount = usdc_spot
else:
    transfer_amount = float(amount)

if transfer_amount <= 0:
    print('Nothing to transfer.')
    sys.exit(0)

if transfer_amount > usdc_spot:
    print(f'ERROR: requested \${transfer_amount:.2f} but only \${usdc_spot:.2f} available')
    sys.exit(1)

print(f'Transferring \${transfer_amount:.2f} Spot → Perps...')
result = exchange.usd_class_transfer(transfer_amount, to_perp=True)
status = result.get('status', 'unknown')
if status == 'ok':
    print(f'OK: \${transfer_amount:.2f} transferred to Perps')
else:
    print(f'Transfer result: {result}')
"
