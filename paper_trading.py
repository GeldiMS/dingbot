"""
Paper Trading Bot - Real market data, fake money
Run: python paper_trading.py

Connects to real Coinalyze and Binance APIs.
Tracks TWO parallel paper accounts ($1000 each):
  - 24/7 Account: Trades all day every day
  - Scheduled Account: Only trades during configured hours

NO REAL ORDERS ARE PLACED - This is paper trading only!
"""
import asyncio
import signal
import sys
from copy import deepcopy
from datetime import datetime
from typing import List

from logger import logger
from misc import Liquidation, LiquidationSet
from coinalyze_scanner import CoinalyzeScanner, COINALYZE_LIQUIDATION_URL, LIQUIDATION_DAYS, LIQUIDATION_HOURS
from pExchange import PaperExchange
from paper_logger import PaperLogger, PaperAccount

# Starting balance for paper trading
STARTING_BALANCE = 1000.0

# Global for signal handling
running = True
start_time = None


def signal_handler(sig, frame):
    """Handle Ctrl+C"""
    global running
    running = False
    print("\n\n🛑 Stopping paper trading...")


async def main():
    global running, start_time
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    start_time = datetime.now()
    paper_logger = PaperLogger()
    
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  🤖 PAPER TRADING BOT                                            ║
║  Starting with ${STARTING_BALANCE:,.2f} per account                            ║
║                                                                  ║
║  Two accounts tracking:                                          ║
║    • 24/7 Account - Trades all day every day                     ║
║    • Scheduled Account - Trades Mon-Fri, configured hours only   ║
║                                                                  ║
║  ⚠️  NO REAL ORDERS WILL BE PLACED                               ║
║  Press Ctrl+C to stop and see final results                      ║
╚══════════════════════════════════════════════════════════════════╝
    """)
    
    await asyncio.sleep(2)
    
    # === Setup 24/7 Account ===
    LIQUIDATIONS_247: List[Liquidation] = []
    LIQUIDATION_SET_247 = LiquidationSet(liquidations=LIQUIDATIONS_247)
    
    scanner_247 = CoinalyzeScanner(datetime.now(), LIQUIDATION_SET_247)
    await scanner_247.set_symbols()
    
    exchange_247 = PaperExchange(
        LIQUIDATION_SET_247,
        scanner_247,
        starting_balance=STARTING_BALANCE,
        mode="24/7",
        paper_logger=paper_logger
    )
    scanner_247.exchange = exchange_247
    
    # === Setup Scheduled Account ===
    LIQUIDATIONS_SCHED: List[Liquidation] = []
    LIQUIDATION_SET_SCHED = LiquidationSet(liquidations=LIQUIDATIONS_SCHED)
    
    scanner_sched = CoinalyzeScanner(datetime.now(), LIQUIDATION_SET_SCHED)
    await scanner_sched.set_symbols()
    
    exchange_sched = PaperExchange(
        LIQUIDATION_SET_SCHED,
        scanner_sched,
        starting_balance=STARTING_BALANCE,
        mode="Scheduled",
        paper_logger=paper_logger
    )
    scanner_sched.exchange = exchange_sched
    
    await exchange_247.set_leverage(symbol="BTC/USDT:USDT", leverage=25, direction="long")
    await exchange_247.set_leverage(symbol="BTC/USDT:USDT", leverage=25, direction="short")
    await exchange_sched.set_leverage(symbol="BTC/USDT:USDT", leverage=25, direction="long")
    await exchange_sched.set_leverage(symbol="BTC/USDT:USDT", leverage=25, direction="short")
    
    logger.info("Paper trading started - using REAL market data, FAKE money")
    logger.info(f"24/7 Account: ${STARTING_BALANCE:,.2f}")
    logger.info(f"Scheduled Account: ${STARTING_BALANCE:,.2f}")
    logger.info(f"BTC markets being scanned: {scanner_247.symbols}")
    
    first_run = True
    last_dashboard_update = datetime.now()
    
    while running:
        now = datetime.now()
        
        # Main loop - every 5 minutes (matching real bot)
        if (now.minute % 5 == 0 and now.second == 0) or first_run:
            if first_run:
                first_run = False
            
            # Update scanner times
            scanner_247.now = now
            scanner_sched.now = now
            
            # Get real candle from Binance
            last_candle = await exchange_247.get_last_candle(now)
            
            if last_candle:
                btc_price = last_candle.close
                
                # Fetch real liquidations from Coinalyze
                liquidation_data = await scanner_247.handle_coinalyze_url(COINALYZE_LIQUIDATION_URL)
                
                # === 24/7 Account: Always trade ===
                await exchange_247.run_loop(last_candle)
                
                # Handle liquidations for 24/7 (always)
                # The scanner already handles the filtering internally
                await scanner_247.handle_liquidation_set(last_candle, liquidation_data)
                
                # Log liquidation analysis
                for liq_data in liquidation_data:
                    symbol = liq_data.get("symbol", "unknown")
                    for hist in liq_data.get("history", []):
                        long_btc = hist.get("l", 0)
                        short_btc = hist.get("s", 0)
                        # Convert BTC to USD
                        long_usd = long_btc * btc_price
                        short_usd = short_btc * btc_price
                        if long_btc > 0 or short_btc > 0:
                            if long_usd >= 2000 or short_usd >= 2000:
                                logger.info(f"✅ TRADE SIGNAL: {symbol} | Long: {long_btc:.3f} BTC (${long_usd:,.0f}) | Short: {short_btc:.3f} BTC (${short_usd:,.0f})")
                            else:
                                logger.info(f"⏭️ SKIPPED (too small): {symbol} | Long: ${long_usd:,.0f} | Short: ${short_usd:,.0f} (need >$2000)")
                
                # === Scheduled Account: Only trade during configured hours ===
                is_trading_day = now.weekday() in LIQUIDATION_DAYS
                is_trading_hour = now.hour in LIQUIDATION_HOURS
                
                if is_trading_day and is_trading_hour:
                    await exchange_sched.run_loop(last_candle)
                    await scanner_sched.handle_liquidation_set(last_candle, liquidation_data)
                
                # Update dashboard every minute-ish
                if (now - last_dashboard_update).seconds >= 60:
                    paper_logger.print_dashboard(
                        exchange_247.get_account_state(),
                        exchange_sched.get_account_state(),
                        btc_price
                    )
                    last_dashboard_update = now
            
            await asyncio.sleep(0.99)
        
        # Position sizes update
        if now.minute % 5 == 4 and now.second == 0:
            await exchange_247.set_position_sizes()
            await exchange_sched.set_position_sizes()
            await asyncio.sleep(0.99)
        
        await asyncio.sleep(0.1)
    
    # === Final Results ===
    runtime_hours = (datetime.now() - start_time).total_seconds() / 3600
    paper_logger.print_final_results(
        exchange_247.get_account_state(),
        exchange_sched.get_account_state(),
        runtime_hours
    )
    
    # === Save results to files ===
    save_results_to_file(exchange_247, "paper_results_24_7.txt", runtime_hours)
    save_results_to_file(exchange_sched, "paper_results_scheduled.txt", runtime_hours)
    print(f"\n📁 Results saved to:")
    print(f"   • paper_results_24_7.txt")
    print(f"   • paper_results_scheduled.txt")


def save_results_to_file(exchange: PaperExchange, filename: str, runtime_hours: float):
    """Save account results and trade history to file"""
    account = exchange.get_account_state()
    
    with open(filename, 'w') as f:
        f.write(f"{'='*60}\n")
        f.write(f"PAPER TRADING RESULTS - {account.mode.upper()}\n")
        f.write(f"{'='*60}\n\n")
        
        f.write(f"Runtime: {runtime_hours:.2f} hours\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        f.write(f"ACCOUNT SUMMARY\n")
        f.write(f"-"*40 + "\n")
        f.write(f"Starting Balance:  ${account.starting_balance:,.2f}\n")
        f.write(f"Ending Balance:    ${account.balance:,.2f}\n")
        f.write(f"Total P&L:         ${account.pnl:+,.2f} ({account.pnl_pct:+.2f}%)\n")
        f.write(f"Total Trades:      {account.trades}\n")
        f.write(f"Winning Trades:    {account.wins}\n")
        f.write(f"Losing Trades:     {account.losses}\n")
        f.write(f"Win Rate:          {account.win_rate:.2f}%\n")
        f.write(f"Max Drawdown:      ${account.max_drawdown:,.2f}\n\n")
        
        f.write(f"TRADE HISTORY\n")
        f.write(f"-"*40 + "\n")
        
        if exchange.trade_history:
            f.write(f"{'#':<4} {'Time':<20} {'Dir':<6} {'Entry':>12} {'Exit':>12} {'P&L':>12} {'Reason':<6}\n")
            f.write("-"*80 + "\n")
            
            for i, trade in enumerate(exchange.trade_history, 1):
                trade_time = datetime.fromtimestamp(trade['timestamp']/1000).strftime('%Y-%m-%d %H:%M')
                f.write(f"{i:<4} {trade_time:<20} {trade['direction']:<6} "
                       f"${trade['entry_price']:>10,.2f} ${trade['exit_price']:>10,.2f} "
                       f"${trade['pnl']:>+10,.2f} {trade['reason']:<6}\n")
        else:
            f.write("No trades executed.\n")
        
        f.write(f"\n{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
