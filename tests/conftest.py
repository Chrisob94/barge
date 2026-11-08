from __future__ import annotations

import pytest

from barge.backends import S3Backend, SSHBackend


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # never read the developer's real config, state or AWS credentials
    monkeypatch.setenv("BARGE_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr("barge.backends.ssh.SSH_CONFIG", tmp_path / "no-ssh-config")


@pytest.fixture
def s3():
    from moto import mock_aws

    with mock_aws():
        backend = S3Backend("test-bucket", region="us-east-1")
        backend.client.create_bucket(Bucket="test-bucket")
        yield backend


@pytest.fixture(scope="session")
def sftp_server(tmp_path_factory):
    from .sftp_stub import StubSFTPServer

    server = StubSFTPServer(tmp_path_factory.mktemp("sftp-root"))
    server.known_hosts = server.write_known_hosts(tmp_path_factory.mktemp("ssh") / "known_hosts")
    yield server
    server.close()


@pytest.fixture
def sftp(sftp_server, tmp_path):
    from .sftp_stub import PASSWORD, USER

    # a fresh folder per test inside the shared server root
    root = sftp_server.root / tmp_path.name
    root.mkdir()
    backend = SSHBackend(
        "127.0.0.1",
        port=sftp_server.port,
        user=USER,
        password=PASSWORD,
        root=f"/{tmp_path.name}",
        known_hosts=str(sftp_server.known_hosts),
    )
    backend.disk = root  # the real folder behind it, for assertions
    yield backend
    backend.close()
