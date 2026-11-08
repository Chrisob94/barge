# Every backend speaks the same kind of path: absolute, "/"-separated, and no trailing
# slash except for the root itself. e.g. "/photos/2024"

from __future__ import annotations

import posixpath


def normalize(path: str) -> str:
    path = path.replace("\\", "/")
    normalized = posixpath.normpath("/" + path.lstrip("/"))
    # normpath keeps a leading "//" (POSIX allows it to mean something special)
    return "/" + normalized.lstrip("/")


def join(base: str, *parts: str) -> str:
    return normalize(posixpath.join(normalize(base), *(p.lstrip("/") for p in parts)))


def parent(path: str) -> str:
    return posixpath.dirname(normalize(path))


def basename(path: str) -> str:
    return posixpath.basename(normalize(path))


def relative_to(path: str, base: str) -> str:
    # "/a/b/c.txt" relative to "/a" -> "b/c.txt"
    path, base = normalize(path), normalize(base)
    if base == "/":
        return path.lstrip("/")
    if path == base:
        return ""
    if not path.startswith(base + "/"):
        raise ValueError(f"{path!r} is not inside {base!r}")
    return path[len(base) + 1 :]


def is_inside(path: str, base: str) -> bool:
    try:
        relative_to(path, base)
    except ValueError:
        return False
    return True
