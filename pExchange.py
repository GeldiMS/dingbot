"""Paper Trading Exchange - Real market data, fake money"""
from asyncio import sleep
from copy import deepcopy
import os
import ccxt.pro as ccxt
from coinalyze_scanner import CoinalyzeScanner
from datetime import datetime, timedelta, date
from decouple import config, Csv
from logger import logger
from misc import (
    Candle,
    Liquidation,
    LiquidationSet,
    PositionToOpen,
    TPLimitOrderToPlace,
)
from paper_logger import PaperLogger, PaperAccount
import pandas as pd
from typing import List, Tuple, Optional
from dataclasses import dataclass, field


TICKER: str = "BTC/USDT:USDT"
EXCHANGE_PRICE_PRECISION: int = config("EXCHANGE_PRICE_PRECISION", cast=int, default="1")
BINANCE_EXCHANGE = ccxt.binance()

LONG = "long"
SHORT = "short"

LEVERAGE = config("LEVERAGE", cast=int, default="25")
USE_FIXED_RISK = config("USE_FIXED_RISK", cast=bool, default=False)
if USE_FIXED_RISK:
    FIXED_RISK_EX_FEES = config("FIXED_RISK_EX_FEES", cast=float, default="50.0")
else:
    POSITION_PERCENTAGE = config("POSITION_PERCENTAGE", cast=float, default="1.0")

FORBIDDEN_NR_OF_CANDLES_BEFORE_ENTRY = config(
    "FORBIDDEN_NR_OF_CANDLES_BEFORE_ENTRY",
    cast=Csv(int),
    default="1",
)

# Paper trading fees (matching BloFin)
TAKER_FEE = 0.0005  # 0.05%
MAKER_FEE = 0.0002  # 0.02%


@dataclass
class PaperPosition:
    """Open paper position"""
    position_id: str
    direction: str
    entry_price: float
    size: float
    stop_loss_price: float
    take_profit_price: Optional[float]
    entry_timestamp: int


@dataclass
class PaperOrder:
    """Pending paper order"""
    order_id: str
    direction: str
    price: float
    size: float
    stop_loss_price: float
    take_profit_price: float
    timestamp: int


class PaperExchange:
    """Paper trading exchange - uses real prices, tracks fake money"""
    
    def __init__(
        self,
        liquidation_set: LiquidationSet,
        scanner: CoinalyzeScanner,
        starting_balance: float = 1000.0,
        mode: str = "24/7",  # "24/7" or "scheduled"
        paper_logger: PaperLogger = None
    ) -> None:
        self.liquidation_set = liquidation_set
        self.scanner = scanner
        self.mode = mode
        self.paper_logger = paper_logger
        
        # Account state
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.peak_balance = starting_balance
        self.max_drawdown = 0.0
        
        # Positions and orders
        self.open_positions: List[PaperPosition] = []
        self.pending_orders: List[PaperOrder] = []
        self.tp_limit_orders_to_place: List[TPLimitOrderToPlace] = []
        self.positions_to_open: List[PositionToOpen] = []
        
        # Trade tracking
        self.trade_history: List[dict] = []
        self.wins = 0
        self.losses = 0
        self.order_id_counter = 0
        
        # Position sizing
        self._position_size = 0.1
        
        # Compatibility
        self.positions: List[dict] = []
        self.market_sl_orders: List[dict] = []
        self.limit_orders: List[dict] = []
        self.discord_message_queue = []  # Not used in paper trading
    
    def get_account_state(self) -> PaperAccount:
        """Get current account state for logging"""
        return PaperAccount(
            name=f"Paper Account ({self.mode})",
            mode=self.mode,
            starting_balance=self.starting_balance,
            balance=self.balance,
            trades=len(self.trade_history),
            wins=self.wins,
            losses=self.losses,
            peak_balance=self.peak_balance,
            max_drawdown=self.max_drawdown
        )
    
    def _generate_order_id(self) -> str:
        """Generate unique order ID"""
        self.order_id_counter += 1
        return f"paper_{self.mode}_{self.order_id_counter}"
    
    async def get_open_positions(self) -> List[dict]:
        """Get open positions (from memory)"""
        self.positions = [
            {
                "amount": f"{pos.size} contract(s)",
                "direction": pos.direction,
                "price": f"$ {round(pos.entry_price, EXCHANGE_PRICE_PRECISION):,}",
            }
            for pos in self.open_positions
        ]
        return self.positions
    
    async def set_leverage(self, symbol: str, leverage: int, direction: str) -> None:
        """Set leverage (no-op for paper)"""
        logger.info(f"[PAPER-{self.mode}] Set leverage to {leverage}x for {direction}")
    
    async def get_last_candle(self, now: datetime) -> Candle | None:
        """Get REAL last candle from Binance"""
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
            logger.error(f"[PAPER] Error fetching candle: {e}")
            return None
    
    async def set_position_sizes(self) -> None:
        """Calculate position size based on paper balance"""
        try:
            price = await self.get_price()
            if not price:
                price = 95000.0
            
            if USE_FIXED_RISK:
                usdt_size = FIXED_RISK_EX_FEES * (1 / LEVERAGE * 100)
            else:
                usdt_size = self.balance / LEVERAGE * POSITION_PERCENTAGE
            position_size = round(usdt_size / price * LEVERAGE * 1000, 1)
            self._position_size = max(position_size, 0.1)
        except Exception as e:
            logger.error(f"[PAPER] Error setting position size: {e}")
            self._position_size = 0.1
    
    @property
    def position_size(self) -> float:
        return self._position_size
    
    async def process_market_movements(self, candle: Candle) -> None:
        """Check if orders fill or positions hit SL/TP based on real prices"""
        
        # Check pending orders
        for order in deepcopy(self.pending_orders):
            filled = False
            
            if order.direction == LONG:
                if candle.low <= order.price:
                    filled = True
                    fill_price = order.price
            else:
                if candle.high >= order.price:
                    filled = True
                    fill_price = order.price
            
            if filled:
                self.pending_orders.remove(order)
                position = PaperPosition(
                    position_id=order.order_id,
                    direction=order.direction,
                    entry_price=fill_price,
                    size=order.size,
                    stop_loss_price=order.stop_loss_price,
                    take_profit_price=order.take_profit_price,
                    entry_timestamp=candle.timestamp
                )
                self.open_positions.append(position)
                
                # Entry fee
                position_value = order.size * fill_price / 1000
                fee = position_value * MAKER_FEE
                self.balance -= fee
                
                logger.info(f"[PAPER-{self.mode}] Order filled: {order.direction.upper()} @ ${fill_price:,.2f}")
        
        # Check positions for TP/SL
        for position in deepcopy(self.open_positions):
            closed = False
            close_reason = None
            exit_price = None
            
            if position.direction == LONG:
                if position.take_profit_price and candle.high >= position.take_profit_price:
                    closed = True
                    close_reason = "tp"
                    exit_price = position.take_profit_price
                elif candle.low <= position.stop_loss_price:
                    closed = True
                    close_reason = "sl"
                    exit_price = position.stop_loss_price
            else:
                if position.take_profit_price and candle.low <= position.take_profit_price:
                    closed = True
                    close_reason = "tp"
                    exit_price = position.take_profit_price
                elif candle.high >= position.stop_loss_price:
                    closed = True
                    close_reason = "sl"
                    exit_price = position.stop_loss_price
            
            if closed:
                # Calculate P&L
                position_value = position.size * position.entry_price / 1000
                
                if position.direction == LONG:
                    pnl = (exit_price - position.entry_price) / position.entry_price * position_value * LEVERAGE
                else:
                    pnl = (position.entry_price - exit_price) / position.entry_price * position_value * LEVERAGE
                
                # Exit fee
                exit_value = position.size * exit_price / 1000
                exit_fee = exit_value * (MAKER_FEE if close_reason == "tp" else TAKER_FEE)
                pnl -= exit_fee
                
                # Update balance
                self.balance += pnl
                
                # Track wins/losses
                if pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                
                # Drawdown tracking
                if self.balance > self.peak_balance:
                    self.peak_balance = self.balance
                current_drawdown = self.peak_balance - self.balance
                if current_drawdown > self.max_drawdown:
                    self.max_drawdown = current_drawdown
                
                # Record trade
                self.trade_history.append({
                    "timestamp": candle.timestamp,
                    "trade_id": position.position_id,
                    "direction": position.direction,
                    "entry_price": position.entry_price,
                    "exit_price": exit_price,
                    "size": position.size,
                    "pnl": pnl,
                    "reason": close_reason
                })
                
                self.open_positions.remove(position)
                
                # Log trade
                if self.paper_logger:
                    self.paper_logger.log_trade(
                        f"Paper-{self.mode}",
                        position.direction,
                        position.entry_price,
                        exit_price,
                        pnl,
                        close_reason
                    )
                
                logger.info(f"[PAPER-{self.mode}] Position closed: {position.direction.upper()} "
                          f"P&L: ${pnl:+,.2f}, Balance: ${self.balance:,.2f}")
    
    async def handle_position_to_open(
        self, position_to_open: PositionToOpen, last_candle: Candle
    ) -> None:
        """Handle position opening logic (same as real bot)"""
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
        
        if cancel_above or cancel_below:
            self.positions_to_open.remove(position_to_open)
            return
        
        if not long_above and not short_below:
            return
        
        self.positions_to_open.remove(position_to_open)
        
        # Check candle timing
        first_candle_after_confirmation = datetime.fromtimestamp(
            position_to_open.liquidation.time
        ) + (position_to_open.candles_before_confirmation + 1) * timedelta(minutes=5)
        nr_of_candles_before_entry = (
            self.scanner.now.replace(second=0, microsecond=0)
            - first_candle_after_confirmation
        ).seconds // 300
        
        if nr_of_candles_before_entry in FORBIDDEN_NR_OF_CANDLES_BEFORE_ENTRY:
            return
        
        # Place order
        amount = round(
            self.position_size
            * (position_to_open.long_weight if long_above else position_to_open.short_weight)
            / (position_to_open.long_sl if long_above else position_to_open.short_sl),
            1,
        )
        amount = max(amount, 0.1)
        
        price, stoploss_price, takeprofit_price = await self.limit_order_placement(
            direction=LONG if long_above else SHORT,
            amount=amount,
            stoploss_percentage=position_to_open.long_sl if long_above else position_to_open.short_sl,
            takeprofit_percentage=position_to_open.long_tp if long_above else position_to_open.short_tp,
        )
    
    async def check_if_entry_orders_are_closed(self) -> None:
        """No-op for paper trading - TP handled in process_market_movements"""
        pass
    
    async def get_algorithm_input_file(
        self, strategy_type: str, input_date: date
    ) -> pd.DataFrame:
        """Get algorithm input file (same as real bot)"""
        try:
            return pd.read_csv(
                f"algorithm_input/algorithm_input-{input_date}-{strategy_type}.csv"
            )
        except:
            file_names = [
                name for name in os.listdir("algorithm_input/")
                if os.path.isfile(os.path.join("algorithm_input/", name))
                and name.startswith("algorithm_input-")
                and strategy_type in name
            ]
            file_names.sort()
            return pd.read_csv(f"algorithm_input/{file_names[-1]}")
    
    async def handle_liquidation(
        self, liquidation: Liquidation, last_candle: Candle
    ) -> None:
        """Handle liquidation (same as real bot)"""
        liquidation_datetime = datetime.fromtimestamp(liquidation.candle.timestamp / 1000)
        
        # Check age - silently remove old liquidations
        if liquidation_datetime < (
            self.scanner.now.replace(second=0, microsecond=0) - timedelta(minutes=15)
        ):
            self.liquidation_set.liquidations.remove(liquidation)
            return
        
        # Check reaction
        is_strong = await self.reaction_to_liquidation_is_strong(liquidation, last_candle.close)
        
        if not is_strong:
            return  # Not ready yet, check again next loop
            
        now = self.scanner.now.replace(second=0, microsecond=0)
        candles_before_confirmation = (
            int(round((now - liquidation_datetime).total_seconds() / 300, 0)) - 1
        )
        self.liquidation_set.liquidations.remove(liquidation)
        
        # Read algorithm inputs
        live_trade = False
        live_algorithm_input = await self.get_algorithm_input_file(
            strategy_type="live", input_date=liquidation_datetime.date()
        )
        for row in live_algorithm_input.itertuples():
            if row.hour == liquidation_datetime.hour:
                live_trade = row.trade
                if live_trade:
                    live_tp = row.tp
                    live_weight = row.weight
                    live_sl = row.sl
        
        reversed_trade = False
        reversed_algorithm_input = await self.get_algorithm_input_file(
            strategy_type="reversed", input_date=liquidation_datetime.date()
        )
        for row in reversed_algorithm_input.itertuples():
            if row.hour == liquidation_datetime.hour:
                reversed_trade = row.trade
                if reversed_trade:
                    reversed_tp = row.tp
                    reversed_weight = row.weight
                    reversed_sl = row.sl
        
        # For 24/7 mode, force trade if neither algorithm allows it
        if self.mode == "24/7" and not live_trade and not reversed_trade:
            live_trade = True
            live_tp = 4.0
            live_sl = 1.0
            live_weight = 0.5
        
        # Build position params
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
        
        if cancel_above and cancel_below:
            return  # No valid entry, skip silently
        
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
        
        # CLEAN ORDER LOG - only log when order is created
        direction = "LONG" if long_above else "SHORT"
        entry_price = long_above or short_below
        logger.info(f"📋 ORDER CREATED [{self.mode}] | {direction} | Entry: ${entry_price:,.0f} | Reason: {liquidation.direction.upper()} liquidation ${liquidation.amount * last_candle.close:,.0f}")
    
    async def run_loop(self, last_candle: Candle) -> None:
        """Run the main loop"""
        await self.process_market_movements(last_candle)
        
        for position_to_open in deepcopy(self.positions_to_open):
            await self.handle_position_to_open(position_to_open, last_candle)
            await sleep(0.01)
        
        if self.tp_limit_orders_to_place:
            await self.check_if_entry_orders_are_closed()
        
        for liquidation in deepcopy(self.liquidation_set.liquidations):
            await self.handle_liquidation(liquidation, last_candle)
        
        # Order summary - show if there are pending orders
        pending_count = len(self.pending_orders)
        positions_to_open_count = len(self.positions_to_open)
        open_positions_count = len(self.open_positions)
        
        if pending_count > 0 or positions_to_open_count > 0 or open_positions_count > 0:
            logger.info(f"📊 [{self.mode}] ORDERS: {pending_count} pending | {positions_to_open_count} waiting for entry | {open_positions_count} open positions")
    
    async def reaction_to_liquidation_is_strong(
        self, liquidation: Liquidation, price: float
    ) -> bool:
        """Check if reaction is strong"""
        if (liquidation.direction == LONG and price > liquidation.candle.high) or (
            liquidation.direction == SHORT and price < liquidation.candle.low
        ):
            return True
        return False
    
    async def get_sl_and_tp_price(
        self, direction: str, price: float, stoploss_percentage: float, takeprofit_percentage: float
    ) -> tuple[float, float]:
        """Calculate SL and TP prices"""
        stoploss_price = (
            round(price * (1 - stoploss_percentage / 100), EXCHANGE_PRICE_PRECISION)
            if direction == LONG
            else round(price * (1 + stoploss_percentage / 100), EXCHANGE_PRICE_PRECISION)
        )
        takeprofit_price = (
            round(price * (1 + takeprofit_percentage / 100), EXCHANGE_PRICE_PRECISION)
            if direction == LONG
            else round(price * (1 - takeprofit_percentage / 100), EXCHANGE_PRICE_PRECISION)
        )
        return stoploss_price, takeprofit_price
    
    async def get_price(self) -> float | None:
        """Get REAL current price from Binance"""
        try:
            ticker = await BINANCE_EXCHANGE.fetch_ticker(symbol=TICKER)
            return ticker["last"]
        except Exception as e:
            logger.error(f"[PAPER] Error fetching price: {e}")
            return None
    
    async def limit_order_placement(
        self, direction: str, amount: float, stoploss_percentage: float, takeprofit_percentage: float
    ) -> Tuple[float, float, float]:
        """Place paper order (no real order, just track locally)"""
        price = await self.get_price()
        price = (
            round(price * 1.0001, EXCHANGE_PRICE_PRECISION)
            if direction == SHORT
            else round(price * 0.9999, EXCHANGE_PRICE_PRECISION)
        )
        stoploss_price, takeprofit_price = await self.get_sl_and_tp_price(
            direction, price, stoploss_percentage, takeprofit_percentage
        )
        
        order = PaperOrder(
            order_id=self._generate_order_id(),
            direction=direction,
            price=price,
            size=amount,
            stop_loss_price=stoploss_price,
            take_profit_price=takeprofit_price,
            timestamp=int(self.scanner.now.timestamp() * 1000)
        )
        self.pending_orders.append(order)
        
        if self.paper_logger:
            self.paper_logger.log_order_placed(
                f"Paper-{self.mode}",
                direction,
                price,
                stoploss_price,
                takeprofit_price
            )
        
        logger.info(f"[PAPER-{self.mode}] Order placed: {direction.upper()} @ ${price:,.2f}")
        
        return price, stoploss_price, takeprofit_price
