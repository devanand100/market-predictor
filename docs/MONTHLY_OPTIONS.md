# Monthly-expiry options research (45 → 21 DTE)

Written up 2026-10-20 from the existing outputs in `data/reports/monthly_*.csv`.
Data: Dhan 60-minute monthly-expiry options (`download_monthly_options.py` →
`data/raw/hourly_parquet/options_monthly/`, git-ignored), ATM ±10 strikes (±3 for next month
from 2026). Strikes outside the window are priced by put-call parity or Black-76 (see
`research_monthly_straddle.py`). Sizing as Strategy 1 from Rs 5L; 69 monthly cycles 2021–26;
in-sample 2021–23, OOS 2024–26.

| Script | Idea | Result | Verdict |
|---|---|---|---|
| `research_monthly_straddle.py` | Short ATM straddle, enter ~45 DTE, close 21 DTE | 11.6% CAGR, −35.9% DD (IS 11.6 / OOS 11.7) | Works, deep DD |
| `research_monthly_straddle_vix.py` | Entry filters on the previous session | **VRP ≥ 2 (VIX − 20-day RV)**: 20.5% CAGR, −23.1% DD, 38 trades; IS 18.5 / OOS 20.0; beats 98–99% of random skips. VIX ≤ x filters hurt. | Best monthly variant |
| `research_monthly_straddle_stops.py` | Premium stops and targets | Stops ≤ 0.75× credit destroy the edge (−3% to −16% CAGR); 1.0× is harmless | Reject tight stops |
| `research_monthly_straddle_recenter.py` | Re-centre when the forward moves 1–3% | Every rule lowers CAGR; unlimited re-centring loses 20–40%/yr | Reject |
| `research_monthly_condor.py` | Iron condors / flies | All variants lose or are flat (wings inside the ±500 window are too expensive) | Reject |

Takeaway: like the weekly work, selling monthly premium earns the variance risk premium only
when implied vol is above realised (VRP ≥ 2). Not yet sized for Rs 3L or combined with the
weekly strategies.
