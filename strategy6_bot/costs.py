"""
Statutory charges and slippage, copied from ../cost_model.py so the Lambda package is self-contained.
tests/test_costs.py asserts these give the same numbers as the research cost model.
"""
BROKERAGE_PER_ORDER = 20.0
GST_RATE = 0.18
SEBI_FEE = 10 / 1e7            # Rs 10 per crore
STAMP_OPT_BUY = 0.00003        # 0.003%
LOT = 65
MONTHLY_RUNNING_COSTS = 1990.0  # Rs/month: Dhan Data API + VPS + misc (cost_model.MONTHLY_RUNNING_COSTS)


def stt_option_sell(date_str):
    if date_str >= "2026-04-01":
        return 0.0015
    if date_str >= "2024-10-01":
        return 0.001
    if date_str >= "2023-04-01":
        return 0.000625
    return 0.0005


def exch_option(date_str):
    return 0.0003503 if date_str >= "2024-10-01" else 0.000503


def option_fill_charges(premium, qty, is_buy, date_str):
    """Charges (Rs) for one option order of `qty` units at `premium`, itemised."""
    turnover = premium * qty
    exch = turnover * exch_option(date_str)
    sebi = turnover * SEBI_FEE
    stt = 0.0 if is_buy else turnover * stt_option_sell(date_str)
    stamp = turnover * STAMP_OPT_BUY if is_buy else 0.0
    gst = GST_RATE * (BROKERAGE_PER_ORDER + exch + sebi)
    return {"brokerage": BROKERAGE_PER_ORDER, "stt": stt, "exch": exch, "sebi": sebi, "stamp": stamp, "gst": gst}


def option_fill_cost(premium, qty, is_buy, date_str):
    return sum(option_fill_charges(premium, qty, is_buy, date_str).values())


def slip(premium, urgent=False):
    """Adverse slippage in option points per fill: 0.3 + 0.5% normally, 0.5 + 1.5% on stops."""
    if urgent:
        return 0.5 + 0.015 * premium
    return 0.3 + 0.005 * premium


def add_charges(a, b):
    return {k: a.get(k, 0.0) + b.get(k, 0.0) for k in set(a) | set(b)}
