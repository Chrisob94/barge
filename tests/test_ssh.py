import io

import pytest

from barge.backends import BackendError, NotFound, SSHBackend


def test_connects_over_sftp(sftp):
    sftp.list_dir("/")
    assert sftp.mode == "sftp"
    assert sftp.label.startswith("sftp://tester@127.0.0.1")


def test_write_read_list(sftp):
    progress = []
    sftp.write("/a/b/hello.txt", io.BytesIO(b"hi" * 1000), size=2000, progress=progress.append)
    assert (sftp.disk / "a/b/hello.txt").read_bytes() == b"hi" * 1000
    assert sum(progress) == 2000
    assert [e.name for e in sftp.list_dir("/a/b")] == ["hello.txt"]
    with sftp.open_read("/a/b/hello.txt") as f:
        assert f.read() == b"hi" * 1000


def test_stat_and_missing(sftp):
    (sftp.disk / "d").mkdir()
    assert sftp.stat("/d").is_dir
    with pytest.raises(NotFound):
        sftp.stat("/missing")
    with pytest.raises(NotFound):
        sftp.list_dir("/missing")


def test_rename_mkdir_delete(sftp):
    sftp.mkdir("/x/y/z")
    assert (sftp.disk / "x/y/z").is_dir()
    sftp.write("/x/f.txt", io.BytesIO(b"f"), size=1)
    sftp.rename("/x/f.txt", "/x/y/g.txt")
    assert (sftp.disk / "x/y/g.txt").exists()
    with pytest.raises(BackendError):
        sftp.rename("/x/y", "/x/y/z")  # already exists
    sftp.delete_tree("/x")
    assert not (sftp.disk / "x").exists()


def test_clone_gets_its_own_connection(sftp):
    sftp.list_dir("/")
    other = sftp.clone()
    assert other is not sftp
    assert other.mode == "sftp"
    other.list_dir("/")
    assert other._client is not sftp._client
    other.close()


def test_rejects_unknown_host_key(sftp_server, tmp_path):
    empty = tmp_path / "known_hosts"
    empty.write_text("")
    b = SSHBackend(
        "127.0.0.1", port=sftp_server.port, user="tester", password="hunter2", known_hosts=str(empty)
    )
    with pytest.raises(BackendError, match="known_hosts"):
        b.list_dir("/")


def test_bad_password(sftp_server):
    b = SSHBackend(
        "127.0.0.1",
        port=sftp_server.port,
        user="tester",
        password="wrong",
        known_hosts=str(sftp_server.known_hosts),
    )
    with pytest.raises(BackendError):
        b.list_dir("/")


def test_scp_mode_lists_with_ls(monkeypatch):
    b = SSHBackend("example", user="me", mode="scp")
    b.mode = "scp"
    calls = []

    def fake_run(*argv):
        calls.append(argv)
        return (
            "total 8\n"
            "drwxr-xr-x 2 me me 4096 Jan  2  2025 logs\n"
            "-rw-r--r-- 1 me me  120 Jan  2  2025 app.conf\n"
        )

    monkeypatch.setattr(b, "_run", fake_run)
    monkeypatch.setattr(b, "_ensure", lambda: None)
    entries = b.list_dir("/etc/app")
    assert [(e.name, e.is_dir, e.size) for e in entries] == [("logs", True, 0), ("app.conf", False, 120)]
    assert calls == [("ls", "-la", "/etc/app/")]


def test_scp_mode_quotes_paths(monkeypatch):
    b = SSHBackend("example", user="me", mode="scp")
    b.mode = "scp"
    sent = []

    class FakeChannel:
        def recv_exit_status(self):
            return 0

    class FakeStream(io.BytesIO):
        channel = FakeChannel()

    class FakeClient:
        def exec_command(self, cmd, timeout=None):
            sent.append(cmd)
            return None, FakeStream(b""), FakeStream(b"")

    monkeypatch.setattr(b, "_ensure", lambda: FakeClient())
    b.mkdir("/tmp/it's here; rm -rf ~")
    assert sent == ["LC_ALL=C mkdir -p -- '/tmp/it'\"'\"'s here; rm -rf ~'"]


def test_invalid_mode():
    with pytest.raises(ValueError):
        SSHBackend("example", mode="ftp")
