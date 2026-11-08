from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .. import paths
from .base import CHUNK_SIZE, Backend, BackendError, Entry, NotFound, ProgressCallback, sort_entries


class LocalBackend(Backend):
    scheme = "local"

    def __init__(self, root: str | os.PathLike[str] = "/") -> None:
        self.root = Path(root).expanduser().resolve()

    @property
    def label(self) -> str:
        return "local" if str(self.root) == "/" else f"local:{self.root}"

    def real_path(self, path: str) -> Path:
        return self.root / paths.normalize(path).lstrip("/")

    def to_backend_path(self, real: str | os.PathLike[str]) -> str:
        resolved = Path(real).expanduser().resolve()
        try:
            rel = resolved.relative_to(self.root)
        except ValueError as e:
            raise BackendError(f"{resolved} is outside {self.root}") from e
        return paths.normalize(rel.as_posix())

    def _entry(self, path: str, st: os.stat_result, is_dir: bool) -> Entry:
        return Entry(
            path=paths.normalize(path),
            is_dir=is_dir,
            size=0 if is_dir else st.st_size,
            mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
        )

    def list_dir(self, path: str) -> list[Entry]:
        real = self.real_path(path)
        try:
            it = os.scandir(real)
        except FileNotFoundError as e:
            raise NotFound(path) from e
        except NotADirectoryError as e:
            raise BackendError(f"{path} is not a directory") from e
        except PermissionError as e:
            raise BackendError(f"Permission denied: {path}") from e

        entries = []
        with it:
            for d in it:
                try:
                    st = d.stat()
                    is_dir = d.is_dir()
                except OSError:
                    continue  # broken symlink, or deleted while we were listing
                entries.append(self._entry(paths.join(path, d.name), st, is_dir))
        return sort_entries(entries)

    def stat(self, path: str) -> Entry:
        try:
            st = self.real_path(path).stat()
        except FileNotFoundError as e:
            raise NotFound(path) from e
        return self._entry(path, st, os.path.isdir(self.real_path(path)))

    def open_read(self, path: str) -> BinaryIO:
        try:
            return open(self.real_path(path), "rb")
        except FileNotFoundError as e:
            raise NotFound(path) from e
        except IsADirectoryError as e:
            raise BackendError(f"{path} is a directory") from e

    def write(
        self,
        path: str,
        src: BinaryIO,
        *,
        size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        target = self.real_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp file next to the target, so a failed copy never leaves half a file
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK_SIZE):
                    out.write(chunk)
                    if progress:
                        progress(len(chunk))
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def delete(self, path: str) -> None:
        try:
            self.real_path(path).unlink()
        except FileNotFoundError as e:
            raise NotFound(path) from e

    def rmdir(self, path: str) -> None:
        try:
            self.real_path(path).rmdir()
        except FileNotFoundError as e:
            raise NotFound(path) from e
        except OSError as e:
            raise BackendError(f"Could not remove {path}: {e.strerror}") from e

    def delete_tree(self, path: str) -> None:
        real = self.real_path(path)
        if real == self.root:
            raise BackendError("Refusing to delete the root directory")
        try:
            shutil.rmtree(real)
        except FileNotFoundError as e:
            raise NotFound(path) from e

    def mkdir(self, path: str) -> None:
        self.real_path(path).mkdir(parents=True, exist_ok=True)

    def rename(self, src: str, dst: str) -> None:
        target = self.real_path(dst)
        if target.exists():
            raise BackendError(f"{dst} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(self.real_path(src), target)
        except FileNotFoundError as e:
            raise NotFound(src) from e
