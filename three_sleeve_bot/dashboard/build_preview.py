"""
Build a standalone preview of the trade-journal dashboard (the same page as the Claude artifact).

  python three_sleeve_bot/dashboard/build_preview.py              # sample backtest data (sample/*.json)
  python three_sleeve_bot/dashboard/build_preview.py --mongo      # live paper trades from MongoDB (MONGODB_URI in .env)
Then open three_sleeve_bot/dashboard/preview.html in a browser (git-ignored, regenerated each run).

The page (index.html) is unchanged from the artifact; dash_shim.js stands in for the artifact runtime and theme.css
for its design tokens. With --mongo the rows come from api.py (the same functions the planned Lambda dashboard serves).
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
D3 = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mongo", action="store_true")
    ap.add_argument("--src", default="paper")
    a = ap.parse_args()
    if a.mongo:
        sys.path.insert(0, str(HERE.parent))
        import envfile
        envfile.load(into_environ=True)
        import api
        sources = {"trades": api.trades_rows(a.src), "daily": api.daily_rows(a.src)}
    else:
        sources = {"trades": json.loads((HERE / "sample" / "trades.json").read_text(encoding="utf-8")),
                   "daily": json.loads((HERE / "sample" / "daily.json").read_text(encoding="utf-8"))}
    page = (HERE / "index.html").read_text(encoding="utf-8")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Strategy 5 trade journal</title>
<style>{(HERE / "theme.css").read_text(encoding="utf-8")}</style>
<script src="{D3}"></script>
<script>window.DASH_SOURCES = {json.dumps(sources, allow_nan=False)};
window.DASH_DEFAULT_PARAMS = {{"year": "All", "sleeve": "All", "day": null}};</script>
<script>{(HERE / "dash_shim.js").read_text(encoding="utf-8")}</script>
</head><body><div id="dash-root">
{page}
</div></body></html>"""
    out = HERE / "preview.html"
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(sources['trades'])} trades, {len(sources['daily'])} days)")


if __name__ == "__main__":
    main()
