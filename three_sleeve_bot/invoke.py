"""
Run a bot action, on AWS (default) or locally.

  python three_sleeve_bot/invoke.py STATUS
  python three_sleeve_bot/invoke.py TEST                      # dry run: decisions only, nothing recorded
  python three_sleeve_bot/invoke.py PAUSE | RESUME | FLATTEN
  python three_sleeve_bot/invoke.py SET_TOKEN --token <new Dhan token>
  python three_sleeve_bot/invoke.py CLEAR_TRADE --tid P-GAP-2026-10-20
  python three_sleeve_bot/invoke.py RECONCILE --recon-date 2026-10-19
  python three_sleeve_bot/invoke.py TEST --local              # run in-process against the dev database
  python three_sleeve_bot/invoke.py ENTRY_0931 --local --prod # in-process against the live database (careful)
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
    ap.add_argument("--recon-date")
    ap.add_argument("--local", action="store_true", help="run in-process (uses the repo .env)")
    ap.add_argument("--prod", action="store_true", help="with --local: use the live database instead of *_dev")
    a = ap.parse_args()
    event = {"action": a.action.upper(), "by": "invoke.py"}
    for k in ("date", "time", "token", "tid"):
        if getattr(a, k):
            event[k] = getattr(a, k)
    if a.recon_date:
        event["recon_date"] = a.recon_date

    import envfile
    envfile.load(into_environ=True)
    if a.local:
        if not a.prod:
            os.environ["MONGO_DB"] = os.environ.get("MONGO_DB", "three_sleeve") + "_dev"
        print(f"local run against database {os.environ.get('MONGO_DB', 'three_sleeve')}")
        import lambda_function
        print(json.dumps(lambda_function.lambda_handler(event), indent=1, default=str)[:6000])
        return
    import boto3
    lam = boto3.Session(region_name=os.environ.get("AWS_REGION", "ap-south-1")).client("lambda")
    r = lam.invoke(FunctionName=os.environ.get("LAMBDA_FUNCTION_NAME", "ThreeSleevePaperBot"),
                   InvocationType="RequestResponse", LogType="Tail", Payload=json.dumps(event).encode())
    print(r["Payload"].read().decode()[:6000])
    if r.get("LogResult"):
        print("\n--- log tail ---\n" + base64.b64decode(r["LogResult"]).decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
