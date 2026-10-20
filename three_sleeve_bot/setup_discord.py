"""
ONE-OFF, run locally: create the bot's single Discord channel and its webhook.

  python three_sleeve_bot/setup_discord.py            # idempotent: reuses the channel/webhook if they exist
  python three_sleeve_bot/setup_discord.py --test     # also posts a test message
  python three_sleeve_bot/setup_discord.py --cleanup  # delete the earlier 6-channel layout (category three-sleeve-paper)

Reads DISCORD_BOT_TOKEN and DISCORD_GUILD_ID from the repo .env and writes DISCORD_WEBHOOK_URL back into
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
CHANNEL = "three-sleeve-paper"
TOPIC = "Three-sleeve PAPER bot: signals, trades, daily report, reconciliation, errors, system (one message per run)"
OLD_CATEGORY = "three-sleeve-paper"
OLD_KEYS = ["DISCORD_WEBHOOK_SIGNALS", "DISCORD_WEBHOOK_TRADES", "DISCORD_WEBHOOK_DAILY_REPORT",
            "DISCORD_WEBHOOK_RECONCILIATION", "DISCORD_WEBHOOK_ERRORS", "DISCORD_WEBHOOK_SYSTEM"]
WEBHOOK_NAME = "three-sleeve-bot"


def api(method, path, token, body=None):
    for attempt in range(3):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                              "User-Agent": "DiscordBot (https://github.com/three-sleeve-bot, 1.0)"})
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


def cleanup(token, guild, chans):
    cat = next((c for c in chans if c["type"] == 4 and c["name"] == OLD_CATEGORY), None)
    if cat is None:
        print("old layout: nothing to delete")
        return
    for c in [c for c in chans if c.get("parent_id") == cat["id"]]:
        api("DELETE", f"/channels/{c['id']}", token)
        print(f"  deleted #{c['name']}")
        time.sleep(0.5)
    api("DELETE", f"/channels/{cat['id']}", token)
    print(f"  deleted category {OLD_CATEGORY}")
    envfile.remove_keys(OLD_KEYS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    a = ap.parse_args()
    env = envfile.load()
    token, guild = env.get("DISCORD_BOT_TOKEN"), env.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")

    chans = api("GET", f"/guilds/{guild}/channels", token)
    if a.cleanup:
        cleanup(token, guild, chans)
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
    envfile.set_keys({"DISCORD_WEBHOOK_URL": url})
    print(f"DISCORD_WEBHOOK_URL written to {envfile.ENV_PATH} (value not shown).")

    if a.test:
        body = json.dumps({"embeds": [{"title": "✅ Webhook test", "color": 0x2ecc71,
                                       "description": "All bot notifications arrive here, one message per run."}]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "ThreeSleeveBot/1.0"})
        urllib.request.urlopen(req, timeout=10).read()
        print("test message sent")


if __name__ == "__main__":
    main()
