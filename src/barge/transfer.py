from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum

from . import paths
from .backends import Backend, BackendError, NotFound


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Conflict(StrEnum):
    OVERWRITE = "overwrite"
    SKIP = "skip"
    NEWER = "newer"  # only replace if the source is newer


class Cancelled(Exception):
    pass


_ids = itertools.count(1)


@dataclass(eq=False)
class Job:
    src: Backend
    src_path: str
    dst: Backend
    dst_path: str
    size: int | None = None
    move: bool = False
    id: int = field(default_factory=lambda: next(_ids))
    state: State = State.QUEUED
    done_bytes: int = 0
    attempts: int = 0
    error: str | None = None
    started: float | None = None
    finished: float | None = None

    @property
    def name(self) -> str:
        return paths.basename(self.src_path)

    @property
    def fraction(self) -> float:
        if self.state in (State.DONE, State.SKIPPED):
            return 1.0
        if not self.size:
            return 0.0
        return min(self.done_bytes / self.size, 1.0)

    @property
    def finished_ok(self) -> bool:
        return self.state in (State.DONE, State.SKIPPED)


def plan(src: Backend, src_path: str, dst: Backend, dst_dir: str, *, move: bool = False) -> list[Job]:
    """Jobs to copy src_path (file or folder) into the folder dst_dir."""
    entry = src.stat(src_path)
    target = paths.join(dst_dir, entry.name)
    if not entry.is_dir:
        return [Job(src, entry.path, dst, target, entry.size, move)]

    jobs = []
    for child in src.walk(entry.path):
        if child.is_dir:
            continue
        rel = paths.relative_to(child.path, entry.path)
        jobs.append(Job(src, child.path, dst, paths.join(target, rel), child.size, move))
    return jobs


def copy_one(job: Job, conflict: Conflict = Conflict.OVERWRITE, progress=None) -> State:
    if conflict is not Conflict.OVERWRITE:
        try:
            existing = job.dst.stat(job.dst_path)
        except NotFound:
            existing = None
        if existing is not None:
            if conflict is Conflict.SKIP:
                return State.SKIPPED
            src_entry = job.src.stat(job.src_path)
            if src_entry.mtime and existing.mtime and src_entry.mtime <= existing.mtime:
                return State.SKIPPED

    with closing(job.src.open_read(job.src_path)) as f:
        job.dst.write(job.dst_path, f, size=job.size, progress=progress)
    if job.move:
        job.src.delete(job.src_path)
    return State.DONE


class TransferQueue:
    """Runs jobs on a small thread pool. on_change is called from worker threads."""

    def __init__(
        self,
        workers: int = 3,
        retries: int = 2,
        on_change: Callable[[Job], None] | None = None,
        retry_delay: float = 1.0,
    ) -> None:
        self.retries = retries
        self.retry_delay = retry_delay
        self.on_change = on_change or (lambda job: None)
        self.jobs: list[Job] = []
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="barge-xfer")
        self._local = threading.local()
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0

    def submit(self, jobs: Iterable[Job], conflict: Conflict = Conflict.OVERWRITE) -> list[Job]:
        jobs = list(jobs)
        with self._lock:
            self.jobs.extend(jobs)
            self._pending += len(jobs)
            if jobs:
                self._idle.clear()
        for job in jobs:
            self.on_change(job)
            self._pool.submit(self._run, job, conflict)
        return jobs

    def cancel(self, job_id: int | None = None) -> None:
        with self._lock:
            if job_id is None:
                self._cancelled.update(j.id for j in self.jobs if not j.finished_ok)
            else:
                self._cancelled.add(job_id)

    def wait(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def clear_finished(self) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.state in (State.QUEUED, State.RUNNING)]

    def shutdown(self) -> None:
        self.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stats(self) -> dict[str, float]:
        with self._lock:
            jobs = list(self.jobs)
        running = [j for j in jobs if j.state is State.RUNNING]
        speed = 0.0
        for j in running:
            if j.started and j.done_bytes:
                speed += j.done_bytes / max(time.monotonic() - j.started, 0.001)
        left = sum((j.size or 0) - j.done_bytes for j in jobs if j.state in (State.QUEUED, State.RUNNING))
        return {
            "running": len(running),
            "queued": sum(j.state is State.QUEUED for j in jobs),
            "done": sum(j.finished_ok for j in jobs),
            "failed": sum(j.state is State.FAILED for j in jobs),
            "speed": speed,
            "eta": left / speed if speed else 0.0,
        }

    def _backend(self, b: Backend) -> Backend:
        # one clone per backend per worker thread, so SSH sessions aren't shared
        cache = self._local.__dict__.setdefault("clones", {})
        if id(b) not in cache:
            cache[id(b)] = b.clone()
        return cache[id(b)]

    def _run(self, job: Job, conflict: Conflict) -> None:
        try:
            if job.id in self._cancelled:
                job.state = State.CANCELLED
                return

            work = Job(
                self._backend(job.src),
                job.src_path,
                self._backend(job.dst),
                job.dst_path,
                job.size,
                job.move,
                id=job.id,
            )

            def progress(n: int) -> None:
                if job.id in self._cancelled:
                    raise Cancelled
                job.done_bytes += n
                self.on_change(job)

            while True:
                job.attempts += 1
                job.state = State.RUNNING
                job.started = job.started or time.monotonic()
                job.done_bytes = 0
                self.on_change(job)
                try:
                    job.state = copy_one(work, conflict, progress)
                    job.error = None
                    break
                except Cancelled:
                    job.state = State.CANCELLED
                    break
                except NotFound as e:
                    job.state, job.error = State.FAILED, f"not found: {e}"
                    break
                except (BackendError, OSError) as e:
                    job.error = str(e)
                    if job.attempts > self.retries or job.id in self._cancelled:
                        job.state = State.FAILED
                        break
                    time.sleep(self.retry_delay * job.attempts)
        except Exception as e:  # never let a worker die silently
            job.state, job.error = State.FAILED, f"{type(e).__name__}: {e}"
        finally:
            job.finished = time.monotonic()
            self.on_change(job)
            with self._lock:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.set()
