"""The Soulseek contract: the daemon's environment, and its control socket."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import slsk

from fake_server import Fake

ONE = [{"username": "peer", "filename": "Music\\Artist - Title.flac", "filesize": 100,
        "bitrate": 855, "duration_sec": 449, "has_free_slots": True, "avg_speed": 500_000}]


def make_config(tmp_path: Path, credentials: str = "", state_dir: Path | None = None) -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(
        '[paths]\nstaging = "staging"\nlossless = "lossless"\nlossy = "lossy"\nstate = "state"\n'
        f'[soulseek]\nstate_dir = "{state_dir or tmp_path / "daemon"}"\n' + credentials,
        encoding="utf-8",
    )
    return config_module.load(path)


def short_dir() -> Path:
    """A directory short enough for `AF_UNIX`, whose paths are capped at about a hundred bytes on macOS."""
    return Path(tempfile.mkdtemp(prefix="slsk-"))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("SLSK_USERNAME", "someone")
    monkeypatch.setenv("SLSK_PASSWORD", "a secret")


def test_the_environment_carries_what_the_daemon_reads(tmp_path, creds, monkeypatch):
    """The credentials under the names the daemon looks for, the state directory both ends derive the socket
    from, and the one port the account registers."""
    monkeypatch.setenv("SLSK_LISTEN_PORT", "41000")
    config = make_config(tmp_path)
    env = slsk.environment(config)

    assert env["SOULSEEK_USERNAME"] == "someone"
    assert env["SOULSEEK_PASSWORD"] == "a secret"
    assert env["SOULSEEK_STATE_DIR"] == str(tmp_path / "daemon")
    assert env["SOULSEEK_LISTENER_PORT"] == "41000"
    assert env["SOULSEEK_MAX_CONCURRENT_DOWNLOADS"] == str(config.max_downloads)


def test_credentials_are_renamed_to_what_the_daemon_expects(tmp_path, monkeypatch):
    """config.toml may call them whatever this machine calls them; the daemon gets the real names."""
    monkeypatch.setenv("MY_SLSK_USER", "someone")
    monkeypatch.setenv("MY_SLSK_PASSWORD", "a secret")
    config = make_config(
        tmp_path,
        '[credentials]\nsoulseek_user = "MY_SLSK_USER"\nsoulseek_password = "MY_SLSK_PASSWORD"\n',
    )

    env = slsk.environment(config)

    assert env["SOULSEEK_USERNAME"] == "someone"
    assert env["SOULSEEK_PASSWORD"] == "a secret"


def test_a_missing_credential_names_the_variable(tmp_path, creds, monkeypatch):
    monkeypatch.delenv("SLSK_PASSWORD")
    with pytest.raises(slsk.MissingCredential, match="SLSK_PASSWORD"):
        slsk.environment(make_config(tmp_path))


def test_the_socket_lives_where_the_config_says(tmp_path):
    assert slsk.socket_path(make_config(tmp_path)) == tmp_path / "daemon" / "daemon.sock"


def test_the_concurrency_the_daemon_runs_at_comes_from_config(tmp_path, creds):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[soulseek]\nmax_downloads = 7\nworkers = 5\nstate_dir = "%s"\n' % (tmp_path / "d"),
        encoding="utf-8",
    )

    env = slsk.environment(config_module.load(config_path))

    assert env["SOULSEEK_MAX_CONCURRENT_DOWNLOADS"] == "7"


class Running:
    """A fake daemon on a socket, and a client of it."""

    def __init__(self, tmp_path: Path, **kwargs) -> None:
        self.directory = short_dir()
        self.socket = self.directory / "daemon.sock"
        self.fake = Fake(self.socket, **kwargs)
        self.config = make_config(tmp_path, state_dir=self.directory)

    def __enter__(self) -> tuple[slsk.Daemon, Fake]:
        self.fake.start()
        daemon = slsk.Daemon(self.config)
        daemon.ensure()
        return daemon, self.fake

    def __exit__(self, *exc: object) -> bool:
        self.fake.stop_server()
        shutil.rmtree(self.directory, ignore_errors=True)
        return False


def test_the_client_authenticates_and_names_the_account(tmp_path, creds):
    with Running(tmp_path) as (daemon, _fake):
        assert daemon.username == "someone"
        assert daemon.status()["username"] == "someone"
        assert daemon.session_lost() is False


def test_a_protocol_mismatch_is_refused_rather_than_guessed(tmp_path, creds):
    """The daemon bumps its protocol when a change would break a client written against the old shape. A
    client that guessed would send the old shape and read the answer as a failure of the network."""
    with pytest.raises(slsk.DaemonError, match="protocol"):
        with Running(tmp_path, protocol=3):
            pass


def test_the_wrong_account_on_the_daemon_is_named(tmp_path, creds):
    """One session per account: a daemon logged in as somebody else would mix two libraries and hold the
    wrong session against the server's own limit."""
    directory = short_dir()
    fake = Fake(directory / "daemon.sock", username="another")
    fake.start()
    daemon = slsk.Daemon(make_config(tmp_path, state_dir=directory))
    try:
        with pytest.raises(slsk.DaemonError, match="another"):
            daemon.ensure()
    finally:
        fake.stop_server()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_search_is_put_on_the_wire_and_read_once(tmp_path, creds):
    with Running(tmp_path, results=ONE) as (daemon, fake):
        offers = daemon.search("kiasmos blurred", timeout=0)

    assert [offer["username"] for offer in offers] == ["peer"]
    assert offers[0]["filename"] == "Music\\Artist - Title.flac"
    assert offers[0]["duration_sec"] == 449
    methods = [method for method, _ in fake.calls if method != "auth"]
    assert methods == ["search.forget", "search.start", "search.results", "search.forget"]


def test_a_download_is_addressable_and_its_path_is_derived(tmp_path, creds):
    """The daemon names the directory and the peer's own path; the file is the basename of that path inside
    it, which is where the daemon writes the rename."""
    landed = tmp_path / "staging" / "Artist - Title.flac"
    landed.parent.mkdir(parents=True)
    landed.write_bytes(b"x")

    with Running(tmp_path, results=ONE, download=f"downloading:transferring:100:{landed}") as (daemon, _fake):
        daemon.set_dir(landed.parent)
        started = daemon.download("peer", "Music\\Artist - Title.flac", size=100)
        assert started["status"]["state"] == "in_progress"
        listed = daemon.downloads()[("peer", "Music\\Artist - Title.flac")]
        assert slsk.local_path(listed) == str(landed)
        daemon.cancel("peer", "Music\\Artist - Title.flac")

    assert _fake.calls[-1] == ("download.cancel", {"username": "peer",
                                                  "filename": "Music\\Artist - Title.flac"})


def test_an_error_from_the_daemon_keeps_its_exit_code(tmp_path, creds):
    """Application failures carry `data.exit`, the same status the CLI would have produced locally, so a
    wrapper branches on it exactly as it branches on an exit code."""
    directory = short_dir()
    fake = Fake(directory / "daemon.sock")
    fake.start()
    daemon = slsk.Daemon(make_config(tmp_path, state_dir=directory))
    try:
        daemon.ensure()
        with pytest.raises(slsk.DaemonError) as caught:
            daemon.call("no.such.method")
    finally:
        fake.stop_server()
        shutil.rmtree(directory, ignore_errors=True)

    assert caught.value.exit_status == 3
