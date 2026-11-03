"""The daemon as the desktop owns it: start, status, stop, and the settings a running daemon can take.

`slsk.Daemon` is the wire. This module decides whether a daemon may be started at all, keeps the child it
started, and turns the daemon's own status and transfer list into one snapshot a window can draw. An attached
daemon is not stopped here: only the child this runtime spawned is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import processes, slsk
from .config import Config

# States the desktop draws. `starting` and `stopping` are transitions, the rest are settled.
STOPPED = "stopped"
STARTING = "starting"
CONNECTED = "connected"
DISCONNECTED = "disconnected"
STOPPING = "stopping"
FAILED = "failed"

# A download in one of these states is over; the rest are still touching a file.
FINISHED = {"completed", "failed", "cancelled", "timed_out"}

STOP_GRACE = 10.0


@dataclass(frozen=True)
class RuntimeSettings:
    """What one side asked for, or what the daemon is actually doing."""

    server: str = ""
    listen_port: int = 0
    upload_slots: int = 0
    max_downloads: int = 0
    shared_dirs: tuple[str, ...] = ()
    download_dir: str = ""
    username: str = ""


@dataclass(frozen=True)
class RuntimeStatus:
    state: str
    managed: bool = False
    username: str = ""
    session_loss: str | None = None
    daemon_status: dict[str, Any] | None = None
    transfers: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    saved: RuntimeSettings | None = None
    effective: RuntimeSettings | None = None
    restart_required: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    reindexing: bool | None = None
    error: str | None = None


class SoulseekRuntime:
    """One daemon, the settings around it, and whether this process may stop it."""

    def __init__(self, config: Config, *, log=print) -> None:
        self._config = config
        # What the daemon was attached or started with: saved settings are compared against this, never
        # against themselves, or a restart-required port would look effective the moment it was saved.
        self._running_config = config
        self._log = log
        self.daemon = slsk.Daemon(config, log=log)
        self._state = STOPPED
        self._error: str | None = None
        self._effective = RuntimeSettings()
        self._shares: tuple[str, ...] = ()
        self._reindexing: bool | None = None
        self._last_status = self._snapshot()

    # ── what the window calls ────────────────────────────────────────────────

    @property
    def last_status(self) -> RuntimeStatus:
        """The latest snapshot, without touching the socket: safe on a drawing thread."""
        return self._last_status

    def start(self) -> RuntimeStatus:
        if self._state == CONNECTED and self.daemon.username:
            return self.status()
        self._state = STARTING
        self._error = None
        try:
            shared = _share_paths(self._config)
            if not self._connect():
                self._state = STOPPED
                self._error = ("no daemon answered and the Soulseek username and password are not "
                               "configured to start one")
            elif self.daemon.process is not None and self.daemon.process.poll() is not None:
                self._state = FAILED
                self._error = "the Soulseek daemon exited while starting"
            else:
                self._state = CONNECTED
                self._running_config = self._config
                self._apply_live_settings(self._config, shared)
        except (slsk.DaemonError, slsk.PortBusy, slsk.MissingCredential, ValueError) as error:
            if self.daemon.process is not None and self.daemon.process.poll() is None:
                self._stop_owned(self.daemon.process)
            self._state = FAILED
            self._error = str(error)
            self._log(f"the Soulseek daemon did not start: {error}")
        return self.status()

    def status(self) -> RuntimeStatus:
        """Ask the daemon and return one snapshot. This does socket I/O."""
        if self._state not in {CONNECTED, DISCONNECTED}:
            return self._snapshot()
        recovering = self._state == DISCONNECTED
        try:
            daemon_status = self.daemon.status()
            downloads = self.daemon.downloads()
            self._state = CONNECTED
            if recovering:
                self._error = None
        except (slsk.DaemonError, OSError) as error:
            self._state = DISCONNECTED
            self._error = str(error)
            return self._snapshot()
        events = self.daemon.drain_events()
        self._effective = self._effective_settings(daemon_status)
        self._apply_pending_download_dir(downloads)
        self._last_status = self._snapshot(daemon_status=daemon_status, transfers=_rows(downloads),
                                           events=events)
        return self._last_status

    def refresh_status(self) -> RuntimeStatus:
        """Poll from a worker thread; identical to `status`, named for the caller's intent."""
        return self.status()

    def stop(self) -> RuntimeStatus:
        self._state = STOPPING
        process = self.daemon.process
        if process is not None:
            self._stop_owned(process)
        else:
            self.daemon.close()
        self._state = STOPPED
        self._error = None
        return self.status()

    def apply_settings(self, config: Config) -> RuntimeStatus:
        """Save the new config, apply what the daemon takes live, and name what waits for a restart."""
        self._config = config
        self._error = None
        try:
            shared = _share_paths(config)
        except ValueError as error:
            self._error = str(error)
            self._log(str(error))
            return self.status()
        if self._state != CONNECTED:
            return self.status()
        try:
            self._set_shares(shared)
            self._apply_download_dir(config)
        except (slsk.DaemonError, OSError) as error:
            self._error = str(error)
            self._state = DISCONNECTED
        return self.status()

    # ── lifecycle internals ──────────────────────────────────────────────────

    def _connect(self) -> bool:
        if self.daemon.connect():
            return True
        process = self.daemon.process
        if process is not None and process.poll() is None:
            return self._await_connect()
        if not self._can_spawn():
            return False
        self.daemon.start()
        return self._await_connect()

    def _await_connect(self) -> bool:
        deadline = time.monotonic() + slsk.STARTUP_TIMEOUT
        while not self.daemon.connect():
            if time.monotonic() > deadline:
                raise slsk.DaemonError(
                    "start", f"no daemon on {self.daemon.endpoint()} after {slsk.STARTUP_TIMEOUT:g}s")
            time.sleep(0.25)
        return True

    def _can_spawn(self) -> bool:
        config = self._config
        return bool(config.credential(config.slsk_username_env)
                    and config.credential(config.slsk_password_env))

    def _stop_owned(self, process) -> None:
        try:
            self.daemon.stop()
            process.wait(timeout=STOP_GRACE)
        except Exception as error:                      # noqa: BLE001 — a dead child is the goal
            self._log(f"the Soulseek daemon did not stop on its own: {error}")
            processes.stop_owned(process, grace=0)
        finally:
            self.daemon.close()

    # ── settings ─────────────────────────────────────────────────────────────

    def _apply_live_settings(self, config: Config, shared: tuple[Path, ...]) -> None:
        self._set_shares(shared)
        self._apply_download_dir(config)

    def _set_shares(self, shared: tuple[Path, ...]) -> None:
        answer = self.daemon.share(shared)
        self._shares = tuple(str(path) for path in shared)
        self._reindexing = False
        self._log(f"shared {len(self._shares)} folders ({answer.get('files', 0)} files)")

    def _apply_download_dir(self, config: Config) -> None:
        """Point the daemon at the new staging directory, unless a transfer there is still running."""
        current = self._effective.download_dir or _daemon_download_dir(self.daemon)
        if str(config.staging) == current:
            return
        if _active_transfers(self.daemon.downloads()):
            return
        self.daemon.set_dir(config.staging)

    def _apply_pending_download_dir(self, downloads: dict[tuple[str, str], dict[str, Any]]) -> None:
        """Apply a destination that was deferred while transfers were running."""
        current = self._effective.download_dir
        destination = str(self._config.staging)
        if not current or destination == current or _active_transfers(downloads):
            return
        try:
            self.daemon.set_dir(self._config.staging)
        except (slsk.DaemonError, OSError) as error:
            self._log(f"the download directory could not move yet: {error}")
            return
        self._effective = replace(self._effective, download_dir=destination)

    def _effective_settings(self, daemon_status: dict[str, Any]) -> RuntimeSettings:
        server = str(daemon_status.get("server") or self._running_config.soulseek_server)
        return RuntimeSettings(
            server=server,
            listen_port=int(daemon_status.get("listen_port")
                            or self._running_config.soulseek_listen_port),
            upload_slots=self._running_config.upload_slots,
            max_downloads=self._running_config.max_downloads,
            shared_dirs=self._shares or tuple(str(path) for path in slsk.shared_directories(self._config)),
            download_dir=str(daemon_status.get("download_dir") or ""),
            username=str(daemon_status.get("username") or self.daemon.username),
        )

    def _saved_settings(self, config: Config) -> RuntimeSettings:
        return RuntimeSettings(
            server=config.soulseek_server,
            listen_port=config.soulseek_listen_port,
            upload_slots=config.upload_slots,
            max_downloads=config.max_downloads,
            shared_dirs=tuple(str(path) for path in slsk.shared_directories(config)),
            download_dir=str(config.staging),
            username=config.credential(config.slsk_username_env),
        )

    def _snapshot(self, *, daemon_status: dict[str, Any] | None = None,
                  transfers: tuple[dict[str, Any], ...] = (),
                  events: tuple[dict[str, Any], ...] = ()) -> RuntimeStatus:
        saved = self._saved_settings(self._config)
        effective = self._effective
        return RuntimeStatus(
            state=self._state,
            managed=self.daemon.process is not None and self.daemon.process.poll() is None,
            username=self.daemon.username,
            session_loss=_session_loss(daemon_status or {}),
            daemon_status=daemon_status,
            transfers=transfers,
            events=events,
            saved=saved,
            effective=effective,
            restart_required=_restart_required(saved, effective),
            deferred=("download_dir",) if (self._state == CONNECTED and _deferred_download(
                saved.download_dir, effective.download_dir)) else (),
            reindexing=self._reindexing,
            error=self._error,
        )


def _active_transfers(downloads: dict[tuple[str, str], dict[str, Any]]) -> bool:
    return any(str((item.get("status") or {}).get("state")) not in FINISHED
               for item in downloads.values())


def _daemon_download_dir(daemon: slsk.Daemon) -> str:
    try:
        return str(daemon.status().get("download_dir") or "")
    except slsk.DaemonError:
        return ""


def _deferred_download(saved: str, effective: str) -> bool:
    return bool(effective) and saved != effective


def _session_loss(daemon_status: dict[str, Any]) -> str | None:
    value = daemon_status.get("session_loss")
    if isinstance(value, dict):
        return str(value.get("kind") or "") or None
    return str(value) if value else None


def _restart_required(saved: RuntimeSettings, effective: RuntimeSettings) -> tuple[str, ...]:
    """Saved values the running daemon did not take. Empty until it has reported what it runs."""
    if not effective.listen_port:
        return ()
    changed = []
    if saved.server and saved.server != effective.server:
        changed.append("server")
    if saved.listen_port != effective.listen_port:
        changed.append("listen_port")
    if saved.upload_slots != effective.upload_slots:
        changed.append("upload_slots")
    if saved.max_downloads != effective.max_downloads:
        changed.append("max_downloads")
    return tuple(changed)


def _share_paths(config: Config) -> tuple[Path, ...]:
    """The configured roots, refusing any that would expose staging or application state."""
    for lane in (config.lossless, config.lossy):
        Path(lane).expanduser().mkdir(parents=True, exist_ok=True)
    private = tuple(root.expanduser().resolve()
                    for root in (config.staging, config.state_root, slsk.state_dir(config),
                                 config.peers.parent))
    shared = tuple(Path(path).expanduser().resolve() for path in slsk.shared_directories(config))
    for directory in shared:
        if any(directory == root or root in directory.parents for root in private):
            raise ValueError(f"cannot share {directory}: it holds staging or application state")
    return shared


def _transfer_state(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("status") or {}


def _rows(downloads: dict[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    rows = []
    for item in downloads.values():
        status = _transfer_state(item)
        received = int(status.get("bytes_downloaded") or 0)
        total = status.get("total_bytes")
        total = int(total) if isinstance(total, int) and total > 0 else None
        rows.append({
            "peer": str(item.get("username") or ""),
            "remote_file": str(item.get("filename") or ""),
            "local_path": slsk.local_path(item),
            "status": str(status.get("state") or ""),
            "received_bytes": received,
            "total_bytes": total,
            "progress": received / total if total else None,
            "speed_bytes_per_sec": float(status.get("speed_bytes_per_sec") or 0.0),
            "queue_position": item.get("queue_position"),
        })
    return tuple(rows)
