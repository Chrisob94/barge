import io

import pytest

from barge.backends import BackendError, LocalBackend, NotFound


@pytest.fixture
def local(tmp_path):
    return LocalBackend(tmp_path)


def test_write_creates_parents_and_reports_progress(local, tmp_path):
    seen = []
    local.write("/a/b/file.txt", io.BytesIO(b"x" * 3000), progress=seen.append)
    assert (tmp_path / "a/b/file.txt").read_bytes() == b"x" * 3000
    assert sum(seen) == 3000


def test_failed_write_leaves_no_partial_file(local, tmp_path):
    class Boom(io.BytesIO):
        def read(self, n=-1):
            raise OSError("disk on fire")

    with pytest.raises(OSError):
        local.write("/f.txt", Boom())
    assert list(tmp_path.iterdir()) == []


def test_list_sorts_dirs_first(local, tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "A.txt").write_text("a")
    (tmp_path / "zdir").mkdir()
    assert [e.name for e in local.list_dir("/")] == ["zdir", "A.txt", "b.txt"]


def test_missing_things(local):
    with pytest.raises(NotFound):
        local.list_dir("/nope")
    with pytest.raises(NotFound):
        local.stat("/nope")
    assert not local.exists("/nope")


def test_rename_refuses_to_clobber(local, tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")
    with pytest.raises(BackendError):
        local.rename("/a", "/b")
    local.rename("/a", "/sub/c")
    assert (tmp_path / "sub/c").read_text() == "a"


def test_delete_tree_and_root_guard(local, tmp_path):
    (tmp_path / "d/e").mkdir(parents=True)
    (tmp_path / "d/e/f").write_text("f")
    local.delete_tree("/d")
    assert not (tmp_path / "d").exists()
    with pytest.raises(BackendError):
        local.delete_tree("/")


def test_walk(local, tmp_path):
    (tmp_path / "a/b").mkdir(parents=True)
    (tmp_path / "a/b/c.txt").write_text("c")
    assert [e.path for e in local.walk("/")] == ["/a", "/a/b", "/a/b/c.txt"]
