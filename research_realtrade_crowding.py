"""
Do "crowded sellers" measures improve Strategy 6's short strangles? (real-market replay, research_realtrade.py)

Each S2 / E0 trade is taken at 1 lot (Rs 10 crore account, no running costs) so its per-unit P&L can be bucketed by a
crowding measure known before the 09:30 entry:
  A vrp        previous VIX close - previous session's RV20 (low = options not overpriced)
  B underpaid  mean over the last 8 completed weekly cycles of |spot(expiry 15:15) - spot(S2 entry)| / ATM straddle at
               the S2 entry (high = sellers were underpaid lately)
  C richness   today's ATM straddle at 09:30 / (0.8 x spot x VIX/100 x sqrt(T)) (low = thin premium today)
  D client_z   client net short index options (short - long, CE + PE), z-score over 60 sessions, previous session
               (high = retail unusually short options); also pro_z, fii_z
  E oi_rel     total weekly option OI (CE + PE, +/-500 window) at 09:30 / its mean on the same session-to-expiry over
               the last 8 cycles (high = crowded book)
Thresholds come from 2021-2023 trades only (terciles); 2024+ is out-of-sample.
Then the filters that help in-sample are run on the full Strategy 6 portfolio (Rs 5L bot settings, Rs 15L research).

  python research_realtrade_crowding.py          # stage 1 (features + buckets) and stage 2 (portfolio)
Writes data/reports/crowding_features.csv, crowding_trades.csv, crowding_buckets.csv, crowding_portfolio.csv
"""
import math
import sys

import numpy as np
import pandas as pd

import research_realtrade as rt
from cost_model import get_lot_size
from research_realtrade_grid import strangle, long_dir

sys.path.insert(0, "strategy6_bot")
import rules as R  # noqa: E402

FEATS = ["vrp", "underpaid", "richness", "client_z", "pro_z", "fii_z", "oi_rel"]
GOOD_HIGH = {"vrp": True, "underpaid": False, "richness": True, "client_z": False, "pro_z": False, "fii_z": False,
             "oi_rel": False}      # direction in which the measure means "sellers are well paid / not crowded"


def features():
    days, daily = rt.data()
    info, tds = rt.calendar(days, daily)
    d = pd.DataFrame(index=[x for x in tds if x in info])
    vix_prev = daily["vix_prev"].reindex(pd.to_datetime(d.index)).to_numpy()
    rv_prev = daily["rv20"].shift(1).reindex(pd.to_datetime(d.index)).to_numpy()
    d["vrp"] = vix_prev - rv_prev
    # C: richness at 09:30
    rich, oi_tot = [], []
    for x in d.index:
        day, m = days[x], rt.ENTRY_MIN
        s = day.spot[m]
        K = rt.atm_strike(s)
        ce, pe = rt.real(day, K, "CE", m), rt.real(day, K, "PE", m)
        i, n, exp = info[x]
        T = max((pd.Timestamp(exp) + pd.Timedelta(hours=15, minutes=30)
                 - (pd.Timestamp(x) + pd.Timedelta(hours=9, minutes=30))).total_seconds(), 3600) / (365 * 86400)
        v = daily.loc[x, "vix_prev"]
        rich.append((ce + pe) / (0.8 * s * v / 100 * math.sqrt(T)) if ce and pe and v == v else np.nan)
        oi_tot.append(np.nan)
    d["richness"] = rich
    # B: trailing underpayment of the S2-entry straddle
    cyc = {}
    for x, (i, n, exp) in info.items():
        if n - 1 - i == 2:
            day = days[x]
            s0 = day.spot[rt.ENTRY_MIN]
            K = rt.atm_strike(s0)
            ce, pe = rt.real(day, K, "CE", rt.ENTRY_MIN), rt.real(day, K, "PE", rt.ENTRY_MIN)
            if ce and pe and exp in days:
                cyc[exp] = abs(days[exp].spot[rt.EXIT_MIN] - s0) / (ce + pe)
    ce_ = pd.Series(cyc).sort_index()
    d["underpaid"] = [ce_[ce_.index < x].tail(8).mean() if (ce_.index < x).sum() >= 4 else np.nan for x in d.index]
    # D: participant OI (previous session)
    p = pd.read_csv("data/processed/participant_oi_features.csv", parse_dates=["date"]).set_index("date").sort_index()
    for who in ("client", "pro", "fii"):
        net = (p[f"{who}_opt_idx_ce_short"] + p[f"{who}_opt_idx_pe_short"]
               - p[f"{who}_opt_idx_ce_long"] - p[f"{who}_opt_idx_pe_long"])
        z = (net - net.rolling(60).mean()) / net.rolling(60).std()
        z = z.shift(1)                                       # previous session only (no look-ahead)
        d[f"{who}_z"] = z.reindex(pd.to_datetime(d.index), method="ffill").to_numpy()
    # E: OI at 09:30 relative to the same session-to-expiry in the last 8 cycles
    o = pd.read_parquet("data/processed/options_1min.parquet", columns=["date", "minute", "strike", "ce_oi", "pe_oi", "spot"],
                        filters=[("minute", "==", rt.ENTRY_MIN)])
    o = o[(o.strike - o.spot).abs() <= 500]
    tot = o.groupby("date")[["ce_oi", "pe_oi"]].sum().sum(axis=1)
    left = pd.Series({x: info[x][1] - 1 - info[x][0] for x in d.index})
    rel = {}
    for x in d.index:
        if x not in tot.index:
            continue
        same = [y for y in left.index if y < x and left[y] == left[x] and y in tot.index][-8:]
        if len(same) >= 4:
            rel[x] = tot[x] / np.mean([tot[y] for y in same])
    d["oi_rel"] = pd.Series(rel)
    d.index.name = "date"
    return d


def trades():
    W = rt.Weekly
    cfg = [("S2", [W("S2", strangle(1.25), dte=2, stop=1.0, vix_min=15)], 1e8, False),
           ("E0", [W("E0", strangle(0.5), dte=0, stop=0.5, vix_min=15)], 1e8, False)]
    out = []
    for nm, (E, T, a) in rt.replay_many(cfg).items():
        T = T.copy()
        T["pu"] = T.pnl / (T.lots * T.entry.map(get_lot_size))
        out.append(T[["sleeve", "entry", "exit", "reason", "pnl", "pu", "vix"]])
    return pd.concat(out)


def buckets(T, F):
    rows = []
    for sl, g in T.groupby("sleeve"):
        g = g.join(F, on="entry")
        isg, oos = g[g.entry <= rt.IS_END], g[g.entry > rt.IS_END]
        base = dict(sleeve=sl, feature="(none)", is_pf=rt.pf(isg.pu), oos_pf=rt.pf(oos.pu), is_n=len(isg), oos_n=len(oos),
                    is_pts=round(isg.pu.sum()), oos_pts=round(oos.pu.sum()))
        rows.append(base)
        for f in FEATS:
            x = isg[f].dropna()
            if len(x) < 30:
                continue
            q1, q2 = x.quantile([1 / 3, 2 / 3])
            lab = lambda v: np.nan if v != v else ("low" if v <= q1 else "mid" if v <= q2 else "high")
            r = dict(sleeve=sl, feature=f, q1=round(q1, 3), q2=round(q2, 3))
            for part, gg in (("is", isg), ("oos", oos)):
                b = gg.assign(b=gg[f].map(lab))
                for k in ("low", "mid", "high"):
                    s = b[b.b == k].pu
                    r[f"{part}_{k}_pf"], r[f"{part}_{k}_n"], r[f"{part}_{k}_avg"] = rt.pf(s), len(s), round(s.mean(), 1) if len(s) else None
            # filter: skip the "crowded" tercile (low if GOOD_HIGH else high); trades with no value are kept
            bad = "low" if GOOD_HIGH[f] else "high"
            for part, gg in (("is", isg), ("oos", oos)):
                keep = gg[gg[f].map(lab) != bad].pu
                r[f"{part}_keep_pf"], r[f"{part}_keep_n"], r[f"{part}_keep_pts"] = rt.pf(keep), len(keep), round(keep.sum())
            r["skip"] = f"{bad} tercile ({"<=" if bad == "low" else ">"} {q1 if bad == 'low' else q2:.3g})"
            rows.append(r)
    return pd.DataFrame(rows)


if __name__ == "__main__" and len(sys.argv) == 1:
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    F = features()
    F.to_csv("data/reports/crowding_features.csv")
    print(F.describe().round(2).T[["count", "mean", "min", "50%", "max"]], flush=True)
    T = trades()
    T.to_csv("data/reports/crowding_trades.csv", index=False)
    B = buckets(T, F)
    B.to_csv("data/reports/crowding_buckets.csv", index=False)
    cols = ["sleeve", "feature", "skip", "is_low_pf", "is_mid_pf", "is_high_pf", "oos_low_pf", "oos_mid_pf", "oos_high_pf",
            "is_pf", "is_keep_pf", "is_keep_n", "oos_pf", "oos_keep_pf", "oos_keep_n"]
    print(B.reindex(columns=cols).to_string(index=False))


# ---------------------------------------------------------------- stage 2: the surviving filter
class RichGate(rt.Weekly):
    """Weekly structure that skips the entry when today's ATM straddle at 09:30 is thin relative to VIX
    (richness = straddle / (0.8 x spot x VIX_prev/100 x sqrt(T)) below `min_rich`)."""

    def __init__(self, *a, min_rich=None, **k):
        super().__init__(*a, **k)
        self.min_rich = min_rich

    def enter(self, acct, day, m, exp, dctx):
        if self.min_rich:
            s = day.spot[m]
            K = rt.atm_strike(s)
            ce, pe = rt.real(day, K, "CE", m), rt.real(day, K, "PE", m)
            T = max((pd.Timestamp(exp) + pd.Timedelta(hours=15, minutes=30)
                     - (pd.Timestamp(acct.date) + pd.Timedelta(hours=9, minutes=30))).total_seconds(), 3600) / (365 * 86400)
            v = dctx["vix"]
            if ce is None or pe is None or not (v == v) or (ce + pe) / (0.8 * s * v / 100 * math.sqrt(T)) < self.min_rich:
                return
        return super().enter(acct, day, m, exp, dctx)


def s6_sleeves(w2, we, fg, e0_rich=None, s2_rich=None):
    return [RichGate("S2", strangle(1.25), dte=2, stop=1.0, vix_min=15, mode="risk", weight=w2, min_rich=s2_rich),
            RichGate("E0", strangle(0.5), dte=0, stop=0.5, vix_min=15, mode="risk", weight=we, min_rich=e0_rich),
            rt.Weekly("GAP", long_dir(0), dte=4, direction="gap", tp=1.0, tp_limit=True, vix_max=13, mode="risk", weight=fg)]


def stage2():
    T = pd.read_csv("data/reports/crowding_trades.csv")
    F = pd.read_csv("data/reports/crowding_features.csv", index_col=0)
    e = T[T.sleeve == "E0"].join(F, on="entry")
    print("\nE0 richness threshold sensitivity (skip below):")
    for q in (0.0, 0.2, 0.25, 1 / 3, 0.4, 0.5):
        thr = e[e.entry <= rt.IS_END].richness.quantile(q) if q else 0
        k = e[(e.richness >= thr) | e.richness.isna()]
        i, o = k[k.entry <= rt.IS_END], k[k.entry > rt.IS_END]
        print(f"  q {q:.2f} thr {thr:.3f} | IS n {len(i)} PF {rt.pf(i.pu)} pts {i.pu.sum():.0f} | "
              f"OOS n {len(o)} PF {rt.pf(o.pu)} pts {o.pu.sum():.0f}")
    thr = round(e[e.entry <= rt.IS_END].richness.quantile(1 / 3), 3)
    rt.RUNNING = R.MONTHLY_RUNNING_COSTS
    rows = []
    for label, margin, cap, w in (("bot S6 Rs 5L, 9% model", 0.09, 500_000, (0.55, 0.55, 0.05)),
                                  ("S6 Rs 15L, 11.6% margin", 0.116, 1_500_000, (0.45, 0.45, 0.05))):
        rt.MARGIN_PCT = margin
        cfg = [(f"{label} | no filter", s6_sleeves(*w), cap, True),
               (f"{label} | E0 richness >= {thr}", s6_sleeves(*w, e0_rich=thr), cap, True)]
        for nm, (E, Tt, a) in rt.replay_many(cfg).items():
            r = rt.report(nm, E, Tt, a, cap)
            rt.show(r)
            rows.append(r)
    rt.MARGIN_PCT = 0.09
    pd.DataFrame(rows).to_csv("data/reports/crowding_portfolio.csv", index=False)
    return thr


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "stage2":
    stage2()
