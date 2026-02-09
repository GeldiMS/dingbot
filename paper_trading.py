"""
Paper Trading Bot - Simulates trading using real market data with fake money.
Reuses original CoinalyzeScanner and mirrors Exchange logic.
"""

from asyncio import run, sleep
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
import signal
import sys
import os

# Import original bot components
from coinalyze_scanner import (
    CoinalyzeScanner, 
    COINALYZE_LIQUIDATION_URL,
    MINIMAL_LIQUIDATION,
    MINIMAL_NR_OF_LIQUIDATIONS,
    LIQUIDATION_DAYS,
    LIQUIDATION_HOURS,
    N_MINUTES_TIMEDELTA,
    INTERVAL,
)
from exchange import (
    TICKER,
    LEVERAGE,
    EXCHANGE_PRICE_PRECISION,
    BINANCE_EXCHANGE,
    LONG,
    SHORT,
    FORBIDDEN_NR_OF_CANDLES_BEFORE_ENTRY,
)
from logger import logger
from misc import Candle, Liquidation, LiquidationSet, PositionToOpen
import pandas as pd


# ================================================================
# Configuration
# ================================================================
STARTING_BALANCE = 1000.0


# ================================================================
# Data Classes for Paper Trading
# ================================================================
@dataclass
class PaperPosition:
    """Represents an open paper position"""
    order_id: int
    direction: str  # "long" or "short"
    entry_price: float
    size: float  # in USD
    stop_loss: float
    take_profit: float
    timestamp: datetime
    liquidation_id: str


@dataclass
class PaperOrder:
    """Represents a pending paper order (waiting for price to hit entry)"""
    order_id: int
    direction: str
    entry_price: float
    size: float
    stop_loss_pct: float
    take_profit_pct: float
    liquidation_id: str
    created_at: datetime


# ================================================================
# Paper Exchange - Simulates trading
# ================================================================
class PaperExchange:
    """Paper trading exchange that mirrors original Exchange behavior"""
    
    def __init__(
        self,
        liquidation_set: LiquidationSet,
        scanner: CoinalyzeScanner,
        starting_balance: float,
        mode: str,  # "24/7" or "Scheduled"
    ) -> None:
        self.liquidation_set = liquidation_set
        self.scanner = scanner
        self.mode = mode
        
        # Account state
        self.balance = starting_balance
        self.starting_balance = starting_balance
        
        # Order tracking
        self.order_id_counter = 0
        self.positions: List[PaperPosition] = []
        self.positions_to_open: List[PositionToOpen] = []
        
        # Statistics
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        
        # Position sizing (matches original bot)
        self._position_size = 0
        
        logger.info(f"[{mode}] Paper exchange initialized with ${starting_balance:,.2f}")
    
    async def set_leverage(self, symbol: str, leverage: int, direction: str) -> None:
        """Simulate setting leverage"""
        logger.info(f"[{self.mode}] Set leverage to {leverage}x for {direction}")
    
    async def get_last_candle(self, now: datetime) -> Optional[Candle]:
        """Get the last candle from Binance (real data)"""
        try:
            now_minus_5m = now.replace(
                minute=now.minute - now.minute % 5, second=0, microsecond=0
            ) - timedelta(minutes=5)
            last_candles = await BINANCE_EXCHANGE.fetch_ohlcv(
                symbol=TICKER,
                timeframe="5m",
                since=int(now_minus_5m.timestamp() * 1000),
                limit=2,
            )
            candle = Candle(*last_candles[0])
            return candle
        except Exception as e:
            logger.error(f"[{self.mode}] Error fetching candle: {e}")
            return None
    
    async def get_price(self) -> Optional[float]:
        """Get current price from Binance"""
        try:
            ticker = await BINANCE_EXCHANGE.fetch_ticker(symbol=TICKER)
            return ticker["last"]
        except Exception as e:
            logger.error(f"[{self.mode}] Error fetching price: {e}")
            return None
    
    async def set_position_sizes(self) -> None:
        """Calculate position size based on balance"""
        try:
            price = await self.get_price()
            if price:
                # Position size in contracts (matches original bot logic)
                position_size = int(self.balance * LEVERAGE / price)
                if position_size != self._position_size:
                    self._position_size = position_size
        except Exception as e:
            logger.error(f"[{self.mode}] Error setting position size: {e}")
    
    @property
    def position_size(self) -> int:
        return self._position_size
    
    def get_algorithm_input_file(self, strategy_type: str, input_date) -> pd.DataFrame:
        """Get algorithm input file (matches original bot)"""
        try:
            file_names = [
                name
                for name in os.listdir("algorithm_input")
                if name.endswith(".csv") and strategy_type in name
            ]
            if not file_names:
                return pd.DataFrame()
            file_names.sort()
            return pd.read_csv(f"algorithm_input/{file_names[-1]}")
        except Exception as e:
            logger.error(f"[{self.mode}] Error reading algorithm file: {e}")
            return pd.DataFrame()
    
    async def reaction_to_liquidation_is_strong(
        self, liquidation: Liquidation, price: float
    ) -> bool:
        """Check if price reaction is strong (matches original)"""
        if (liquidation.direction == LONG and price > liquidation.candle.high) or (
            liquidation.direction == SHORT and price < liquidation.candle.low
        ):
            return True
        return False
    
    async def handle_liquidation(
        self, liquidation: Liquidation, last_candle: Candle
    ) -> None:
        """Handle a single liquidation (mirrors original Exchange.handle_liquidation)"""
        
        liquidation_datetime = datetime.fromtimestamp(liquidation.candle.timestamp / 1000)
        
        # Remove if older than 15 minutes
        if liquidation_datetime < (
            self.scanner.now.replace(second=0, microsecond=0) - timedelta(minutes=15)
        ):
            self.liquidation_set.liquidations.remove(liquidation)
            return
        
        # Check if reaction is strong enough
        if not await self.reaction_to_liquidation_is_strong(liquidation, last_candle.close):
            return
        
        # Reaction is strong - prepare trade
        now = self.scanner.now.replace(second=0, microsecond=0)
        candles_before_confirmation = (
            int(round((now - liquidation_datetime).total_seconds() / 300, 0)) - 1
        )
        self.liquidation_set.liquidations.remove(liquidation)
        
        # Read algorithm input files
        live_trade = False
        live_algorithm_input = self.get_algorithm_input_file("live", liquidation_datetime.date())
        for row in live_algorithm_input.itertuples():
            if row.hour == liquidation_datetime.hour:
                live_trade = row.trade if hasattr(row, 'trade') and row.trade else False
                if live_trade:
                    live_tp = row.tp
                    live_weight = row.weight
                    live_sl = row.sl
        
        reversed_trade = False
        reversed_algorithm_input = self.get_algorithm_input_file("reversed", liquidation_datetime.date())
        for row in reversed_algorithm_input.itertuples():
            if row.hour == liquidation_datetime.hour:
                reversed_trade = row.trade if hasattr(row, 'trade') and row.trade else False
                if reversed_trade:
                    reversed_tp = row.tp
                    reversed_weight = row.weight
                    reversed_sl = row.sl
        
        # Initialize trade parameters
        long_above = short_below = short_tp = short_sl = short_weight = None
        long_tp = long_sl = long_weight = cancel_above = cancel_below = None
        
        if liquidation.direction == LONG:
            below_price = round(last_candle.close * 0.995, EXCHANGE_PRICE_PRECISION)
            if reversed_trade:
                short_below = below_price
                short_tp = reversed_tp
                short_sl = reversed_sl
                short_weight = reversed_weight
            else:
                cancel_below = below_price
            
            above_price = round(last_candle.close * 1.005, EXCHANGE_PRICE_PRECISION)
            if candles_before_confirmation > 1:
                cancel_above = above_price
            else:
                if live_trade:
                    long_above = above_price
                    long_tp = live_tp
                    long_sl = live_sl
                    long_weight = live_weight
                else:
                    cancel_above = above_price
        
        elif liquidation.direction == SHORT:
            above_price = round(last_candle.close * 1.005, EXCHANGE_PRICE_PRECISION)
            if reversed_trade:
                long_above = above_price
                long_tp = reversed_tp
                long_sl = reversed_sl
                long_weight = reversed_weight
            else:
                cancel_above = above_price
            
            below_price = round(last_candle.close * 0.995, EXCHANGE_PRICE_PRECISION)
            if candles_before_confirmation > 1:
                cancel_below = below_price
            else:
                if live_trade:
                    short_below = below_price
                    short_tp = live_tp
                    short_sl = live_sl
                    short_weight = live_weight
                else:
                    cancel_below = below_price
        
        # Check if both cancel conditions are set (no trade)
        if cancel_above and cancel_below:
            return
        
        # Create position to open
        position_to_open = PositionToOpen(
            _id=liquidation._id,
            liquidation=liquidation,
            candles_before_confirmation=candles_before_confirmation,
            long_above=long_above,
            short_below=short_below,
            short_tp=short_tp,
            short_sl=short_sl,
            short_weight=short_weight,
            long_tp=long_tp,
            long_sl=long_sl,
            long_weight=long_weight,
            cancel_above=cancel_above,
            cancel_below=cancel_below,
        )
        self.positions_to_open.append(position_to_open)
        
        # Log the waiting condition
        if long_above:
            logger.info(f"[{self.mode}] ⏳ Waiting: {liquidation._id} LONG above ${long_above:,.0f}")
        if short_below:
            logger.info(f"[{self.mode}] ⏳ Waiting: {liquidation._id} SHORT below ${short_below:,.0f}")
    
    async def handle_position_to_open(
        self, position_to_open: PositionToOpen, last_candle: Candle
    ) -> None:
        """Handle a position to open (mirrors original)"""
        
        long_above = (
            position_to_open.long_above
            and last_candle.close > position_to_open.long_above
        )
        short_below = (
            position_to_open.short_below
            and last_candle.close < position_to_open.short_below
        )
        cancel_above = (
            position_to_open.cancel_above
            and last_candle.close > position_to_open.cancel_above
        )
        cancel_below = (
            position_to_open.cancel_below
            and last_candle.close < position_to_open.cancel_below
        )
        
        # Cancel if price moved to cancel zone
        if cancel_above or cancel_below:
            self.positions_to_open.remove(position_to_open)
            return
        
        # Conditions not met yet
        if not long_above and not short_below:
            return
        
        # Remove from pending
        self.positions_to_open.remove(position_to_open)
        
        # Check forbidden candles before entry
        first_candle_after_confirmation = datetime.fromtimestamp(
            position_to_open.liquidation.time
        ) + (position_to_open.candles_before_confirmation + 1) * timedelta(minutes=5)
        nr_of_candles_before_entry = (
            self.scanner.now.replace(second=0, microsecond=0)
            - first_candle_after_confirmation
        ).seconds // 300
        
        if nr_of_candles_before_entry in FORBIDDEN_NR_OF_CANDLES_BEFORE_ENTRY:
            return
        
        # Execute paper trade
        direction = LONG if long_above else SHORT
        await self.execute_paper_trade(
            direction=direction,
            position_to_open=position_to_open,
            last_candle=last_candle,
        )
    
    async def execute_paper_trade(
        self, direction: str, position_to_open: PositionToOpen, last_candle: Candle
    ) -> None:
        """Execute a paper trade"""
        
        self.order_id_counter += 1
        order_id = self.order_id_counter
        
        # Get trade parameters
        if direction == LONG:
            entry_price = last_candle.close
            sl_pct = position_to_open.long_sl
            tp_pct = position_to_open.long_tp
            weight = position_to_open.long_weight
        else:
            entry_price = last_candle.close
            sl_pct = position_to_open.short_sl
            tp_pct = position_to_open.short_tp
            weight = position_to_open.short_weight
        
        # Calculate SL/TP prices
        if direction == LONG:
            sl_price = round(entry_price * (1 - sl_pct / 100), EXCHANGE_PRICE_PRECISION)
            tp_price = round(entry_price * (1 + tp_pct / 100), EXCHANGE_PRICE_PRECISION)
        else:
            sl_price = round(entry_price * (1 + sl_pct / 100), EXCHANGE_PRICE_PRECISION)
            tp_price = round(entry_price * (1 - tp_pct / 100), EXCHANGE_PRICE_PRECISION)
        
        # Calculate position size
        size = round(self._position_size * weight / sl_pct, 1)
        size = max(size, 0.1)
        
        # Create position
        position = PaperPosition(
            order_id=order_id,
            direction=direction,
            entry_price=entry_price,
            size=size,
            stop_loss=sl_price,
            take_profit=tp_price,
            timestamp=datetime.now(),
            liquidation_id=position_to_open._id,
        )
        self.positions.append(position)
        
        logger.info(
            f"[{self.mode}] 📋 ORDER #{order_id} | {direction.upper()} @ ${entry_price:,.0f} | "
            f"SL: ${sl_price:,.0f} | TP: ${tp_price:,.0f} | Size: {size}"
        )
    
    async def check_positions(self) -> None:
        """Check if any positions should be closed (SL/TP hit)"""
        
        price = await self.get_price()
        if not price:
            return
        
        for position in deepcopy(self.positions):
            should_close = False
            close_reason = ""
            close_price = price
            
            if position.direction == LONG:
                if price <= position.stop_loss:
                    should_close = True
                    close_reason = "SL"
                    close_price = position.stop_loss
                elif price >= position.take_profit:
                    should_close = True
                    close_reason = "TP"
                    close_price = position.take_profit
            else:  # SHORT
                if price >= position.stop_loss:
                    should_close = True
                    close_reason = "SL"
                    close_price = position.stop_loss
                elif price <= position.take_profit:
                    should_close = True
                    close_reason = "TP"
                    close_price = position.take_profit
            
            if should_close:
                await self.close_position(position, close_reason, close_price)
    
    async def close_position(
        self, position: PaperPosition, reason: str, close_price: float
    ) -> None:
        """Close a position and calculate P&L"""
        
        self.positions.remove(position)
        
        # Calculate P&L
        if position.direction == LONG:
            pnl_pct = (close_price - position.entry_price) / position.entry_price
        else:
            pnl_pct = (position.entry_price - close_price) / position.entry_price
        
        pnl = position.size * pnl_pct * LEVERAGE
        self.balance += pnl
        self.total_pnl += pnl
        self.total_trades += 1
        
        if pnl > 0:
            self.wins += 1
            emoji = "🟢"
        else:
            self.losses += 1
            emoji = "🔴"
        
        logger.info(
            f"[{self.mode}] {emoji} ORDER #{position.order_id} CLOSED | {reason} | "
            f"P&L: ${pnl:+,.2f} | Balance: ${self.balance:,.2f}"
        )
    
    async def run_loop(self, last_candle: Candle) -> None:
        """Run the main loop (mirrors original)"""
        
        # Check existing positions for SL/TP
        await self.check_positions()
        
        # Handle pending positions
        for position_to_open in deepcopy(self.positions_to_open):
            await self.handle_position_to_open(position_to_open, last_candle)
            await sleep(0.1)
        
        # Handle detected liquidations
        for liquidation in deepcopy(self.liquidation_set.liquidations):
            await self.handle_liquidation(liquidation, last_candle)


# ================================================================
# Paper Scanner - Extended for 24/7 mode
# ================================================================
class PaperScanner(CoinalyzeScanner):
    """Extended scanner that can trade 24/7"""
    
    def __init__(self, now: datetime, liquidation_set: LiquidationSet, mode: str) -> None:
        super().__init__(now, liquidation_set)
        self.mode = mode
    
    async def handle_liquidation_set(self, candle: Candle, symbols: list) -> None:
        """Handle liquidation set - 24/7 mode ignores day/hour restrictions"""
        
        total_long, total_short = 0, 0
        l_time = symbols[0].get("t") if len(symbols) else 0
        nr_of_liquidations = 0
        
        for history in symbols:
            long = history.get("l", 0)
            total_long += long
            if long > 100:
                nr_of_liquidations += 1
            short = history.get("s", 0)
            total_short += short
            if short > 100:
                nr_of_liquidations += 1
        
        # Determine if we should add to liquidation list
        is_trading_day = datetime.fromtimestamp(candle.timestamp / 1000).weekday() in LIQUIDATION_DAYS
        is_trading_hour = datetime.fromtimestamp(candle.timestamp / 1000).hour in LIQUIDATION_HOURS
        
        # For 24/7 mode: always allow trading
        # For Scheduled mode: only during configured days/hours
        should_add = (self.mode == "24/7") or (is_trading_day and is_trading_hour)
        
        if total_long > MINIMAL_LIQUIDATION and nr_of_liquidations >= MINIMAL_NR_OF_LIQUIDATIONS:
            long_liquidation = Liquidation(
                _id=f"l-{datetime.fromtimestamp(candle.timestamp / 1000).strftime('%H%M')}",
                amount=total_long,
                direction="long",
                time=l_time,
                nr_of_liquidations=nr_of_liquidations,
                candle=candle,
                on_liquidation_days=is_trading_day,
                during_liquidation_hours=is_trading_hour,
            )
            if should_add:
                self.liquidation_set.liquidations.insert(0, long_liquidation)
                if self.mode == "24/7":
                    logger.info(f"🔔 LONG liquidation: ${total_long:,.0f}")
        
        if total_short > MINIMAL_LIQUIDATION and nr_of_liquidations >= MINIMAL_NR_OF_LIQUIDATIONS:
            short_liquidation = Liquidation(
                _id=f"s-{datetime.fromtimestamp(candle.timestamp / 1000).strftime('%H%M')}",
                amount=total_short,
                direction="short",
                time=l_time,
                nr_of_liquidations=nr_of_liquidations,
                candle=candle,
                on_liquidation_days=is_trading_day,
                during_liquidation_hours=is_trading_hour,
            )
            if should_add:
                self.liquidation_set.liquidations.insert(0, short_liquidation)
                if self.mode == "24/7":
                    logger.info(f"🔔 SHORT liquidation: ${total_short:,.0f}")


# ================================================================
# Main Paper Trading Loop
# ================================================================
async def main() -> None:
    print("\n" + "=" * 70)
    print("  📊 PAPER TRADING BOT")
    print("=" * 70)
    print(f"  • 24/7 Account - Trades all hours")
    print(f"  • Scheduled Account - Trades Mon-Fri, hours {list(LIQUIDATION_HOURS)}")
    print(f"  • Starting Balance: ${STARTING_BALANCE:,.2f}")
    print("=" * 70 + "\n")
    
    # === Setup 24/7 Account ===
    liquidations_247: List[Liquidation] = []
    liquidation_set_247 = LiquidationSet(liquidations=liquidations_247)
    
    scanner_247 = PaperScanner(datetime.now(), liquidation_set_247, mode="24/7")
    await scanner_247.set_symbols()
    
    exchange_247 = PaperExchange(
        liquidation_set_247, scanner_247, STARTING_BALANCE, mode="24/7"
    )
    scanner_247.exchange = exchange_247
    
    # === Setup Scheduled Account ===
    liquidations_sched: List[Liquidation] = []
    liquidation_set_sched = LiquidationSet(liquidations=liquidations_sched)
    
    scanner_sched = PaperScanner(datetime.now(), liquidation_set_sched, mode="Scheduled")
    await scanner_sched.set_symbols()
    
    exchange_sched = PaperExchange(
        liquidation_set_sched, scanner_sched, STARTING_BALANCE, mode="Scheduled"
    )
    scanner_sched.exchange = exchange_sched
    
    # Set leverage for both
    for direction in ["long", "short"]:
        await exchange_247.set_leverage(TICKER, LEVERAGE, direction)
        await exchange_sched.set_leverage(TICKER, LEVERAGE, direction)
    
    # Set initial position sizes
    await exchange_247.set_position_sizes()
    await exchange_sched.set_position_sizes()
    
    logger.info(f"Scanning BTC markets: {scanner_247.symbols[:100]}...")
    logger.info("Paper trading started!")
    
    first_run = True
    start_time = datetime.now()
    
    while True:
        now = datetime.now()
        
        # Run every 5 minutes (matching original bot)
        if (now.minute % 5 == 0 and now.second == 0) or first_run:
            if first_run:
                first_run = False
            
            # Update scanner time
            scanner_247.now = now
            scanner_sched.now = now
            
            # Get last candle
            last_candle = await exchange_247.get_last_candle(now)
            if last_candle:
                # Run loop for both accounts
                await exchange_247.run_loop(last_candle)
                await exchange_sched.run_loop(last_candle)
                
                # Fetch new liquidations (using same API call, processed by both scanners)
                symbols = await scanner_247.handle_coinalyze_url(COINALYZE_LIQUIDATION_URL)
                await scanner_247.handle_liquidation_set(last_candle, symbols)
                await scanner_sched.handle_liquidation_set(last_candle, deepcopy(symbols))
            
            await sleep(0.99)
        
        # Recalculate position sizes (every 5 min at :04)
        if now.minute % 5 == 4 and now.second == 0:
            await exchange_247.set_position_sizes()
            await exchange_sched.set_position_sizes()
            await sleep(0.99)
        
        await sleep(0.01)


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    print("\n\n" + "=" * 70)
    print("  📊 PAPER TRADING RESULTS")
    print("=" * 70)
    print("  Shutting down...")
    print("=" * 70 + "\n")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    run(main())
