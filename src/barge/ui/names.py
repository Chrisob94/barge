from __future__ import annotations

from pathlib import Path

from ..backends import Backend

DARK_COLORS = {"local": "#5DCAA5", "s3": "#97C459", "ssh": "#AFA9EC"}
LIGHT_COLORS = {"local": "#0F6E56", "s3": "#3B6D11", "ssh": "#534AB7"}


def short_name(backend: Backend) -> str:
    if backend.scheme == "local":
        return "local"
    if backend.scheme == "s3":
        return backend.label.removeprefix("s3://").split("/")[0]
    return getattr(backend, "alias", backend.label)


def where(backend: Backend, path: str) -> str:
    """Compact "where is this" text: ~/projects, web-01:/var/www, s3://bucket/key."""
    if backend.scheme == "local":
        real = str(getattr(backend, "real_path", lambda p: Path(p))(path))
        home = str(Path.home())
        return "~" + real[len(home) :] if real == home or real.startswith(home + "/") else real
    if backend.scheme == "s3":
        return backend.label + ("" if path == "/" else path)
    return f"{short_name(backend)}:{path}"


def color(backend: Backend, dark: bool = True) -> str:
    colors = DARK_COLORS if dark else LIGHT_COLORS
    return colors.get(backend.scheme, colors["ssh"])
