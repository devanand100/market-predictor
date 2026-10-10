"""
Weekend theta strangle: improvement variants, contract-exact (see research_weekend_combo.py for the base).

Each variant attacks one weakness of the base (Fri 15:15 short strangle, dynamic delta 0.15-0.22,
VIX >= 12, Monday 09:20 stop at 2.5x a leg, exit Monday 15:15):
  - strike distance: static delta 0.10 / 0.15 / 0.25
  - regime: VIX >= 14 / >= 16
  - holding: exit at Monday 09:20 (weekend decay only, no Monday intraday)
  - stop: continuous Monday stop on combined loss >= 1x credit (minute closes), instead of one 09:20 check
  - defined risk: buy wings at the edge of the stored strike window (ATM +/- 500 at entry) -> iron condor,
    with and without the stop
  - expiry regime: only Thursday-expiry era (pre Sep-2025) vs Tuesday era is reported per period
Judge on 2021-23, check 2024-26. Legs outside the +/-500 window use intrinsic (generous to short legs,
harsh to long wings). Writes data/reports/weekend_variants_summary.csv.
"""
import pandas as pd

from cost_model import option_fill_cost, slip
from engine import atm_strike, load_daily, load_days
from research_weekend_combo import LOT, strike_for_delta, vix_1515

M_FRI, M_0920, M_1515 = 360, 5, 360
BASE = dict(delta="dyn", vix_min=12.0, sl_mult=2.5, stop="0920", exit=M_1515, wings=False)


def trade(D, M, fri, mon, v, p):
    spot = D.spot[M_FRI]
    dl = min(0.22, max(0.15, 0.10 + 0.006 * v)) if p["delta"] == "dyn" else p["delta"]
    ce, pe = strike_for_delta(spot, v, dl, "CE"), strike_for_delta(spot, v, dl, "PE")
    legs = [("CE", ce, -1), ("PE", pe, -1)]
    if p["wings"]:
        K = atm_strike(spot)
        legs += [("CE", max(K + 500, ce + 100), +1), ("PE", min(K - 500, pe - 100), +1)]
    entry = [(t, k, s, D.px(k, t, M_FRI)) for t, k, s in legs]
    fills = [(t, k, s, px - slip(px) if s < 0 else px + slip(px)) for t, k, s, px in entry]
    credit = -sum(s * f for _, _, s, f in fills)

    def value(m):  # cost to close (points) at Monday minute m, using closes
        return sum(-s * M.px(k, t, m) for t, k, s, _ in fills)

    m_exit, urgent = p["exit"], False
    if p["stop"] == "0920":
        if any(s < 0 and M.px(k, t, M_0920) >= p["sl_mult"] * f for t, k, s, f in fills):
            m_exit, urgent = M_0920, True
    elif p["stop"] == "cont":
        for m in range(0, p["exit"] + 1):
            if value(m) - credit >= p["sl_mult"] * credit:
                m_exit, urgent = m, True
                break
    pts, cost = 0.0, 0.0
    for t, k, s, f in fills:
        px = M.px(k, t, m_exit)
        out = px + slip(px, urgent) if s < 0 else max(0.05, px - slip(px, urgent))
        pts += (f - out) * (-s)   # short: sold f, bought back out; long: the reverse
        cost += option_fill_cost(f, LOT, s > 0, fri) + option_fill_cost(out, LOT, s < 0, mon)
    outside = any(D.col(k) is None or M.col(k) is None for _, k, _, _ in fills)
    return {"entry": fri, "exit": mon, "vix": v, "credit": credit, "pts": pts, "rs": pts * LOT - cost,
            "stopped": urgent, "outside": outside, "move_pct": (M.spot[m_exit] / spot - 1) * 100}


def run(days, daily, p):
    vix = vix_1515()
    exp = set(daily.index[daily.is_expiry == True].strftime("%Y-%m-%d"))
    rows = []
    for fri in sorted(days):
        f = pd.Timestamp(fri)
        mon = (f + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        if f.dayofweek != 4 or mon not in days or fri in exp:
            continue
        v = vix.get(fri)
        if v is None or v < p["vix_min"]:
            continue
        rows.append(trade(days[fri], days[mon], fri, mon, v, p))
    return pd.DataFrame(rows)


def summarise(t, name):
    out = []
    for per, g in (("2021-23", t[t.entry < "2024"]), ("2024-26", t[t.entry >= "2024"]), ("all", t)):
        r = g.rs
        out.append({"variant": name, "period": per, "n": len(g), "total_rs": r.sum(), "avg_rs": r.mean(),
                    "win%": (r > 0).mean() * 100, "worst_rs": r.min(),
                    "pf": r[r > 0].sum() / max(1.0, -r[r <= 0].sum()),
                    "max_dd_rs": (r.cumsum() - r.cumsum().cummax()).min(),
                    "stops": int(g.stopped.sum()), "outside_window": int(g.outside.sum())})
    return out


def main():
    days, daily = load_days(), load_daily()
    variants = {
        "base (reference rules)": {},
        "delta 0.10 static": {"delta": 0.10},
        "delta 0.15 static": {"delta": 0.15},
        "delta 0.25 static": {"delta": 0.25},
        "VIX >= 14": {"vix_min": 14.0},
        "VIX >= 16": {"vix_min": 16.0},
        "exit Mon 09:20 always": {"exit": M_0920, "stop": None},
        "continuous stop 1x credit": {"stop": "cont", "sl_mult": 1.0},
        "iron condor, wings at +/-500": {"wings": True},
        "iron condor, no stop": {"wings": True, "stop": None},
        "iron condor + exit 09:20": {"wings": True, "exit": M_0920, "stop": None},
        "delta 0.10 + exit 09:20": {"delta": 0.10, "exit": M_0920, "stop": None},
    }
    rows, trades = [], {}
    for name, ch in variants.items():
        t = run(days, daily, {**BASE, **ch})
        trades[name] = t
        rows += summarise(t, name)
    res = pd.DataFrame(rows)
    res.to_csv("data/reports/weekend_variants_summary.csv", index=False)
    pd.set_option("display.width", 250, "display.max_columns", 20)
    for per in ("2021-23", "2024-26", "all"):
        print(f"\n=== {per} (per lot, ₹) ===")
        print(res[res.period == per].drop(columns="period").round(1).to_string(index=False))
    print("\nApr-2025 crash weekend (2025-04-04) per variant:")
    for name, t in trades.items():
        x = t[t.entry == "2025-04-04"]
        print(f"  {name:32s} {'skipped' if x.empty else f'₹{x.rs.iloc[0]:,.0f}'}")


if __name__ == "__main__":
    main()
