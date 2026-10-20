import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent.parent
REPO = BOT.parent
for p in (str(BOT), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)
