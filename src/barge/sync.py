from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from . import paths
from .backends import Backend, Entry, NotFound
from .transfer import Job

# FAT and some servers only keep 2 second mtime precision
MTIME_SLACK = timedelta(seconds=2)


class Action(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    DELETE = "delete"


@dataclass(frozen=True)
class Step:
    action: Action
    rel: str
    size: int
    reason: str = ""


def index(backend: Backend, root: str) -> dict[str, Entry]:
    try:
        return {paths.relative_to(e.path, root): e for e in backend.walk(root) if not e.is_dir}
    except NotFound:
        return {}


def diff(
    src: dict[str, Entry], dst: dict[str, Entry], *, delete: bool = False, use_mtime: bool = True
) -> list[Step]:
    steps = []
    for rel in sorted(src):
        s = src[rel]
        d = dst.get(rel)
        if d is None:
            steps.append(Step(Action.NEW, rel, s.size))
        elif s.size != d.size:
            steps.append(Step(Action.CHANGED, rel, s.size, f"{d.size} → {s.size} bytes"))
        elif use_mtime and s.mtime and d.mtime and s.mtime - d.mtime > MTIME_SLACK:
            steps.append(Step(Action.CHANGED, rel, s.size, "newer in source"))
    if delete:
        for rel in sorted(dst.keys() - src.keys()):
            steps.append(Step(Action.DELETE, rel, dst[rel].size, "not in source"))
    return steps


def unchanged_count(src: dict[str, Entry], steps: list[Step]) -> int:
    touched = {s.rel for s in steps if s.action is not Action.DELETE}
    return len(src) - len(touched)


def jobs_for(steps: list[Step], src: Backend, src_root: str, dst: Backend, dst_root: str) -> list[Job]:
    return [
        Job(src, paths.join(src_root, s.rel), dst, paths.join(dst_root, s.rel), s.size)
        for s in steps
        if s.action is not Action.DELETE
    ]


def apply_deletes(steps: list[Step], dst: Backend, dst_root: str) -> list[str]:
    errors = []
    for s in steps:
        if s.action is Action.DELETE:
            try:
                dst.delete(paths.join(dst_root, s.rel))
            except NotFound:
                pass
            except Exception as e:  # report and keep going
                errors.append(f"{s.rel}: {e}")
    return errors
