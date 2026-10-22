"""
Discord notifications: one channel per book (Strategy 6: DISCORD_WEBHOOK_URL, Strategy 7: its own URL),
ONE message per book channel per run.

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
FOOTER = "Strategy 6 PAPER bot • no real orders"
TAGS = {"signals": "📡", "trades": "💹", "daily_report": "📊", "reconciliation": "🔁", "errors": "🚨", "system": "⚙️"}
MAX_EMBEDS = 10
_buffers = {}      # book -> embeds queued in this run
_urls, _footers = {}, {}
_cur = "s6"
sent = []          # every flushed message (tests and the TEST action inspect this); each carries its "book"


def use(book, url=None, footer=None):
    """Route the following send() calls to one book's channel."""
    global _cur
    _cur = book
    if url is not None:
        _urls[book] = url
    if footer is not None:
        _footers[book] = footer


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
    _buffers.setdefault(_cur, []).append(embed)


def pending():
    return list(_buffers.get(_cur, []))


def clear():
    _buffers.clear()


def flush(header=None):
    """Post everything queued in this run as ONE message per book channel. Returns the number sent."""
    n = 0
    for book in list(_buffers):
        n += _flush_one(book, _buffers.pop(book), header)
    return n


def _flush_one(book, embeds, header):
    if not embeds:
        return 0
    if len(embeds) > MAX_EMBEDS:
        keep, rest = embeds[:MAX_EMBEDS - 1], embeds[MAX_EMBEDS - 1:]
        keep.append({"title": f"+ {len(rest)} more", "color": GREY,
                     "description": "\n\n".join(f"**{e['title']}**\n{e.get('description', '')}" for e in rest)[:4000]})
        embeds = keep
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    for e in embeds:
        e["timestamp"] = stamp
    embeds[-1]["footer"] = {"text": _footers.get(book, FOOTER)}
    payload = {"embeds": embeds}
    if header:
        payload["content"] = header[:2000]
    sent.append(dict(payload, book=book))
    url = _urls.get(book) if book in _urls else os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return 1
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "Strategy6Bot/1.0"})
        try:
            urllib.request.urlopen(req, timeout=8).read()
            return 1
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            print(f"Discord send failed ({book}): HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")
            return 1
        except Exception as e:
            print(f"Discord send failed ({book}): {e}")
            return 1
    return 1
