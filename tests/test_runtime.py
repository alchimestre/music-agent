from __future__ import annotations

import json
import socket
import socketserver
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from fake_server import Fake
from test_slsk import make_config


class ControlServer:
    def __init__(self, path: Path, token: str, *, username: str = "someone") -> None:
        self.direct = Fake(path, username=username)
        self.token = token
        self.clients: set[socket.socket] = set()
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                owner.clients.add(self.request)
                authenticated = False
                try:
                    for line in self.rfile:
                        request = json.loads(line)
                        method = request.get("method")
                        params = request.get("params") or {}
                        if method == "auth":
                            authenticated = params.get("token") == owner.token
                            if not authenticated:
                                answer = {"jsonrpc": "2.0", "id": request.get("id"),
                                          "error": {"code": -32000, "message": "bad token"}}
                            else:
                                answer = owner.answer(request)
                        elif authenticated:
                            answer = owner.answer(request)
                        else:
                            answer = {"jsonrpc": "2.0", "id": request.get("id"),
                                      "error": {"code": -32600, "message": "auth first"}}
                        self.wfile.write(json.dumps(answer).encode() + b"\n")
                finally:
                    owner.clients.discard(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.control = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.address = f"{host}:{port}"

    def answer(self, request: dict) -> dict:
        method = request.get("method")
        result = self.direct._dispatch(method, request.get("params") or {})
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}

    def emit(self, event: dict) -> None:
        payload = json.dumps(event).encode() + b"\n"
        for client in tuple(self.clients):
            client.sendall(payload)

    def close_clients(self) -> None:
        for client in tuple(self.clients):
            client.close()

    def close(self) -> None:
        self.close_clients()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.direct.stop_server()


@pytest.fixture
def control(tmp_path):
    token = "control-token"
    state = tmp_path / "daemon-state"
    state.mkdir()
    (state / "daemon.token").write_text(token, encoding="utf-8")
    (state / "daemon.token").chmod(0o600)
    server = ControlServer(tmp_path / "fake.sock", token)
    yield server, state
    server.close()


def tcp_config(tmp_path: Path, address: str, state: Path):
    config = make_config(tmp_path, state_dir=state)
    return replace(config, daemon_address=address, soulseek_username="someone")


def test_runtime_authenticates_tcp_and_leaves_an_external_daemon_running(control, tmp_path):
    from music_agent import runtime

    server, state = control
    manager = runtime.SoulseekRuntime(tcp_config(tmp_path, server.address, state))

    status = manager.start()
    again = manager.start()
    stopped = manager.stop()

    assert status.state == "connected"
    assert again.state == "connected"
    assert status.managed is False
    assert status.username == "someone"
    assert any(method == "auth" and params["token"] == server.token
               for method, params in server.direct.calls)
    assert [method for method, _params in server.direct.calls].count("auth") == 1
    assert stopped.state == "stopped"
    assert not any(method == "daemon.stop" for method, _params in server.direct.calls)


def test_runtime_returns_daemon_transfer_snapshot_and_drains_events(control, tmp_path):
    from music_agent import runtime

    server, state = control
    server.direct.directory = str(tmp_path / "staging")
    server.direct.download("peer", "Music\\one.flac", size=100)
    manager = runtime.SoulseekRuntime(tcp_config(tmp_path, server.address, state))
    manager.start()

    server.emit({"jsonrpc": "2.0", "method": "event.download_status",
                 "params": {"username": "peer", "filename": "Music\\one.flac"}})
    deadline = time.monotonic() + 1
    events = []
    while not events and time.monotonic() < deadline:
        events = manager.daemon.drain_events()
        time.sleep(0.01)
    status = manager.status()
    manager.stop()

    assert events[0]["method"] == "event.download_status"
    assert status.transfers[0]["peer"] == "peer"
    assert status.transfers[0]["remote_file"] == "Music\\one.flac"
    assert status.transfers[0]["status"] == "in_progress"


def test_apply_settings_defers_download_directory_and_reports_restart_values(control, tmp_path):
    from music_agent import runtime

    server, state = control
    old_directory = tmp_path / "old-staging"
    new_directory = tmp_path / "new-staging"
    server.direct.directory = str(old_directory)
    server.direct.download("peer", "Music\\one.flac", size=100)
    base = tcp_config(tmp_path, server.address, state)
    manager = runtime.SoulseekRuntime(base)
    manager.start()
    config = replace(base, staging=new_directory, shared_dirs=(tmp_path / "Music",),
                     soulseek_server="new.server:2416", soulseek_listen_port=32123,
                     upload_slots=5, max_downloads=8)

    status = manager.apply_settings(config)
    manager.stop()

    assert status.saved.download_dir == str(new_directory)
    assert status.effective.download_dir == str(old_directory)
    assert "download_dir" in status.deferred
    assert set(status.restart_required) == {"server", "listen_port", "upload_slots", "max_downloads"}
    assert server.direct.shares == [str((tmp_path / "Music").resolve())]


def test_a_deferred_download_directory_is_applied_once_transfers_finish(control, tmp_path):
    from music_agent import runtime

    server, state = control
    old_directory = tmp_path / "old-staging"
    new_directory = tmp_path / "new-staging"
    server.direct.directory = str(old_directory)
    server.direct.download("peer", "Music\\one.flac", size=100)
    base = tcp_config(tmp_path, server.address, state)
    manager = runtime.SoulseekRuntime(base)
    manager.start()

    deferred = manager.apply_settings(replace(base, staging=new_directory))
    server.direct.downloads_by_key.clear()
    applied = manager.status()
    manager.stop()

    assert "download_dir" in deferred.deferred
    assert applied.effective.download_dir == str(new_directory)
    assert applied.deferred == ()
    assert server.direct.directory == str(new_directory)


def test_apply_settings_refuses_shares_that_expose_staging(control, tmp_path):
    from music_agent import runtime

    server, state = control
    base = tcp_config(tmp_path, server.address, state)
    manager = runtime.SoulseekRuntime(base)
    manager.start()
    applied = list(server.direct.shares)
    config = replace(base, shared_dirs=(base.staging,))

    status = manager.apply_settings(config)
    manager.stop()

    assert status.error
    assert str(base.staging) not in server.direct.shares
    assert server.direct.shares == applied


FAKE_DAEMON = r'''
import json
import os
import socket
import sys

password = sys.stdin.readline().rstrip("\r\n")
if not password or password in sys.argv:
    sys.exit(10)
if "SLSK_PASSWORD" in os.environ or "SOULSEEK_PASSWORD" in os.environ:
    sys.exit(11)
state = os.environ["SOULSEEK_STATE_DIR"]
with open(os.path.join(state, "password-read"), "w", encoding="utf-8") as handle:
    handle.write(password)
token = "owned-token"
token_path = os.path.join(state, "daemon.token")
with open(token_path, "w", encoding="utf-8") as handle:
    handle.write(token)
os.chmod(token_path, 0o600)
arguments = sys.argv[1:]
def value(name, default):
    return arguments[arguments.index(name) + 1] if name in arguments else default
host, port = value("--bind", "127.0.0.1:1").rsplit(":", 1)
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((host, int(port)))
listener.listen()
running = True
while running:
    connection, _address = listener.accept()
    with connection:
        buffer = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                request = json.loads(line)
                method = request["method"]
                if method == "auth":
                    result = {"protocol": 2, "daemon_version": "19.1.0",
                              "username": value("--username", "")}
                    if request["params"].get("token") != token:
                        result = None
                        answer = {"jsonrpc": "2.0", "id": request["id"],
                                  "error": {"code": -32000, "message": "bad token"}}
                    else:
                        answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                elif method == "daemon.status":
                    answer = {"jsonrpc": "2.0", "id": request["id"],
                              "result": {"username": value("--username", ""),
                                         "server": value("--server", "server.slsknet.org:2416"),
                                         "protocol": 2, "daemon_version": "19.1.0",
                                         "listen_port": int(value("--listener-port", "2234")),
                                         "download_dir": value("--download-dir", ""),
                                         "shared_folders": 0, "shared_files": 0,
                                         "clients": 1, "uptime_secs": 1}}
                elif method == "download.list":
                    answer = {"jsonrpc": "2.0", "id": request["id"], "result": {"downloads": []}}
                elif method == "daemon.stop":
                    answer = {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}
                    with open(os.path.join(state, "stopped"), "w", encoding="utf-8") as handle:
                        handle.write("yes")
                    running = False
                else:
                    answer = {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}
                connection.sendall(json.dumps(answer).encode() + b"\n")
                if method == "daemon.stop":
                    break
listener.close()
'''


def test_runtime_starts_owned_daemon_with_password_stdin(tmp_path, monkeypatch):
    from music_agent import runtime

    state = tmp_path / "owned-state"
    state.mkdir()
    password = "password-from-stdin"
    monkeypatch.setenv("SLSK_PASSWORD", "parent-process-only")
    config = make_config(tmp_path, state_dir=state)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    script = tmp_path / "fake_daemon.py"
    script.write_text(FAKE_DAEMON, encoding="utf-8")
    config = replace(config, soulseek_command=(sys.executable, str(script), "daemon"),
                     daemon_address=f"127.0.0.1:{port}", soulseek_username="someone",
                     secrets={config.slsk_password_env: password})
    monkeypatch.setattr(runtime.slsk, "free_ports", lambda *_args, **_kwargs: [])
    manager = runtime.SoulseekRuntime(config)

    status = manager.start()
    stopped = manager.stop()

    assert status.state == "connected"
    assert status.managed is True
    assert (state / "password-read").read_text(encoding="utf-8") == password
    assert (state / "stopped").read_text(encoding="utf-8") == "yes"
    assert stopped.state == "stopped"


def test_empty_credentials_offline_do_not_spawn(tmp_path, monkeypatch):
    from music_agent import runtime

    monkeypatch.delenv("SLSK_USERNAME", raising=False)
    monkeypatch.delenv("SLSK_PASSWORD", raising=False)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = replace(make_config(tmp_path, state_dir=tmp_path / "state"), daemon_address=f"127.0.0.1:{port}")
    manager = runtime.SoulseekRuntime(config)

    status = manager.start()

    assert status.state == "stopped"
    assert status.managed is False
    assert manager.daemon.process is None
