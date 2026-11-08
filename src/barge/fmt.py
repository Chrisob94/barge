from __future__ import annotations

from datetime import UTC, datetime


def size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n}B"
    for unit in "KMGT":
        n /= 1024
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if n < 100 else f"{n:.0f}{unit}"
    raise AssertionError("unreachable")


def when(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    local = dt.astimezone()
    if local.date() == now.astimezone().date():
        return local.strftime("%H:%M")
    if local.year == now.astimezone().year:
        return f"{local.day} {local:%b}"
    return local.strftime("%b %Y")


def rate(bytes_per_sec: float) -> str:
    return f"{size(int(bytes_per_sec))}/s"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"
