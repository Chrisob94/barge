# Parses `ls -la` output. Only used in SCP mode, where the server won't give us SFTP
# and the shell is all we have for listing directories.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_LINE = re.compile(
    r"^(?P<perms>[-dlbcps][-rwxsStT]{9})[+@.]?\s+"
    r"\d+\s+"
    r"\S+\s+"
    r"(?:\S+\s+)?"
    r"(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time_or_year>\d{1,2}:\d{2}|\d{4})\s+"
    r"(?P<name>.+)$"
)

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


@dataclass(frozen=True, slots=True)
class LsItem:
    name: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: datetime | None


def parse_line(line: str, now: datetime | None = None) -> LsItem | None:
    m = _LINE.match(line.rstrip("\r\n"))
    if not m:
        return None  # "total 48", blank lines, error output

    now = now or datetime.now(UTC)
    kind = m["perms"][0]
    name = m["name"]
    if kind == "l" and " -> " in name:
        name = name.split(" -> ", 1)[0]

    return LsItem(
        name=name,
        is_dir=kind == "d",
        is_link=kind == "l",
        size=int(m["size"]),
        mtime=_mtime(m, now),
    )


def parse(output: str, now: datetime | None = None) -> list[LsItem]:
    items = []
    for line in output.splitlines():
        item = parse_line(line, now)
        if item and item.name not in (".", ".."):
            items.append(item)
    return items


def _mtime(m: re.Match[str], now: datetime) -> datetime | None:
    month = m["month"].lower()
    if month not in MONTHS:
        return None
    month_no = MONTHS.index(month) + 1
    day = int(m["day"])
    tok = m["time_or_year"]
    try:
        if ":" not in tok:
            return datetime(int(tok), month_no, day, tzinfo=UTC)
        # ls leaves the year off for the last ~6 months, so "Dec 30 10:00" seen in
        # January means last year
        hour, minute = map(int, tok.split(":"))
        dt = datetime(now.year, month_no, day, hour, minute, tzinfo=UTC)
        if dt > now + timedelta(days=1):
            dt = dt.replace(year=now.year - 1)
        return dt
    except ValueError:
        return None  # Feb 29 in a non-leap year etc.
