from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Self

from .. import paths

#: Called with the number of bytes moved since the previous call.
ProgressCallback = Callable[[int], None]

CHUNK_SIZE = 1024 * 1024


class BackendError(Exception):
    # the message is shown to the user as-is, so keep it readable
    pass


class NotFound(BackendError):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    path: str
    is_dir: bool
    size: int = 0
    mtime: datetime | None = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.path) or "/"


def sort_entries(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower(), e.name))


class Backend(ABC):
    scheme: str = ""

    @property
    @abstractmethod
    def label(self) -> str: ...

    @abstractmethod
    def list_dir(self, path: str) -> list[Entry]: ...

    @abstractmethod
    def stat(self, path: str) -> Entry: ...

    @abstractmethod
    def open_read(self, path: str) -> BinaryIO: ...

    @abstractmethod
    def write(
        self,
        path: str,
        src: BinaryIO,
        *,
        size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        # must create missing parent directories
        ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def rmdir(self, path: str) -> None: ...

    @abstractmethod
    def mkdir(self, path: str) -> None:
        # like mkdir -p: parents too, and no error if it's already there
        ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...

    def walk(self, path: str) -> Iterator[Entry]:
        # everything below path, depth first. Backends override this when they can do
        # it in fewer round trips
        for entry in self.list_dir(path):
            yield entry
            if entry.is_dir:
                yield from self.walk(entry.path)

    def delete_tree(self, path: str) -> None:
        dirs: list[str] = []
        for entry in self.walk(path):
            if entry.is_dir:
                dirs.append(entry.path)
            else:
                self.delete(entry.path)
        for d in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
            self.rmdir(d)
        if paths.normalize(path) != "/":
            self.rmdir(path)

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except NotFound:
            return False
        return True

    def clone(self) -> Self:
        # transfers run on worker threads; backends with a single connection return a
        # fresh instance here
        return self

    def close(self) -> None:  # noqa: B027 - most backends have nothing to close
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
