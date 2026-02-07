"""
Paper trading scanner - Modified for 24/7 trading (no time restrictions)
"""
from datetime import datetime, timedelta
from decouple import config, Csv
from functools import cached_property

from discord_client import USE_DISCORD

from logger import logger
from misc import Candle, DiscordMessage, Liquidation, LiquidationSet
import requests
from typing import List


COINALYZE_SECRET_API_KEY = config("COINALYZE_SECRET_API_KEY")
COINALYZE_LIQUIDATION_URL = "https://api.coinalyze.net/v1/liquidation-history"
FUTURE_MARKETS_URL = "https://api.coinalyze.net/v1/future-markets"

MINIMAL_NR_OF_LIQUIDATIONS = config("MINIMAL_NR_OF_LIQUIDATIONS", default="1", cast=int)
MINIMAL_LIQUIDATION = config("MINIMAL_LIQUIDATION", default="2000", cast=int)
N_MINUTES_TIMEDELTA = config("N_MINUTES_TIMEDELTA", default="5", cast=int)
INTERVAL = config("INTERVAL", default="5min")
LIQUIDATION_DAYS = config("LIQUIDATION_DAYS", cast=Csv(int), default="0,1,2,3,4")
LIQUIDATION_HOURS = config("LIQUIDATION_HOURS", cast=Csv(int), default="2,3,4,14,15,16")


class PaperScanner:
    """Paper trading scanner - can be configured to trade 24/7 or on schedule"""

    def __init__(self, now: datetime, liquidation_set: LiquidationSet, mode: str = "24/7") -> None:
        self.now = now
        self.liquidation_set = liquidation_set
        self.exchange = None
        self.mode = mode  # "24/7" or "Scheduled"

    @property
    def request_params(self) -> dict:
        """Returns the params for the request to the API"""
        rounded_now = self.now.replace(second=0, microsecond=0) - timedelta(
            minutes=self.now.minute % 5
        )
        return {
            "symbols": self.symbols,
            "from": int(
                datetime.timestamp(rounded_now - timedelta(minutes=N_MINUTES_TIMEDELTA))
            ),
            "to": int(datetime.timestamp(rounded_now)),
            "interval": INTERVAL,
        }

    @cached_property
    def symbols(self) -> str:
        """Returns the symbols for the request to the API"""
        return self._symbols

    async def set_symbols(self) -> None:
        """Returns the symbols for the request to the API"""
        symbols = []
        if hasattr(self, "_symbols"):
            symbols = self._symbols.split(",")
        for market in await self.handle_coinalyze_url(
            url=FUTURE_MARKETS_URL, include_params=False, symbols=True
        ):
            if (symbol := market.get("symbol", "").upper()).startswith("BTCUSD"):
                symbols.append(symbol)
        self._symbols = ",".join(list(set(symbols)))

    async def handle_liquidation_set(self, candle: Candle, symbols: list) -> None:
        """Handle the liquidation set and check for liquidations"""

        total_long, total_short = 0, 0
        l_time = 0
        nr_of_liquidations = 0
        
        for symbol_data in symbols:
            for history in symbol_data.get("history", []):
                l_time = history.get("t", 0)
                long = history.get("l", 0)
                total_long += long
                if long > 100:
                    nr_of_liquidations += 1
                short = history.get("s", 0)
                total_short += short
                if short > 100:
                    nr_of_liquidations += 1

        # Check if we should trade based on mode
        is_trading_time = True
        if self.mode == "Scheduled":
            is_trading_day = datetime.fromtimestamp(candle.timestamp / 1000).weekday() in LIQUIDATION_DAYS
            is_trading_hour = datetime.fromtimestamp(candle.timestamp / 1000).hour in LIQUIDATION_HOURS
            is_trading_time = is_trading_day and is_trading_hour

        if (
            total_long > MINIMAL_LIQUIDATION
            and nr_of_liquidations >= MINIMAL_NR_OF_LIQUIDATIONS
            and is_trading_time
        ):
            long_liquidation = Liquidation(
                _id=str(
                    "l-"
                    + datetime.fromtimestamp(candle.timestamp / 1000).strftime("%H%M")
                ),
                amount=total_long,
                direction="long",
                time=l_time,
                nr_of_liquidations=nr_of_liquidations,
                candle=candle,
                on_liquidation_days=True,  # Always true for paper
                during_liquidation_hours=True,  # Always true for paper
            )
            self.liquidation_set.liquidations.insert(0, long_liquidation)
            logger.info(f"[PAPER-{self.mode}] 📈 LONG liquidation detected: {total_long:.3f} BTC")
            
        if (
            total_short > MINIMAL_LIQUIDATION
            and nr_of_liquidations >= MINIMAL_NR_OF_LIQUIDATIONS
            and is_trading_time
        ):
            short_liquidation = Liquidation(
                _id=str(
                    "s-"
                    + datetime.fromtimestamp(candle.timestamp / 1000).strftime("%H%M")
                ),
                amount=total_short,
                direction="short",
                time=l_time,
                nr_of_liquidations=nr_of_liquidations,
                candle=candle,
                on_liquidation_days=True,
                during_liquidation_hours=True,
            )
            self.liquidation_set.liquidations.insert(0, short_liquidation)
            logger.info(f"[PAPER-{self.mode}] 📉 SHORT liquidation detected: {total_short:.3f} BTC")

    async def handle_coinalyze_url(
        self, url: str, include_params: bool = True, symbols: bool = False
    ) -> List[dict]:
        """Handle the url and check for liquidations"""
        try:
            params = self.request_params if include_params else {}
            response = requests.get(
                url,
                params=params,
                headers={"api_key": COINALYZE_SECRET_API_KEY},
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            if not symbols:
                logger.info(f"COINALYZE: {data}")
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"[PAPER] Error fetching coinalyze data: {e}")
            return []
