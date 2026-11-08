from datetime import UTC, datetime

from barge import fmt, fuzzy


def test_size():
    assert fmt.size(0) == "0B"
    assert fmt.size(1023) == "1023B"
    assert fmt.size(1536) == "1.5K"
    assert fmt.size(412_331) == "403K"
    assert fmt.size(5 * 1024**3) == "5.0G"


def test_duration():
    assert fmt.duration(4) == "0:04"
    assert fmt.duration(3725) == "1:02:05"


def test_when_today_shows_time():
    now = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
    assert ":" in fmt.when(now, now)
    assert fmt.when(None) == ""


def test_fuzzy_prefers_word_starts_and_runs():
    ranked = [t for t, _ in fuzzy.rank("web", ["wide-backup", "web-01", "my-website", "photos"])]
    assert ranked[0] == "web-01"
    assert "photos" not in ranked
    assert ranked[-1] == "wide-backup"  # still matches, just ranked last


def test_fuzzy_positions():
    score, pos = fuzzy.match("w1", "web-01")
    assert pos == [0, 5]
    assert fuzzy.match("zz", "web") is None
