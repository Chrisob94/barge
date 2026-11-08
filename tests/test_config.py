import pytest

from barge.backends import LocalBackend, S3Backend, SSHBackend
from barge.config import ConfigError, load_profiles, open_profile
from barge.location import parse_location

CONFIG = """
[profiles.photos]
type = "s3"
bucket = "my-photos"
prefix = "originals"
region = "eu-west-1"

[profiles.web-01]
type = "ssh"
host = "web-01.example.net"
user = "deploy"
root = "/var/www"
password_env = "WEB_PASS"

[profiles.scratch]
type = "local"
root = "/tmp"
"""


@pytest.fixture
def profiles(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return load_profiles(path)


def test_load_profiles(profiles):
    assert set(profiles) == {"photos", "web-01", "scratch"}
    assert profiles["photos"].describe() == "s3://my-photos/originals"
    assert profiles["web-01"].describe() == "deploy@web-01.example.net:/var/www"


def test_open_profiles(profiles, monkeypatch):
    assert isinstance(open_profile(profiles["scratch"]), LocalBackend)
    s3 = open_profile(profiles["photos"])
    assert isinstance(s3, S3Backend) and s3.prefix == "originals"
    with pytest.raises(ConfigError, match="WEB_PASS"):
        open_profile(profiles["web-01"])
    monkeypatch.setenv("WEB_PASS", "pw")
    ssh = open_profile(profiles["web-01"])
    assert isinstance(ssh, SSHBackend) and ssh.root == "/var/www" and ssh.password == "pw"


@pytest.mark.parametrize(
    "toml, message",
    [
        ('[profiles.x]\ntype = "gopher"', "unknown type"),
        ('[profiles.x]\ntype = "s3"', "missing 'bucket'"),
        ('[profiles.x]\ntype = "s3"\nbucket = "b"\ncolour = "red"', "unknown keys: colour"),
        ("[profiles\n", "config.toml"),
    ],
)
def test_bad_config(tmp_path, toml, message):
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(ConfigError, match=message):
        load_profiles(path)


def test_missing_config_is_fine(tmp_path):
    assert load_profiles(tmp_path / "nope.toml") == {}


def test_parse_location(profiles, tmp_path):
    loc = parse_location("photos:/2024", profiles)
    assert isinstance(loc.backend, S3Backend) and loc.path == "/2024"

    loc = parse_location("s3://bucket/a/b.txt")
    assert loc.backend.bucket == "bucket" and loc.path == "/a/b.txt"

    loc = parse_location("sftp://me:secret@host.example:2222/srv")
    assert isinstance(loc.backend, SSHBackend)
    assert (loc.backend.user, loc.backend.port, loc.backend.password, loc.path) == (
        "me",
        2222,
        "secret",
        "/srv",
    )
    assert parse_location("scp://host/x").backend.requested_mode == "scp"

    loc = parse_location(str(tmp_path))
    assert isinstance(loc.backend, LocalBackend) and loc.path == str(tmp_path.resolve())


def test_parse_location_errors(profiles):
    with pytest.raises(ConfigError, match="nope"):
        parse_location("nope:/x", profiles)
    with pytest.raises(ConfigError, match="ftp"):
        parse_location("ftp://example.com/")
