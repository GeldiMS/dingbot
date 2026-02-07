"""Live console dashboard for paper trading"""
from datetime import datetime
from typing import Dict, List
from dataclasses import dataclass
import os


@dataclass
class PaperAccount:
    """Paper trading account state"""
    name: str
    mode: str  # "24/7" or "scheduled"
    starting_balance: float
    balance: float
    trades: int
    wins: int
    losses: int
    peak_balance: float
    max_drawdown: float
    
    @property
    def pnl(self) -> float:
        return self.balance - self.starting_balance
    
    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.starting_balance) * 100
    
    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return (self.wins / self.trades) * 100


class PaperLogger:
    """Live logging dashboard for paper trading"""
    
    # ANSI color codes
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    
    def __init__(self):
        self.last_update = datetime.now()
    
    @staticmethod
    def clear_screen():
        """Clear terminal screen"""
        os.system('cls' if os.name == 'nt' else 'clear')
    
    def format_currency(self, amount: float) -> str:
        """Format currency with color"""
        color = self.GREEN if amount >= 0 else self.RED
        sign = '+' if amount > 0 else ''
        return f"{color}${sign}{amount:,.2f}{self.RESET}"
    
    def format_percentage(self, pct: float) -> str:
        """Format percentage with color"""
        color = self.GREEN if pct >= 0 else self.RED
        sign = '+' if pct > 0 else ''
        return f"{color}{sign}{pct:.2f}%{self.RESET}"
    
    def print_header(self):
        """Print dashboard header"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n{self.BOLD}{self.CYAN}{'='*70}{self.RESET}")
        print(f"{self.BOLD}{self.CYAN}  🤖 PAPER TRADING DASHBOARD - {now}{self.RESET}")
        print(f"{self.BOLD}{self.CYAN}{'='*70}{self.RESET}\n")
    
    def print_account(self, account: PaperAccount):
        """Print account status"""
        emoji = "🌍" if account.mode == "24/7" else "📅"
        
        print(f"{self.BOLD}{emoji} {account.name.upper()} ({account.mode}){self.RESET}")
        print(f"  Balance:     ${account.balance:,.2f}")
        print(f"  P&L:         {self.format_currency(account.pnl)} ({self.format_percentage(account.pnl_pct)})")
        print(f"  Trades:      {account.trades} (W:{self.GREEN}{account.wins}{self.RESET} / L:{self.RED}{account.losses}{self.RESET})")
        print(f"  Win Rate:    {self.format_percentage(account.win_rate)}")
        print(f"  Max DD:      {self.format_currency(-account.max_drawdown)}")
        print()
    
    def print_dashboard(self, account_247: PaperAccount, account_scheduled: PaperAccount, btc_price: float):
        """Print full dashboard"""
        self.clear_screen()
        self.print_header()
        
        print(f"{self.DIM}Current BTC Price: ${btc_price:,.2f}{self.RESET}\n")
        
        self.print_account(account_247)
        self.print_account(account_scheduled)
        
        # Comparison
        diff = account_247.pnl - account_scheduled.pnl
        if diff > 0:
            winner = "24/7 Trading"
        elif diff < 0:
            winner = "Scheduled Trading"
            diff = -diff
        else:
            winner = "Tied"
        
        print(f"{self.BOLD}📊 COMPARISON:{self.RESET}")
        print(f"  Leader: {self.GREEN}{winner}{self.RESET} (+${diff:,.2f})")
        print(f"\n{self.DIM}Press Ctrl+C to stop and see final results{self.RESET}")
    
    def log_trade(self, account_name: str, direction: str, entry: float, 
                  exit_price: float, pnl: float, reason: str):
        """Log a trade execution"""
        now = datetime.now().strftime("%H:%M:%S")
        result = "✅" if pnl > 0 else "❌"
        dir_color = self.GREEN if direction == "long" else self.RED
        
        print(f"\n{result} [{now}] {self.BOLD}{account_name}{self.RESET} | "
              f"{dir_color}{direction.upper()}{self.RESET} | "
              f"Entry: ${entry:,.2f} → Exit: ${exit_price:,.2f} | "
              f"P&L: {self.format_currency(pnl)} | {reason.upper()}")
    
    def log_liquidation(self, direction: str, amount: float, hour: int):
        """Log detected liquidation"""
        now = datetime.now().strftime("%H:%M:%S")
        emoji = "🔴" if direction == "long" else "🟢"
        print(f"\n{emoji} [{now}] Liquidation detected: {direction.upper()} ${amount:,.0f} (hour {hour})")
    
    def log_order_placed(self, account_name: str, direction: str, price: float, sl: float, tp: float):
        """Log order placement"""
        now = datetime.now().strftime("%H:%M:%S")
        dir_color = self.GREEN if direction == "long" else self.RED
        print(f"\n📝 [{now}] {self.BOLD}{account_name}{self.RESET} | "
              f"{dir_color}{direction.upper()}{self.RESET} order @ ${price:,.2f} | "
              f"SL: ${sl:,.2f} | TP: ${tp:,.2f}")
    
    def log_info(self, message: str):
        """Log info message"""
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\nℹ️  [{now}] {message}")
    
    def log_error(self, message: str):
        """Log error message"""
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n{self.RED}⚠️  [{now}] ERROR: {message}{self.RESET}")
    
    def print_final_results(self, account_247: PaperAccount, account_scheduled: PaperAccount, 
                           runtime_hours: float):
        """Print final results summary"""
        self.clear_screen()
        print(f"\n{self.BOLD}{self.CYAN}{'='*70}{self.RESET}")
        print(f"{self.BOLD}{self.CYAN}  📊 PAPER TRADING FINAL RESULTS{self.RESET}")
        print(f"{self.BOLD}{self.CYAN}{'='*70}{self.RESET}\n")
        
        print(f"Runtime: {runtime_hours:.1f} hours\n")
        
        self.print_account(account_247)
        self.print_account(account_scheduled)
        
        # Determine winner
        diff = account_247.pnl - account_scheduled.pnl
        if diff > 0:
            winner = "🏆 24/7 Trading WINS"
            advantage = diff
        elif diff < 0:
            winner = "🏆 Scheduled Trading WINS"
            advantage = -diff
        else:
            winner = "🤝 TIE"
            advantage = 0
        
        print(f"{self.BOLD}{self.GREEN}{winner}{self.RESET}")
        if advantage > 0:
            print(f"   Advantage: +${advantage:,.2f}")
        
        print(f"\n{self.BOLD}Thank you for using Paper Trading!{self.RESET}\n")
