from datetime import UTC, datetime

from barge.backends import lsparse

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

LISTING = """total 24
drwxr-xr-x  4 deploy www-data 4096 Mar  2 10:15 .
drwxr-xr-x  3 root   root     4096 Jan  9  2025 ..
drwxr-xr-x  2 deploy www-data 4096 Mar  2 10:15 assets
-rw-r--r--  1 deploy www-data 18342 Mar  2 10:15 index.html
-rw-r--r--@ 1 chris  staff      24 Dec 30 09:00 robots.txt
lrwxrwxrwx  1 deploy www-data   11 Feb 14  2024 current -> releases/41
-rw-r--r--  1 deploy www-data    7 Mar  1 08:00 name with spaces.txt
"""


def test_parses_names_types_and_sizes():
    items = {i.name: i for i in lsparse.parse(LISTING, NOW)}
    assert set(items) == {"assets", "index.html", "robots.txt", "current", "name with spaces.txt"}
    assert items["assets"].is_dir
    assert items["index.html"].size == 18342
    assert items["current"].is_link and not items["current"].is_dir


def test_year_less_dates_in_the_future_mean_last_year():
    items = {i.name: i for i in lsparse.parse(LISTING, NOW)}
    assert items["index.html"].mtime == datetime(2026, 3, 2, 10, 15, tzinfo=UTC)
    assert items["robots.txt"].mtime == datetime(2025, 12, 30, 9, 0, tzinfo=UTC)
    assert items["current"].mtime == datetime(2024, 2, 14, tzinfo=UTC)


def test_ignores_junk():
    assert lsparse.parse_line("total 48") is None
    assert lsparse.parse_line("ls: cannot access 'x': No such file or directory") is None
