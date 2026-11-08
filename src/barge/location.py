# Turns whatever the user typed into a backend + path:
#
#   photos:/2024            profile from config.toml
#   web-01:/var/www         host from ~/.ssh/config
#   s3://bucket/key
#   sftp://user@host:2222/srv   (scp:// forces SCP mode, ssh:// picks automatically)
#   ./dist  or  /tmp        local

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from . import paths
from .backends import Backend, LocalBackend, S3Backend, SSHBackend
from .backends.ssh import ssh_config_hosts
from .config import ConfigError, Profile, open_profile


@dataclass
class Location:
    backend: Backend
    path: str

    def __str__(self) -> str:
        return self.backend.label + ("" if self.path == "/" else self.path)


def parse_location(spec: str, profiles: dict[str, Profile] | None = None) -> Location:
    profiles = profiles or {}
    if "://" in spec:
        return _from_url(spec)

    name, colon, rest = spec.partition(":")
    if colon and name:
        if name in profiles:
            return Location(open_profile(profiles[name]), paths.normalize(rest or "/"))
        if name in ssh_config_hosts():
            return Location(SSHBackend(name), paths.normalize(rest or "/"))
        if not os.path.exists(spec):
            raise ConfigError(f"No profile or ssh host called {name!r}")

    local = LocalBackend("/")
    return Location(local, local.to_backend_path(spec))


def _from_url(spec: str) -> Location:
    url = urlsplit(spec)
    path = paths.normalize(unquote(url.path) or "/")

    if url.scheme == "s3":
        if not url.netloc:
            raise ConfigError(f"No bucket in {spec!r}")
        return Location(S3Backend(url.netloc), path)

    if url.scheme in ("ssh", "sftp", "scp"):
        if not url.hostname:
            raise ConfigError(f"No host in {spec!r}")
        backend = SSHBackend(
            url.hostname,
            port=url.port,
            user=unquote(url.username) if url.username else None,
            password=unquote(url.password) if url.password else None,
            mode={"ssh": "auto"}.get(url.scheme, url.scheme),
        )
        return Location(backend, path)

    raise ConfigError(f"Don't know how to open {url.scheme}:// (try s3://, sftp:// or scp://)")
