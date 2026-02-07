# Paper Trading Bot

## Overview
Real-time paper trading using **real market data** with **fake money** ($1000).

Tracks **TWO parallel accounts**:
- **24/7 Account** - Trades all day, every day
- **Scheduled Account** - Only trades during configured hours (Mon-Fri, 2-4am/2-4pm UTC)

## ⚠️ Important
- ✅ Uses REAL prices from Binance
- ✅ Uses REAL liquidations from Coinalyze  
- ✅ Uses your REAL .env API keys for data
- ❌ **NO REAL ORDERS ARE PLACED**

## Usage

```bash
python paper_trading.py
```

## What You'll See

1. **Startup Banner** - Confirms paper trading mode
2. **Live Dashboard** (updates every minute):
   - Current BTC price
   - Both account balances and P&L
   - Trade count, win rate
   - Max drawdown
3. **Trade Notifications** - Real-time alerts when trades execute
4. **Final Results** - Press Ctrl+C to stop and see comparison

## Example Output

```
🤖 PAPER TRADING DASHBOARD - 2026-02-07 15:45:00
══════════════════════════════════════════════════

Current BTC Price: $96,543.21

🌍 PAPER ACCOUNT (24/7)
  Balance:     $1,045.32
  P&L:         +$45.32 (+4.53%)
  Trades:      12 (W:7 / L:5)
  Win Rate:    +58.33%
  Max DD:      -$23.41

📅 PAPER ACCOUNT (SCHEDULED)
  Balance:     $1,018.50
  P&L:         +$18.50 (+1.85%)
  Trades:      4 (W:3 / L:1)
  Win Rate:    +75.00%
  Max DD:      -$8.22

📊 COMPARISON:
  Leader: 24/7 Trading (+$26.82)

Press Ctrl+C to stop and see final results
```

## Files Created

- `paper_trading.py` - Main runner
- `pExchange.py` - Paper exchange (no real orders)
- `paper_logger.py` - Live dashboard

## Requirements

Same as the real bot - make sure your `.env` has:
- `COINALYZE_SECRET_API_KEY`
- Exchange credentials (for price data only)

## Stopping

Press `Ctrl+C` to stop and see the final comparison between 24/7 and Scheduled trading!
