"""
Discord notifications: ONE channel, ONE webhook (DISCORD_WEBHOOK_URL), ONE message per run.

send() only collects an embed; the Lambda handler calls flush() once at the end of every run, which
posts everything from that run as a single Discord message (up to 10 embeds; anything beyond is folded
into the last one). So a run never produces more than one notification.
A send failure is printed (CloudWatch) and never raised; a 429 is waited out once, then dropped.
Discord rejects urllib's default User-Agent, so a custom one is always sent.
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

GREEN, RED, ORANGE, BLUE, GREY, PURPLE = 0x2ecc71, 0xe74c3c, 0xf39c12, 0x3498db, 0x95a5a6, 0x9b59b6
FOOTER = "Three-sleeve PAPER bot • no real orders"
TAGS = {"signals": "📡", "trades": "💹", "daily_report": "📊", "reconciliation": "🔁", "errors": "🚨", "system": "⚙️"}
MAX_EMBEDS = 10
_buffer = []
sent = []          # every flushed message (tests and the TEST action inspect this)


def fmt_rs(x):
    return f"{'−' if x < 0 else ''}₹{abs(x):,.0f}"


def send(kind, title, description="", color=BLUE, fields=None):
    """Queue one embed for this run's single message. `kind` only picks the title icon."""
    print(f"[{kind}] {title}\n{description}")
    for f in fields or []:
        print(f"  - {f['name']}: {f['value']}")
    embed = {"title": f"{TAGS.get(kind, '')} {title}".strip()[:256], "description": description[:4000], "color": color}
    if fields:
        embed["fields"] = [{"name": str(f["name"])[:256], "value": str(f["value"])[:1024] or "-",
                            "inline": f.get("inline", True)} for f in fields[:25]]
    _buffer.append(embed)


def pending():
    return list(_buffer)


def clear():
    _buffer.clear()


def flush(header=None):
    """Post everything queued in this run as one message. Returns True if something was sent."""
    if not _buffer:
        return False
    embeds = list(_buffer)
    _buffer.clear()
    if len(embeds) > MAX_EMBEDS:
        keep, rest = embeds[:MAX_EMBEDS - 1], embeds[MAX_EMBEDS - 1:]
        keep.append({"title": f"+ {len(rest)} more", "color": GREY,
                     "description": "\n\n".join(f"**{e['title']}**\n{e.get('description', '')}" for e in rest)[:4000]})
        embeds = keep
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    for e in embeds:
        e["timestamp"] = stamp
    embeds[-1]["footer"] = {"text": FOOTER}
    payload = {"embeds": embeds}
    if header:
        payload["content"] = header[:2000]
    sent.append(payload)
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "ThreeSleeveBot/1.0"})
        try:
            urllib.request.urlopen(req, timeout=8).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            print(f"Discord send failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
            return False
        except Exception as e:
            print(f"Discord send failed: {e}")
            return False
    return False
