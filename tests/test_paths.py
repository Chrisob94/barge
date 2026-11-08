import pytest

from barge import paths


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", "/"),
        ("/", "/"),
        ("a/b", "/a/b"),
        ("/a/b/", "/a/b"),
        ("//a//b", "/a/b"),
        ("/a/../b", "/b"),
        ("/../..", "/"),
        ("a\\b", "/a/b"),
    ],
)
def test_normalize(raw, expected):
    assert paths.normalize(raw) == expected


def test_join_and_parent():
    assert paths.join("/a", "b", "c.txt") == "/a/b/c.txt"
    assert paths.join("/a", "/b") == "/a/b"
    assert paths.parent("/a/b") == "/a"
    assert paths.parent("/") == "/"
    assert paths.basename("/a/b.txt") == "b.txt"


def test_relative_to():
    assert paths.relative_to("/a/b/c", "/a") == "b/c"
    assert paths.relative_to("/a", "/a") == ""
    assert paths.relative_to("/x/y", "/") == "x/y"
    with pytest.raises(ValueError):
        paths.relative_to("/ab/c", "/a")
    assert not paths.is_inside("/ab", "/a")
