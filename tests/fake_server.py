"""A stand-in for the Soulseek daemon: the handful of calls a pass makes, no Soulseek and no login.

Two shapes of it, because two things are worth testing apart. `Direct` has the same Python methods as
`slsk.Daemon` and is what the acquisition tests drive, so the loop is tested without a socket. `Fake` puts
`Direct` behind a real AF_UNIX listener answering the daemon's JSON-RPC, which is what tests the client.

A test scripts it with:

  results   the offers a search answers with, grouped by peer on the way out
  download  the life of each download, `|`-separated scripts of `;`-separated steps, each
            `status:connection_state:received_bytes:local_path[:total_bytes]`. The Nth download started takes
            the Nth script; every `download.list` walks every transfer one step, holding on its last step.
  calls     every method the pipeline asked for, in order
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

DEFAULT_RESULTS = [
    {
        "username": "vinyl_only",
        "filename": "Music\\Kiasmos - Blurred (Extended Mix).flac",
        "filesize": 82_000_000,
        "bitrate": 855,
        "duration_sec": 448,
        "has_free_slots": True,
    }
]


def _scripts(raw: str | None) -> list[list[dict]]:
    raw = raw or "downloading:transferring:1000:"
    return [_parse(chunk) for chunk in raw.split("|")]


def _parse(chunk: str) -> list[dict]:
    steps = []
    for step in chunk.split(";"):
        status, _, rest = step.partition(":")
        connection, _, rest = rest.partition(":")
        size, _, rest = rest.partition(":")
        path, _, total = rest.partition(":")
        steps.append({
            "status": status,
            "connection_state": connection,
            "bytes": int(size or 0),
            "local_path": path or os.environ.get("FAKE_LOCAL_PATH", ""),
            "total": int(total) if total else None,
        })
    return steps


def _status(step: dict, size: int) -> dict:
    """One script step as the daemon's tagged union."""
    state = step["status"]
    if state in {"downloading", "in_progress"}:
        return {"state": "in_progress", "bytes_downloaded": step["bytes"],
                "total_bytes": step["total"] or size, "speed_bytes_per_sec": 0.0}
    if state in {"finished", "completed"}:
        return {"state": "completed"}
    if state == "failed":
        return {"state": "failed", "reason": step["connection_state"] or "the peer closed"}
    if state in {"cancelled", "timed_out", "paused", "queued"}:
        return {"state": state}
    return {"state": "queued"}


def _directory(path: str) -> str:
    return str(Path(path).parent) if path else ""


class Direct:
    """The daemon's method surface, answering from the script instead of the network."""

    def __init__(self, *, results: list[dict] | None = None, download: str | None = None,
                 session_loss: bool = False, shared_files: int = 2) -> None:
        self.results = list(results if results is not None else DEFAULT_RESULTS)
        self.scripts = _scripts(download)
        self.calls: list[tuple[str, dict]] = []
        self.session_loss = "displaced" if session_loss else None
        self.shared_files = shared_files
        self.directory = ""
        self.shares: list[str] = []
        self.downloads_by_key: dict[tuple[str, str], dict] = {}
        self._group = 0
        self._lock = threading.Lock()

    # ── lifecycle the pass calls ─────────────────────────────────────────────

    def ensure(self) -> None:
        self.calls.append(("ensure", {}))

    def close(self) -> None:
        pass

    def stop(self) -> None:
        self.calls.append(("daemon.stop", {}))

    def __enter__(self) -> "Direct":
        self.ensure()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # ── session ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {"username": os.environ.get("SLSK_USERNAME") or "someone",
                "server": "server.slsknet.org:2416", "daemon_version": "19.1.0", "protocol": 2,
                "shared_files": self.shared_files, "shared_folders": len(self.shares),
                "download_dir": self.directory, "clients": 1, "uptime_secs": 1,
                "session_loss": self.session_loss}

    def session_lost(self) -> bool:
        return self.session_loss == "displaced"

    def set_dir(self, directory: Path | str) -> None:
        self.directory = str(directory)
        self.calls.append(("download.set_dir", {"directory": str(directory)}))

    def share(self, directories) -> dict:
        self.shares = [str(path) for path in directories]
        self.calls.append(("shares.set", {"directories": self.shares}))
        return {"files": self.shared_files, "folders": len(self.shares),
                "directories": list(self.shares)}

    # ── search ───────────────────────────────────────────────────────────────

    def search(self, query: str, *, timeout: float) -> list[dict]:
        self.calls.append(("search", {"query": query}))
        return _flatten(self.grouped())

    def grouped(self) -> list[dict]:
        """The search answer as the daemon sends it: one entry per peer, files under it."""
        peers: dict[str, dict] = {}
        for item in self.results:
            peer = peers.setdefault(item["username"], {
                "token": len(peers), "username": item["username"],
                "slots": 1 if item.get("has_free_slots", True) else 0,
                "speed": int(item.get("avg_speed") or 1_000_000), "files": [],
            })
            attribs = {}
            if item.get("bitrate"):
                attribs["0"] = int(item["bitrate"])
            if item.get("duration_sec"):
                attribs["1"] = int(item["duration_sec"])
            peer["files"].append({"name": item["filename"], "size": int(item.get("filesize") or 0),
                                  "attribs": attribs})
        return list(peers.values())

    # ── transfers ────────────────────────────────────────────────────────────

    def download(self, user: str, filename: str, *, size: int = 0,
                 metadata: dict | None = None) -> dict:
        with self._lock:
            self.calls.append(("download.start", {"username": user, "filename": filename,
                                                  "size": size}))
            key = (user, filename)
            entry = self.downloads_by_key.get(key)
            if entry is None or entry["status"]["state"] in {"failed", "cancelled", "timed_out"}:
                script = self.scripts[min(self._group, len(self.scripts) - 1)]
                self._group += 1
                entry = self.downloads_by_key[key] = {
                    "username": user, "filename": filename, "token": self._group, "size": size,
                    "queue_position": None, "status": _status(script[0], size),
                    "_script": script, "_step": 0, "_local": script[0]["local_path"],
                }
            return self._dto(entry)

    def downloads(self) -> dict[tuple[str, str], dict]:
        with self._lock:
            self.calls.append(("download.list", {}))
            out = {}
            for key, entry in self.downloads_by_key.items():
                entry["_step"] += 1
                step = entry["_script"][min(entry["_step"], len(entry["_script"]) - 1)]
                entry["status"] = _status(step, entry["size"])
                entry["_local"] = step["local_path"] or entry["_local"]
                entry["queue_position"] = entry["_step"] if step["status"] == "queued" else None
                out[key] = self._dto(entry)
            return out

    def _dto(self, entry: dict) -> dict:
        directory = self.directory or _directory(entry["_local"])
        return {"username": entry["username"], "filename": entry["filename"],
                "token": entry["token"], "size": entry["size"],
                "download_directory": directory, "queue_position": entry["queue_position"],
                "status": dict(entry["status"]), "metadata": {}}

    def cancel(self, user: str, filename: str) -> None:
        with self._lock:
            self.calls.append(("download.cancel", {"username": user, "filename": filename}))
            entry = self.downloads_by_key.get((user, filename))
            if entry is not None:
                entry["status"] = {"state": "cancelled"}


def _flatten(results: list[dict]) -> list[dict]:
    from music_agent import slsk

    return slsk._files(results)


class Fake(Direct):
    """`Direct` behind a Unix socket, answering the daemon's JSON-RPC."""

    def __init__(self, path: Path | str, *, protocol: int = 2, username: str | None = None,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self.path = Path(path)
        self.protocol = protocol
        self.username = username or os.environ.get("SLSK_USERNAME") or "someone"
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._listener is not None
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection: socket.socket) -> None:
        buffer = b""
        with connection:
            while True:
                try:
                    chunk = connection.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    message = json.loads(line)
                    connection.sendall(json.dumps(self._answer(message)).encode() + b"\n")

    def _answer(self, message: dict) -> dict:
        method = message.get("method")
        params = message.get("params") or {}
        answer: dict = {"jsonrpc": "2.0", "id": message.get("id")}
        try:
            answer["result"] = self._dispatch(method, params)
        except Exception as error:                   # the daemon's own shape: -32000, with an exit code
            answer["error"] = {"code": -32000, "message": str(error), "data": {"exit": 3}}
        return answer

    def _dispatch(self, method: str, params: dict) -> dict:
        if method in {"auth", "search.start", "search.results", "search.forget", "daemon.status"}:
            self.calls.append((method, params))
        if method == "auth":
            return {"protocol": self.protocol, "daemon_version": "19.1.0",
                    "username": self.username}
        if method == "daemon.status":
            return self.status()
        if method == "daemon.stop":
            self.stop()
            return {"ok": True}
        if method == "search.start":
            return {"ok": True}
        if method == "search.results":
            return {"results": self.grouped()}
        if method == "search.forget":
            return {"ok": True}
        if method == "download.set_dir":
            self.set_dir(params["directory"])
            return {"ok": True}
        if method == "shares.set":
            self.share(params["directories"])
            return {"files": self.shared_files, "folders": len(self.shares),
                "directories": list(self.shares)}
        if method == "download.start":
            return {"download": self.download(params["username"], params["filename"],
                                              size=int(params.get("size") or 0))}
        if method == "download.list":
            return {"downloads": list(self.downloads().values())}
        if method == "download.cancel":
            self.cancel(params["username"], params["filename"])
            return {"ok": True}
        raise ValueError(f"no method {method}")

    def stop_server(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None

    def close(self) -> None:
        self.stop_server()
