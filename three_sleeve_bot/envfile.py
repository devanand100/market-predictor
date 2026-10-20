"""Read / update the repo-root .env (local tooling only; the Lambda reads real environment variables)."""
import os
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load(path=ENV_PATH, into_environ=False):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    if into_environ:
        for k, v in out.items():
            os.environ.setdefault(k, v)
    return out


def set_keys(updates, path=ENV_PATH):
    """Replace KEY=... lines in place (keeping everything else), append missing keys."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            done.add(m.group(1))
    missing = [k for k in updates if k not in done]
    if missing:
        lines.append("")
        lines.append("# three_sleeve_bot (written by setup_discord.py)")
        lines += [f"{k}={updates[k]}" for k in missing]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_keys(keys, path=ENV_PATH):
    if not path.exists():
        return
    keep = [l for l in path.read_text(encoding="utf-8").splitlines()
            if not (m := re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", l)) or m.group(1) not in keys]
    keep = [l for l in keep if l.strip() != "# Discord webhooks for three_sleeve_bot (written by setup_discord.py)"]
    path.write_text("\n".join(keep).rstrip("\n") + "\n", encoding="utf-8")
