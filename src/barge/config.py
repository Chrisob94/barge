# Connection profiles live in ~/.config/barge/config.toml, e.g.
#
#   [profiles.photos]
#   type = "s3"
#   bucket = "my-photos"
#   prefix = "originals"
#   region = "eu-west-1"
#
#   [profiles.web-01]
#   type = "ssh"
#   host = "web-01"            # anything in ~/.ssh/config works
#   root = "/var/www"
#
# Hosts from ~/.ssh/config show up in the palette without needing a profile.

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backends import Backend, LocalBackend, S3Backend, SSHBackend

DEFAULT_PATH = Path("~/.config/barge/config.toml")

KEYS: dict[str, set[str]] = {
    "local": {"root"},
    "s3": {"bucket", "prefix", "region", "endpoint_url", "aws_profile"},
    "ssh": {
        "host",
        "port",
        "user",
        "key_file",
        "password_env",
        "root",
        "mode",
        "trust_new_hosts",
        "timeout",
    },
}
REQUIRED = {"s3": "bucket", "ssh": "host"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    type: str
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        o = self.options
        if self.type == "s3":
            return f"s3://{o['bucket']}/{o.get('prefix', '')}".rstrip("/")
        if self.type == "ssh":
            user = f"{o['user']}@" if "user" in o else ""
            return f"{user}{o['host']}:{o.get('root', '~')}"
        return o.get("root", "/")


def config_path() -> Path:
    return Path(os.environ.get("BARGE_CONFIG", DEFAULT_PATH)).expanduser()


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    profiles = {}
    for name, raw in data.get("profiles", {}).items():
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: [profiles.{name}] should be a table")
        opts = dict(raw)
        kind = opts.pop("type", None)
        if kind not in KEYS:
            raise ConfigError(f"{path}: profile {name!r} has unknown type {kind!r}")
        if unknown := set(opts) - KEYS[kind]:
            raise ConfigError(f"{path}: profile {name!r} has unknown keys: {', '.join(sorted(unknown))}")
        if (req := REQUIRED.get(kind)) and req not in opts:
            raise ConfigError(f"{path}: profile {name!r} is missing {req!r}")
        profiles[name] = Profile(name, kind, opts)
    return profiles


def open_profile(profile: Profile) -> Backend:
    o = profile.options
    if profile.type == "local":
        return LocalBackend(o.get("root", "/"))
    if profile.type == "s3":
        return S3Backend(
            o["bucket"],
            prefix=o.get("prefix", ""),
            region=o.get("region"),
            endpoint_url=o.get("endpoint_url"),
            aws_profile=o.get("aws_profile"),
        )

    password = None
    if env := o.get("password_env"):
        if env not in os.environ:
            raise ConfigError(f"profile {profile.name!r}: ${env} isn't set")
        password = os.environ[env]
    return SSHBackend(
        o["host"],
        port=o.get("port"),
        user=o.get("user"),
        password=password,
        key_file=o.get("key_file"),
        root=o.get("root", "/"),
        mode=o.get("mode", "auto"),
        trust_new_hosts=bool(o.get("trust_new_hosts", False)),
        timeout=float(o.get("timeout", 15)),
    )
