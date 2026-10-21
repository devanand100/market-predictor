"""
ONE-OFF, run locally: create the Strategy 6 bot's Discord channel and its webhook.

  python strategy6_bot/setup_discord.py            # idempotent: reuses the channel/webhook if they exist
  python strategy6_bot/setup_discord.py --test     # also posts a test message

Reads DISCORD_BOT_TOKEN and DISCORD_GUILD_ID from the repo .env and writes S6_DISCORD_WEBHOOK_URL back into
that .env (the URL is never printed). The Lambda only gets the webhook URL; the bot token stays here.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import envfile  # noqa: E402

API = "https://discord.com/api/v10"
CHANNEL = "strategy6-paper"
TOPIC = "Strategy 6 PAPER bot: signals, trades, daily report, errors, system (one message per run)"
WEBHOOK_NAME = "strategy6-bot"


def api(method, path, token, body=None):
    for attempt in range(3):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                              "User-Agent": "DiscordBot (https://github.com/strategy6-bot, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            if e.code == 429:
                time.sleep(float(json.loads(txt).get("retry_after", 1)) + 0.2)
                continue
            sys.exit(f"Discord {method} {path}: HTTP {e.code} {txt[:300]}")
    sys.exit(f"Discord {method} {path}: rate-limited")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    env = envfile.load()
    token, guild = env.get("DISCORD_BOT_TOKEN"), env.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")

    chans = api("GET", f"/guilds/{guild}/channels", token)
    ch = next((c for c in chans if c["type"] == 0 and c["name"] == CHANNEL and not c.get("parent_id")), None)
    if ch is None:
        ch = api("POST", f"/guilds/{guild}/channels", token, {"name": CHANNEL, "type": 0, "topic": TOPIC})
        print(f"#{CHANNEL}: created")
    else:
        print(f"#{CHANNEL}: exists")
    hooks = api("GET", f"/channels/{ch['id']}/webhooks", token) or []
    hook = next((h for h in hooks if h.get("name") == WEBHOOK_NAME and h.get("token")), None)
    if hook is None:
        hook = api("POST", f"/channels/{ch['id']}/webhooks", token, {"name": WEBHOOK_NAME})
        print(f"#{CHANNEL}: webhook created")
    url = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
    envfile.set_keys({"S6_DISCORD_WEBHOOK_URL": url})
    print(f"S6_DISCORD_WEBHOOK_URL written to {envfile.ENV_PATH} (value not shown).")

    if a.test:
        body = json.dumps({"embeds": [{"title": "✅ Webhook test", "color": 0x2ecc71,
                                       "description": "All bot notifications arrive here, one message per run."}]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "Strategy6Bot/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
        print("test message sent")


if __name__ == "__main__":
    main()
