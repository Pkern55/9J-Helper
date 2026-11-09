"""Tell the user when a newer 9J Helper is available.

The latest version is published in a small Google Sheet that is shared only with the people who
use the app (the app already has permission to read Google Sheets, so there is no extra
sign-in). Tab 1 of that sheet has two columns, a name in A and its value in B:

    version   | 0.4.0
    download  | https://drive.google.com/...   (a Drive link to 9J-Helper.exe)
    notes     | optional: what's new

Every failure is silent: not being able to check must never get in the user's way.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple


def parse_version(v) -> Optional[Tuple[int, ...]]:
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*$", "" if v is None else str(v))
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(latest, current) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def read_latest(book) -> Optional[Dict[str, str]]:
    """{name: value} from the first tab, or None if it has no valid version."""
    rows = book.sheet1.get_values("A1:B20")
    info = {str(r[0]).strip().lower(): str(r[1]).strip()
            for r in rows if len(r) >= 2 and str(r[0]).strip()}
    return info if parse_version(info.get("version")) else None


def check(client, sheet_id: str, current: str) -> Optional[Dict[str, str]]:
    """The update info if a newer version is published (with a safe https download link),
    otherwise None. Never raises."""
    if not sheet_id:
        return None
    try:
        info = read_latest(client.open_by_key(sheet_id))
    except Exception:  # noqa: BLE001 - offline, no access, sheet changed...
        return None
    if info and is_newer(info["version"], current) and info.get("download", "").startswith("https://"):
        return info
    return None
