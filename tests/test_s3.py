import io

import pytest

from barge.backends import BackendError, NotFound, S3Backend


def put(s3, key, body=b"x"):
    s3.client.put_object(Bucket="test-bucket", Key=key, Body=body)


def test_write_read_stat(s3):
    s3.write("/docs/a.txt", io.BytesIO(b"hello"))
    assert s3.open_read("/docs/a.txt").read() == b"hello"
    entry = s3.stat("/docs/a.txt")
    assert entry.size == 5 and not entry.is_dir
    assert s3.stat("/docs").is_dir


def test_list_dir_shows_folders_and_files(s3):
    put(s3, "photos/2024/a.jpg")
    put(s3, "photos/b.jpg", b"12345")
    entries = s3.list_dir("/photos")
    assert [(e.name, e.is_dir) for e in entries] == [("2024", True), ("b.jpg", False)]
    assert entries[1].size == 5


def test_empty_folder_from_mkdir(s3):
    s3.mkdir("/empty")
    assert s3.list_dir("/empty") == []
    assert [e.name for e in s3.list_dir("/")] == ["empty"]
    s3.rmdir("/empty")
    with pytest.raises(NotFound):
        s3.list_dir("/empty")


def test_rmdir_refuses_non_empty(s3):
    put(s3, "full/a.txt")
    with pytest.raises(BackendError):
        s3.rmdir("/full")


def test_missing(s3):
    with pytest.raises(NotFound):
        s3.list_dir("/nope")
    with pytest.raises(NotFound):
        s3.stat("/nope.txt")
    with pytest.raises(NotFound):
        s3.delete("/nope.txt")


def test_prefix_scoping(s3):
    put(s3, "site/index.html")
    put(s3, "other/secret.txt")
    scoped = S3Backend("test-bucket", prefix="site", client=s3.client)
    assert [e.path for e in scoped.list_dir("/")] == ["/index.html"]
    assert scoped.label == "s3://test-bucket/site"


def test_listing_more_than_1000_keys(s3):
    for i in range(1105):
        put(s3, f"big/f{i:05}.txt")
    put(s3, "big/sub/inner.txt")
    entries = s3.list_dir("/big")
    assert len(entries) == 1106
    assert sum(1 for e in s3.walk("/big") if not e.is_dir) == 1106


def test_delete_tree_handles_more_than_1000_keys(s3):
    for i in range(2050):
        put(s3, f"logs/{i // 100}/{i}.log")
    put(s3, "keep/me.txt")
    s3.delete_tree("/logs")
    with pytest.raises(NotFound):
        s3.list_dir("/logs")
    assert s3.exists("/keep/me.txt")


def test_delete_tree_refuses_whole_bucket(s3):
    with pytest.raises(BackendError):
        s3.delete_tree("/")


def test_rename_folder(s3):
    put(s3, "old/a.txt", b"a")
    put(s3, "old/deep/b.txt", b"b")
    s3.rename("/old", "/new")
    assert not s3.exists("/old")
    assert s3.open_read("/new/deep/b.txt").read() == b"b"


def test_walk_yields_parent_folders(s3):
    put(s3, "w/a/b/c.txt")
    assert [(e.path, e.is_dir) for e in s3.walk("/w")] == [
        ("/w/a", True),
        ("/w/a/b", True),
        ("/w/a/b/c.txt", False),
    ]
