"""
Indian F&O statutory cost model (Nifty index options & futures), date-aware.

Corrections vs the earlier version of this file:
  * NSE options exchange transaction charge is ~0.035-0.05% of premium, NOT 0.00297%.
  * SEBI turnover fee is Rs 10 / crore (0.0001%).
  * STT history on option SELL premium: 0.05% -> 0.0625% (Apr-2023) -> 0.1% (Oct-2024)
    -> 0.15% (Apr-2026, Union Budget 2026-27).
All strategies close positions before 15:30 on expiry day, so the 0.125% STT on
exercised ITM options is never incurred.

Slippage is modelled separately (see `slip`), because it is the cost retail traders
most underestimate: half-spread + market-impact of a market order on 1-minute data.
"""

BROKERAGE_PER_ORDER = 20.0
GST_RATE = 0.18
SEBI_FEE = 10 / 1e7            # Rs 10 per crore
STAMP_OPT_BUY = 0.00003        # 0.003%
STAMP_FUT_BUY = 0.00002        # 0.002%


def stt_option_sell(date_str: str) -> float:
    if date_str >= "2026-04-01":
        return 0.0015
    if date_str >= "2024-10-01":
        return 0.001
    if date_str >= "2023-04-01":
        return 0.000625
    return 0.0005


def stt_future_sell(date_str: str) -> float:
    if date_str >= "2026-04-01":
        return 0.0005
    if date_str >= "2024-10-01":
        return 0.0002
    if date_str >= "2023-04-01":
        return 0.000125
    return 0.0001


def exch_option(date_str: str) -> float:
    return 0.0003503 if date_str >= "2024-10-01" else 0.000503


def exch_future(date_str: str) -> float:
    return 0.0000173 if date_str >= "2024-10-01" else 0.000019


def option_fill_cost(premium: float, qty: int, is_buy: bool, date_str: str) -> float:
    """Statutory charges (Rs) for one option order of `qty` units at `premium`."""
    turnover = premium * qty
    exch = turnover * exch_option(date_str)
    sebi = turnover * SEBI_FEE
    stt = 0.0 if is_buy else turnover * stt_option_sell(date_str)
    stamp = turnover * STAMP_OPT_BUY if is_buy else 0.0
    gst = GST_RATE * (BROKERAGE_PER_ORDER + exch + sebi)
    return BROKERAGE_PER_ORDER + exch + sebi + stt + stamp + gst


def future_fill_cost(price: float, qty: int, is_buy: bool, date_str: str) -> float:
    turnover = price * qty
    exch = turnover * exch_future(date_str)
    sebi = turnover * SEBI_FEE
    stt = 0.0 if is_buy else turnover * stt_future_sell(date_str)
    stamp = turnover * STAMP_FUT_BUY if is_buy else 0.0
    gst = GST_RATE * (BROKERAGE_PER_ORDER + exch + sebi)
    return BROKERAGE_PER_ORDER + exch + sebi + stt + stamp + gst


def slip(premium: float, urgent: bool = False) -> float:
    """Adverse slippage in option points per fill.
    Normal limit/market fill: 0.3 pt + 0.5% of premium.
    Stop-loss (urgent, market into a moving book): 0.5 pt + 1.5% of premium."""
    if urgent:
        return 0.5 + 0.015 * premium
    return 0.3 + 0.005 * premium


FUT_SLIP_PTS = 1.0   # Nifty futures: ~0.5 pt half-spread + impact, per fill


# Lot size history (for reference / live sizing). Backtests size in units.
def get_lot_size(date_str: str) -> int:
    if date_str >= "2025-12-30":
        return 65
    if date_str >= "2024-11-20":
        return 75
    if date_str >= "2024-04-26":
        return 25
    if date_str >= "2021-07-01":
        return 50
    return 75


# Fixed monthly running costs for a retail algo setup (Rs / month)
MONTHLY_RUNNING_COSTS = {
    "Broker data API (Dhan Data API incl. GST)": 590,
    "Cloud VPS (Mumbai region, 2 vCPU)": 1000,
    "Misc (internet backup, monitoring)": 400,
}
