import io
import os
import time

from barge.backends import BackendError, LocalBackend
from barge.transfer import Conflict, Job, State, TransferQueue, copy_one, plan


def make(tmp_path, name, files):
    root = tmp_path / name
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    root.mkdir(exist_ok=True)
    return LocalBackend(root)


def test_plan_file_and_folder(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"a", "dir/b.txt": b"bb", "dir/sub/c.txt": b"c"})
    dst = make(tmp_path, "dst", {})
    assert [(j.src_path, j.dst_path) for j in plan(src, "/a.txt", dst, "/in")] == [("/a.txt", "/in/a.txt")]
    folder = plan(src, "/dir", dst, "/")
    assert sorted(j.dst_path for j in folder) == ["/dir/b.txt", "/dir/sub/c.txt"]
    assert sum(j.size for j in folder) == 3


def test_queue_copies_local_to_s3_and_sftp(tmp_path, s3, sftp):
    src = make(tmp_path, "src", {"site/index.html": b"<h1>", "site/css/app.css": b"body{}"})
    seen = []
    q = TransferQueue(workers=2, on_change=lambda j: seen.append(j.state))
    jobs = q.submit(plan(src, "/site", s3, "/www") + plan(src, "/site", sftp, "/"))
    assert q.wait(10)
    assert all(j.state is State.DONE for j in jobs)
    assert s3.open_read("/www/site/css/app.css").read() == b"body{}"
    assert (sftp.disk / "site/index.html").read_bytes() == b"<h1>"
    assert State.RUNNING in seen
    q.shutdown()


def test_conflict_skip_and_newer(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"new", "b.txt": b"new"})
    dst = make(tmp_path, "dst", {"a.txt": b"old", "b.txt": b"old"})
    now = time.time()
    os.utime(tmp_path / "src/a.txt", (now - 100, now - 100))  # older than dst
    os.utime(tmp_path / "dst/a.txt", (now, now))

    assert copy_one(Job(src, "/a.txt", dst, "/a.txt"), Conflict.SKIP) is State.SKIPPED
    assert copy_one(Job(src, "/a.txt", dst, "/a.txt"), Conflict.NEWER) is State.SKIPPED
    os.utime(tmp_path / "dst/b.txt", (now - 100, now - 100))
    assert copy_one(Job(src, "/b.txt", dst, "/b.txt"), Conflict.NEWER) is State.DONE
    assert (tmp_path / "dst/b.txt").read_bytes() == b"new"
    assert (tmp_path / "dst/a.txt").read_bytes() == b"old"


def test_move_deletes_source(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"a"})
    dst = make(tmp_path, "dst", {})
    assert copy_one(Job(src, "/a.txt", dst, "/a.txt", move=True)) is State.DONE
    assert not (tmp_path / "src/a.txt").exists()


class Flaky(LocalBackend):
    def __init__(self, root, failures):
        super().__init__(root)
        self.failures = failures

    def write(self, path, src, **kw):
        if self.failures:
            self.failures -= 1
            raise BackendError("connection reset")
        return super().write(path, src, **kw)


def test_retries_then_succeeds(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"a"})
    dst = Flaky(tmp_path / "src", failures=2)
    (tmp_path / "out").mkdir()
    dst.root = tmp_path / "out"
    q = TransferQueue(workers=1, retries=2, retry_delay=0)
    [job] = q.submit([Job(src, "/a.txt", dst, "/a.txt", 1)])
    q.wait(5)
    assert job.state is State.DONE and job.attempts == 3
    q.shutdown()


def test_gives_up_after_retries(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"a"})
    dst = Flaky(tmp_path / "src", failures=10)
    q = TransferQueue(workers=1, retries=1, retry_delay=0)
    [job] = q.submit([Job(src, "/a.txt", dst, "/x.txt", 1)])
    q.wait(5)
    assert job.state is State.FAILED and "connection reset" in job.error
    q.shutdown()


class Slow(LocalBackend):
    def write(self, path, src, *, size=None, progress=None):
        for _ in range(200):
            time.sleep(0.01)
            progress(1)


def test_cancel_stops_running_job(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"a"})
    q = TransferQueue(workers=1)
    [job] = q.submit([Job(src, "/a.txt", Slow(tmp_path), "/b.txt", 200)])
    time.sleep(0.1)
    q.cancel()
    assert q.wait(3)
    assert job.state is State.CANCELLED
    q.shutdown()


def test_stats(tmp_path):
    src = make(tmp_path, "src", {"a.txt": b"aaaa"})
    dst = make(tmp_path, "dst", {})
    q = TransferQueue()
    q.submit([Job(src, "/a.txt", dst, "/a.txt", 4)])
    q.wait(5)
    assert q.stats()["done"] == 1
    q.clear_finished()
    assert q.jobs == []
    q.shutdown()


def test_unexpected_error_marks_job_failed(tmp_path):
    class Broken(LocalBackend):
        def open_read(self, path):
            raise RuntimeError("bug")

    q = TransferQueue(workers=1)
    [job] = q.submit([Job(Broken(tmp_path), "/a", LocalBackend(tmp_path), "/b", 1)])
    q.wait(5)
    assert job.state is State.FAILED and "RuntimeError" in job.error
    q.shutdown()


def test_progress_reaches_size(tmp_path):
    src = make(tmp_path, "src", {"big.bin": os.urandom(3 * 1024 * 1024)})
    dst = make(tmp_path, "dst", {})
    q = TransferQueue()
    [job] = q.submit(plan(src, "/big.bin", dst, "/"))
    q.wait(10)
    assert job.done_bytes == 3 * 1024 * 1024 and job.fraction == 1.0
    q.shutdown()


def test_bytes_io_source(tmp_path):
    dst = make(tmp_path, "dst", {})
    dst.write("/x", io.BytesIO(b"abc"))
    assert (tmp_path / "dst/x").read_bytes() == b"abc"
