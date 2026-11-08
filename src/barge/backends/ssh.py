from __future__ import annotations

import io
import posixpath
import shlex
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Self

import paramiko
from scp import SCPClient, SCPException

from .. import paths
from . import lsparse
from .base import Backend, BackendError, Entry, NotFound, ProgressCallback, sort_entries

SSH_CONFIG = Path("~/.ssh/config").expanduser()


class SSHBackend(Backend):
    """SFTP when the server offers it, otherwise SCP plus shell commands."""

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        key_file: str | None = None,
        root: str = "/",
        mode: str = "auto",
        known_hosts: str | None = None,
        trust_new_hosts: bool = False,
        timeout: float = 15,
    ) -> None:
        if mode not in ("auto", "sftp", "scp"):
            raise ValueError(f"mode must be auto, sftp or scp, not {mode!r}")
        self.alias = host
        self._opts = dict(
            port=port,
            user=user,
            password=password,
            key_file=key_file,
            root=root,
            mode=mode,
            known_hosts=known_hosts,
            trust_new_hosts=trust_new_hosts,
            timeout=timeout,
        )

        cfg = _ssh_config(host)
        self.hostname = cfg.get("hostname", host)
        self.port = port or int(cfg.get("port", 22))
        self.user = user or cfg.get("user") or _local_user()
        self.key_files = [key_file] if key_file else cfg.get("identityfile", [])
        self.password = password
        self.root = paths.normalize(root)
        self.requested_mode = mode
        self.mode = mode if mode != "auto" else None
        self.known_hosts = known_hosts
        self.trust_new_hosts = trust_new_hosts
        self.timeout = timeout

        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._lock = threading.RLock()

    @property
    def scheme(self) -> str:  # type: ignore[override]
        return self.mode or "ssh"

    @property
    def label(self) -> str:
        root = "" if self.root == "/" else self.root
        return f"{self.scheme}://{self.user}@{self.alias}{root}"

    def clone(self) -> Self:
        other = type(self)(self.alias, **self._opts)  # type: ignore[arg-type]
        other.mode = self.mode  # don't probe for SFTP again
        return other

    # connection

    def _connect(self) -> None:
        client = paramiko.SSHClient()
        if self.known_hosts:
            client.load_host_keys(self.known_hosts)
        else:
            client.load_system_host_keys()
        if self.trust_new_hosts:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        try:
            client.connect(
                self.hostname,
                port=self.port,
                username=self.user,
                password=self.password,
                key_filename=[str(Path(k).expanduser()) for k in self.key_files] or None,
                timeout=self.timeout,
                allow_agent=True,
                look_for_keys=not self.key_files,
            )
        except paramiko.BadHostKeyException as e:
            raise BackendError(f"Host key for {self.hostname} has CHANGED. Refusing to connect.") from e
        except paramiko.SSHException as e:
            if "not found in known_hosts" in str(e):
                raise BackendError(
                    f"{self.hostname} isn't in known_hosts. Connect once with ssh to check "
                    "its fingerprint, or set trust_new_hosts for this profile."
                ) from e
            raise BackendError(f"SSH to {self.hostname} failed: {e}") from e
        except OSError as e:
            raise BackendError(f"Couldn't reach {self.hostname}:{self.port}: {e}") from e

        if self.mode in (None, "sftp"):
            try:
                self._sftp = client.open_sftp()
                self.mode = "sftp"
            except paramiko.SSHException as e:
                if self.mode == "sftp":
                    client.close()
                    raise BackendError(f"{self.hostname} doesn't offer SFTP: {e}") from e
                self.mode = "scp"
        self._client = client

    def _ensure(self) -> paramiko.SSHClient:
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            self.close()
        self._connect()
        assert self._client is not None
        return self._client

    def _sftp_client(self) -> paramiko.SFTPClient | None:
        self._ensure()
        return self._sftp if self.mode == "sftp" else None

    def close(self) -> None:
        with self._lock:
            if self._sftp is not None:
                self._sftp.close()
                self._sftp = None
            if self._client is not None:
                self._client.close()
                self._client = None

    def _real(self, path: str) -> str:
        return paths.join(self.root, path)

    def _run(self, *argv: str) -> str:
        cmd = " ".join(shlex.quote(a) for a in argv)
        client = self._ensure()
        _, stdout, stderr = client.exec_command(f"LC_ALL=C {cmd}", timeout=self.timeout)
        out = stdout.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code != 0:
            err = stderr.read().decode("utf-8", "replace").strip()
            if "No such file" in err:
                raise NotFound(argv[-1])
            raise BackendError(f"{argv[0]} failed on {self.alias}: {err or f'exit {code}'}")
        return out

    def _sftp_call(self, path: str, fn, *args):
        try:
            return fn(*args)
        except FileNotFoundError as e:
            raise NotFound(path) from e
        except PermissionError as e:
            raise BackendError(f"Permission denied: {path}") from e
        except OSError as e:
            raise BackendError(f"{path}: {e}") from e
        except paramiko.SSHException as e:
            self.close()
            raise BackendError(f"SSH connection to {self.alias} dropped: {e}") from e

    # listing

    def list_dir(self, path: str) -> list[Entry]:
        with self._lock:
            sftp = self._sftp_client()
            real = self._real(path)
            if sftp:
                attrs = self._sftp_call(path, sftp.listdir_attr, real)
                entries = [self._from_attr(paths.join(path, a.filename), a) for a in attrs]
            else:
                out = self._run("ls", "-la", real + "/")
                entries = [
                    Entry(paths.join(path, i.name), i.is_dir, 0 if i.is_dir else i.size, i.mtime)
                    for i in lsparse.parse(out)
                ]
        return sort_entries(entries)

    def stat(self, path: str) -> Entry:
        path = paths.normalize(path)
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                return self._from_attr(path, self._sftp_call(path, sftp.stat, self._real(path)))
            out = self._run("ls", "-lad", self._real(path))
        items = lsparse.parse(out)
        if not items:
            raise NotFound(path)
        i = items[0]
        return Entry(path, i.is_dir, 0 if i.is_dir else i.size, i.mtime)

    @staticmethod
    def _from_attr(path: str, a: paramiko.SFTPAttributes) -> Entry:
        is_dir = stat.S_ISDIR(a.st_mode or 0)
        mtime = datetime.fromtimestamp(a.st_mtime, tz=UTC) if a.st_mtime else None
        return Entry(path, is_dir, 0 if is_dir else (a.st_size or 0), mtime)

    # data

    def open_read(self, path: str) -> BinaryIO:
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                f = self._sftp_call(path, sftp.open, self._real(path), "rb")
                f.prefetch()
                return f  # type: ignore[return-value]
            buf = io.BytesIO()
            try:
                with SCPClient(self._ensure().get_transport()) as scp:
                    scp.getfo(self._real(path), buf)
            except SCPException as e:
                if "No such file" in str(e):
                    raise NotFound(path) from e
                raise BackendError(f"scp failed: {e}") from e
            buf.seek(0)
            return buf

    def write(
        self,
        path: str,
        src: BinaryIO,
        *,
        size: int | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.mkdir(paths.parent(path))
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                sent = 0

                def cb(done: int, _total: int) -> None:
                    nonlocal sent
                    if progress:
                        progress(done - sent)
                    sent = done

                self._sftp_call(path, sftp.putfo, src, self._real(path), size or 0, cb, False)
                return

            last = 0

            def scp_cb(_name: bytes, _size: int, done: int) -> None:
                nonlocal last
                if progress:
                    progress(done - last)
                last = done

            if size is None:
                data = src.read()
                src, size = io.BytesIO(data), len(data)
            try:
                with SCPClient(self._ensure().get_transport(), progress=scp_cb) as scp:
                    scp.putfo(src, self._real(path), size=size)
            except SCPException as e:
                raise BackendError(f"scp failed: {e}") from e

    # changes

    def delete(self, path: str) -> None:
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                self._sftp_call(path, sftp.remove, self._real(path))
            else:
                self.stat(path)
                self._run("rm", "-f", "--", self._real(path))

    def rmdir(self, path: str) -> None:
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                self._sftp_call(path, sftp.rmdir, self._real(path))
            else:
                self._run("rmdir", "--", self._real(path))

    def delete_tree(self, path: str) -> None:
        if paths.normalize(path) == "/":
            raise BackendError("Refusing to delete the root directory")
        if self.mode == "scp" or (self._sftp_client() is None):
            self.stat(path)
            self._run("rm", "-rf", "--", self._real(path))
            return
        super().delete_tree(path)

    def mkdir(self, path: str) -> None:
        path = paths.normalize(path)
        if path == "/":
            return
        with self._lock:
            sftp = self._sftp_client()
            if not sftp:
                self._run("mkdir", "-p", "--", self._real(path))
                return
            current = "/"
            for part in path.strip("/").split("/"):
                current = posixpath.join(current, part)
                try:
                    sftp.stat(self._real(current))
                except FileNotFoundError:
                    self._sftp_call(current, sftp.mkdir, self._real(current))

    def rename(self, src: str, dst: str) -> None:
        if self.exists(dst):
            raise BackendError(f"{dst} already exists")
        self.mkdir(paths.parent(dst))
        with self._lock:
            sftp = self._sftp_client()
            if sftp:
                self._sftp_call(src, sftp.rename, self._real(src), self._real(dst))
            else:
                self._run("mv", "-n", "--", self._real(src), self._real(dst))


def _ssh_config(host: str) -> dict:
    if not SSH_CONFIG.exists():
        return {}
    try:
        return paramiko.SSHConfig.from_path(str(SSH_CONFIG)).lookup(host)
    except (OSError, paramiko.SSHException):
        return {}


def ssh_config_hosts() -> list[str]:
    """Concrete Host aliases from ~/.ssh/config (wildcards skipped)."""
    if not SSH_CONFIG.exists():
        return []
    try:
        names = paramiko.SSHConfig.from_path(str(SSH_CONFIG)).get_hostnames()
    except (OSError, paramiko.SSHException):
        return []
    return sorted(n for n in names if not any(c in n for c in "*?!"))


def _local_user() -> str:
    import getpass

    return getpass.getuser()
