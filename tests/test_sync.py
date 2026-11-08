from datetime import UTC, datetime, timedelta

from barge import sync
from barge.backends import Entry, LocalBackend
from barge.transfer import TransferQueue

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def e(path, size, mtime=T0):
    return Entry(path, False, size, mtime)


def test_diff_new_changed_unchanged():
    src = {"a": e("/a", 1), "b": e("/b", 2), "c": e("/c", 3, T0 + timedelta(minutes=5)), "d": e("/d", 4)}
    dst = {"b": e("/b", 9), "c": e("/c", 3), "d": e("/d", 4, T0 + timedelta(seconds=1))}
    steps = sync.diff(src, dst)
    assert [(s.action, s.rel) for s in steps] == [
        (sync.Action.NEW, "a"),
        (sync.Action.CHANGED, "b"),
        (sync.Action.CHANGED, "c"),
    ]
    assert sync.unchanged_count(src, steps) == 1


def test_size_only_ignores_mtime():
    src = {"c": e("/c", 3, T0 + timedelta(days=1))}
    dst = {"c": e("/c", 3)}
    assert sync.diff(src, dst, use_mtime=False) == []


def test_delete_extras_only_when_asked():
    src = {"a": e("/a", 1)}
    dst = {"a": e("/a", 1), "old": e("/old", 5)}
    assert sync.diff(src, dst) == []
    [step] = sync.diff(src, dst, delete=True)
    assert step.action is sync.Action.DELETE and step.rel == "old"


def test_end_to_end_local_to_s3(tmp_path, s3):
    root = tmp_path / "site"
    (root / "css").mkdir(parents=True)
    (root / "index.html").write_text("<h1>v2</h1>")
    (root / "css/app.css").write_text("body{}")
    s3.client.put_object(Bucket="test-bucket", Key="www/index.html", Body=b"<h1>v1</h1>!")
    s3.client.put_object(Bucket="test-bucket", Key="www/stale.js", Body=b"old")

    local = LocalBackend(tmp_path)
    src_index = sync.index(local, "/site")
    steps = sync.diff(src_index, sync.index(s3, "/www"), delete=True)
    assert {s.rel: s.action for s in steps} == {
        "css/app.css": sync.Action.NEW,
        "index.html": sync.Action.CHANGED,
        "stale.js": sync.Action.DELETE,
    }

    q = TransferQueue()
    q.submit(sync.jobs_for(steps, local, "/site", s3, "/www"))
    q.wait(10)
    q.shutdown()
    assert sync.apply_deletes(steps, s3, "/www") == []
    assert sorted(sync.index(s3, "/www")) == ["css/app.css", "index.html"]
    assert sync.diff(sync.index(local, "/site"), sync.index(s3, "/www"), use_mtime=False) == []


def test_index_of_missing_folder_is_empty(tmp_path):
    assert sync.index(LocalBackend(tmp_path), "/nope") == {}
