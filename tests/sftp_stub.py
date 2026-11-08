# A tiny in-process SFTP server for tests: paramiko on a random port, serving a
# temp directory, password auth only.

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import paramiko
from paramiko import SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface
from paramiko.sftp import SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK, SFTP_PERMISSION_DENIED

USER = "tester"
PASSWORD = "hunter2"


def _err(e: OSError) -> int:
    if isinstance(e, FileNotFoundError):
        return SFTP_NO_SUCH_FILE
    if isinstance(e, PermissionError):
        return SFTP_PERMISSION_DENIED
    return SFTP_FAILURE


class _Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        if username == USER and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return _err(e)


def make_sftp_interface(root: Path):
    class _SFTP(SFTPServerInterface):
        def _real(self, path):
            return str(root / self.canonicalize(path).lstrip("/"))

        def list_folder(self, path):
            real = self._real(path)
            try:
                out = []
                for name in os.listdir(real):
                    attr = SFTPAttributes.from_stat(os.stat(os.path.join(real, name)))
                    attr.filename = name
                    out.append(attr)
                return out
            except OSError as e:
                return _err(e)

        def stat(self, path):
            try:
                return SFTPAttributes.from_stat(os.stat(self._real(path)))
            except OSError as e:
                return _err(e)

        lstat = stat

        def open(self, path, flags, attr):
            real = self._real(path)
            try:
                fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o644)
            except OSError as e:
                return _err(e)
            mode = "wb" if flags & os.O_WRONLY else ("r+b" if flags & os.O_RDWR else "rb")
            if flags & os.O_APPEND:
                mode = "ab"
            f = os.fdopen(fd, mode)
            h = _Handle(flags)
            h.filename = real
            h.readfile = f
            h.writefile = f
            return h

        def remove(self, path):
            try:
                os.remove(self._real(path))
            except OSError as e:
                return _err(e)
            return SFTP_OK

        def rename(self, old, new):
            try:
                os.rename(self._real(old), self._real(new))
            except OSError as e:
                return _err(e)
            return SFTP_OK

        posix_rename = rename

        def mkdir(self, path, attr):
            try:
                os.mkdir(self._real(path))
            except OSError as e:
                return _err(e)
            return SFTP_OK

        def rmdir(self, path):
            try:
                os.rmdir(self._real(path))
            except OSError as e:
                return _err(e)
            return SFTP_OK

        def chattr(self, path, attr):
            return SFTP_OK

    return _SFTP


class StubSFTPServer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.host_key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._transports: list[paramiko.Transport] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def write_known_hosts(self, path: Path) -> Path:
        hk = paramiko.HostKeys()
        hk.add(f"[127.0.0.1]:{self.port}", self.host_key.get_name(), self.host_key)
        hk.save(str(path))
        return path

    def _accept(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            t.set_subsystem_handler("sftp", SFTPServer, make_sftp_interface(self.root))
            t.start_server(server=_Server())
            self._transports.append(t)

    def close(self) -> None:
        self._stop.set()
        for t in self._transports:
            t.close()
        self.sock.close()
