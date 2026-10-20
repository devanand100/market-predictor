"""
Rs 3L portfolio: Strategy 3 (gap-direction option buy, VIX <= 15, 5%) combined with Strategy 2 v2
(confirmed credit spread; skip credit < 23.2% of max loss, bear calls risk 8%, bull puts 4%).
Each sleeve compounds on its own share of capital; weeks keyed by expiry. Manual execution, so no
fixed monthly costs. Common period: May 2022 onward (Strategy 2 needs participant OI).

Writes data/reports/buy_spread_combo.csv
"""
import numpy as np
import pandas as pd

import final_gap_buy as gb
import final_spread_strategy as fss
from research_directional_spreads import SPREAD_W
from cost_model import option_fill_cost

START = 300_000
LOT = 65
IS_END = "2023-12-31"


def gap_sleeve(T, frac=0.05, vix_max=15):
    """Per-week return of the gap-buy sleeve, lot-exact on its own equity."""
    eq, out = 1.0 * START, {}
    for r in T.itertuples():
        if vix_max is not None and not r.vix <= vix_max:
            continue
        per_lot = r.buy * LOT
        lots = int(frac * eq // per_lot) or (1 if per_lot <= 2 * frac * eq else 0)
        if not lots:
            continue
        q = lots * LOT
        pnl = (r.sell - r.buy) * q - option_fill_cost(r.buy, q, True, r.entry) \
            - option_fill_cost(r.sell, q, False, r.exit)
        out[r.expiry] = pnl / eq
        eq += pnl
    return pd.Series(out)


def spread_sleeve(df):
    """Strategy 2 v2 per-week return on START (rescaled later), lot-exact."""
    eq, out = 1.0 * START, {}
    for r in df.itertuples():
        if r.credit / (SPREAD_W - r.credit) < 0.232:
            continue
        risk = 0.08 if r.dir == "bear call" else 0.04
        lots = max(1, int(risk * eq // (SPREAD_W * LOT)))
        pnl = r.rs * lots
        out[r.exit] = pnl / eq
        eq += pnl
    return pd.Series(out)


def stats(ret, start=START):
    ret = ret.sort_index()
    e = start * np.cumprod(1 + ret.to_numpy())
    eq = np.concatenate([[start], e])
    yrs = (pd.Timestamp(ret.index[-1]) - pd.Timestamp(ret.index[0])).days / 365.25 + 7 / 365.25
    return dict(cagr=round(100 * ((eq[-1] / start) ** (1 / yrs) - 1), 1),
                max_dd=round(100 * (eq / np.maximum.accumulate(eq) - 1).min(), 1),
                sharpe=round(ret.mean() / ret.std() * np.sqrt(52), 2),
                worst_week=round(100 * ret.min(), 1), weeks=len(ret), final=int(eq[-1]))


def blend(a, b, wa):
    """Sleeves compounding separately on wa / 1-wa of capital -> weekly portfolio returns."""
    idx = sorted(set(a.index) | set(b.index))
    ea, eb, out = wa, 1 - wa, {}
    for d in idx:
        before = ea + eb
        ea *= 1 + a.get(d, 0.0)
        eb *= 1 + b.get(d, 0.0)
        out[d] = (ea + eb) / before - 1
    return pd.Series(out)


if __name__ == "__main__":
    T = gb.trades(1.0)
    fss.rp.QTY = LOT
    df, _ = fss.spread_trades()
    g = gap_sleeve(T)
    s = spread_sleeve(df)
    t0 = min(s.index)
    g = g[g.index >= t0]
    both = sorted(set(g.index) & set(s.index))
    print(f"Common period {t0} -> {max(s.index)}; weeks traded: gap {len(g)}, spread {len(s)}, both {len(both)}")
    print("Weekly return correlation on weeks both traded:", round(np.corrcoef(g[both], s[both])[0, 1], 2))
    rows = []
    for name, wa in [("100% gap buy", 1.0), ("70/30", 0.7), ("50/50", 0.5), ("30/70", 0.3), ("100% spread v2", 0.0)]:
        p = blend(g, s, wa)
        for per, part in [("full", p), ("IS 2022-23", p[p.index <= IS_END]), ("OOS 2024-26", p[p.index > IS_END])]:
            rows.append(dict(mix=name, period=per, **stats(part)))
    R = pd.DataFrame(rows)
    R.to_csv("data/reports/buy_spread_combo.csv", index=False)
    print(R.to_string(index=False))
    p = blend(g, s, 0.5)
    yr = (1 + p).groupby(p.index.str[:4]).prod() - 1
    print("50/50 by year:", (100 * yr).round(1).to_dict())
