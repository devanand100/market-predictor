"""
Build the strategy report HTML from the backtest outputs in data/reports/.

    python report/build_report.py        -> report/nifty_strategy_report.html (self-contained, open in a browser)

Run after final_strategy.py, final_spread_strategy.py and research_trade_analysis.py.
Page layout and text live in report_template.html (+ section_*.html inserted at the markers below);
numbers and charts come from the JSON built here, so reruns pick up new results automatically.
Hard-coded tables in section_*.html must be edited by hand when research results change.
"""
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
os.chdir(REPO)
from engine import load_daily  # noqa: E402

R = "data/reports/"
SECTIONS = [  # (file, insert before this marker)
    ("section_vix_filters.html", "  <section>\n    <h2>Strategy 1 rules: weekly strangle</h2>"),
]


def strategy1(d):
    s = json.load(open(R + "final_strategy_summary.json"))
    eq = pd.DataFrame(s["equity_curve"], columns=["date", "equity"])
    base = d["close"].asof(pd.Timestamp(s["full"]["start"]))
    eq["nifty"] = [round(500000 * d["close"].asof(pd.Timestamp(x)) / base) for x in eq.date]
    eq = pd.concat([pd.DataFrame([{"date": s["full"]["start"], "equity": 500000, "nifty": 500000}]), eq])
    eq["dd"] = (eq.equity / eq.equity.cummax() - 1) * 100
    out = {k: s[k] for k in ["full", "in_sample", "out_of_sample", "yearly", "monte_carlo", "stress_double_slippage",
                             "no_running_costs", "worst_trades", "benchmark_nifty_cagr_pct", "benchmark_nifty_maxdd_pct",
                             "monthly_returns", "running_costs_per_month"]}
    out["curve"] = eq.round(2).values.tolist()
    t = pd.read_csv(R + "final_trades.csv")
    out["costs"] = {"stat": int(t.stat_costs.sum()), "fixed": int(t.fixed_costs.sum()), "net": int(t.pnl.sum())}
    return out


def strategy2(d):
    sp = pd.read_csv(R + "spread_strategy_trades.csv")
    st = pd.read_csv(R + "final_trades.csv")
    s2 = json.load(open(R + "spread_strategy_summary.json"))
    t0 = pd.Timestamp(sp.entry.iloc[0])
    st_eq = st.set_index(pd.to_datetime(st.exit))["equity"]
    base_st, base_n = st_eq.asof(t0), d["close"].asof(t0)
    rows = [[str(t0.date()), 500000, 500000, 500000]]
    for r in sp.itertuples():
        ts = pd.Timestamp(r.exit)
        rows.append([r.exit, r.equity, round(500000 * st_eq.asof(ts) / base_st), round(500000 * d["close"].asof(ts) / base_n)])
    sc = pd.read_csv(R + "spread_validation.csv")
    p = sc[sc.set != "ALL"].pivot_table(index=["structure", "signal"], columns="set", values="p_value")
    keys = ["full", "in_sample_2022_23", "out_of_sample_2024_26", "no_running_costs", "monte_carlo", "yearly",
            "direction_split", "avg_lots", "avg_credit_pts", "worst_trades", "weekly_return_correlation",
            "portfolio_50_50", "strangle_only_same_period"]
    return {"curve2": rows, "s2": {k: s2[k] for k in keys},
            "pv": {f"{a}|{b}": v for (a, b), v in p.to_dict("index").items()}}


def trade_analysis():
    T = pd.read_csv(R + "spread_trades_enriched.csv")
    T["m"] = T.expiry.str[:7]
    M = T[(T.m >= "2024-01") & (T.m <= "2026-04")].groupby("m").agg(
        ret=("ret_pct", lambda x: ((1 + x / 100).prod() - 1) * 100), n=("ret_pct", "size"),
        bull=("dir", lambda x: int((x > 0).sum())), bear=("dir", lambda x: int((x < 0).sum())), vix=("vix", "mean"))
    months = pd.period_range("2024-01", "2026-04", freq="M").strftime("%Y-%m")
    monthly = [[m, round(M.ret.get(m, 0.0), 2), int(M.n.get(m, 0)), int(M.bull.get(m, 0)), int(M.bear.get(m, 0)),
                None if m not in M.index else round(M.vix[m], 1)] for m in months]
    B = T[(T.entry >= "2025-12-01") & (T.entry <= "2026-04-30")]
    cols = ["entry", "dir", "vix", "px_vs_ema20", "offset_pct", "week_move_pct", "ret_pct"]
    return {"monthly": monthly, "losersB": B[B.ret_pct < 0][cols].round(2).to_dict("records")}


def main():
    d = load_daily()
    html = open(os.path.join(HERE, "report_template.html"), encoding="utf-8").read()
    for fname, marker in SECTIONS:
        sec = open(os.path.join(HERE, fname), encoding="utf-8").read()
        assert html.count(marker) == 1, f"marker for {fname} not found"
        if sec.strip() not in html:
            html = html.replace(marker, sec + "\n" + marker)
    for tag, data in [("/*DATA*/null", strategy1(d)), ("/*DATA2*/null", strategy2(d)), ("/*DATA3*/null", trade_analysis())]:
        html = html.replace(tag, json.dumps(data, default=str).replace("NaN", "null"))
    out = os.path.join(HERE, "nifty_strategy_report.html")
    open(out, "w", encoding="utf-8").write(html)
    print("wrote", out, f"({len(html) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
