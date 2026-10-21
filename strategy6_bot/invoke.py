"""
Run a bot action, on AWS (default) or locally.

  python strategy6_bot/invoke.py STATUS
  python strategy6_bot/invoke.py TEST                      # dry run: decisions only, nothing recorded
  python strategy6_bot/invoke.py PAUSE | RESUME | FLATTEN
  python strategy6_bot/invoke.py SET_TOKEN --token <new Dhan token>
  python strategy6_bot/invoke.py CLEAR_TRADE --tid P-S2-2026-10-27
  python strategy6_bot/invoke.py TEST --local              # run in-process against the dev database
  python strategy6_bot/invoke.py ENTRY_0931 --local --prod # in-process against the live database (careful)
Options: --date YYYY-MM-DD and --time HH:MM override "now" (replays / tests).
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action")
    ap.add_argument("--date")
    ap.add_argument("--time")
    ap.add_argument("--token")
    ap.add_argument("--tid")
    ap.add_argument("--local", action="store_true", help="run in-process (uses the repo .env)")
    ap.add_argument("--prod", action="store_true", help="with --local: use the live database instead of *_dev")
    a = ap.parse_args()
    event = {"action": a.action.upper(), "by": "invoke.py"}
    for k in ("date", "time", "token", "tid"):
        if getattr(a, k):
            event[k] = getattr(a, k)

    import envfile
    vals = envfile.load()
    for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "MONGODB_URI", "EXTRA_HOLIDAYS", "AWS_REGION"):
        if k in vals:
            os.environ.setdefault(k, vals[k])
    if a.local:
        os.environ["CAPITAL"] = vals["S6_CAPITAL"]
        os.environ["DISCORD_WEBHOOK_URL"] = vals.get("S6_DISCORD_WEBHOOK_URL", "")
        os.environ["MONGO_DB"] = "strategy6" if a.prod else "strategy6_dev"
        print(f"local run against database {os.environ['MONGO_DB']}")
        import lambda_function
        print(json.dumps(lambda_function.lambda_handler(event), indent=1, default=str)[:6000])
        return
    import boto3
    lam = boto3.Session(region_name=os.environ.get("AWS_REGION", "ap-south-1")).client("lambda")
    r = lam.invoke(FunctionName="Strategy6PaperBot",
                   InvocationType="RequestResponse", LogType="Tail", Payload=json.dumps(event).encode())
    print(r["Payload"].read().decode()[:6000])
    if r.get("LogResult"):
        print("\n--- log tail ---\n" + base64.b64decode(r["LogResult"]).decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
