from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import settings, slsk, state

FAILED_STATUSES = frozenset({state.FAILED, state.NOT_FOUND})
FINISHED_STATUSES = frozenset({state.TAGGED, state.IMPORTED, state.FOUND_IN_LIBRARY})
ACTIVE_STATUSES = frozenset({state.DOWNLOADED, state.CONVERTED, state.NORMALIZED})
LIVE_STATUSES = state.LIVE


class _ApplicationWorker(QObject):
    completed = Signal(object)
    progress = Signal(object)
    queue_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.application: Any = None
        self._stop_requested = threading.Event()

    @Slot()
    def initialize(self) -> None:
        self.execute(("initialize", {}))

    @Slot(object)
    def execute(self, command: object) -> None:
        action, payload = command
        try:
            value = self._execute_action(action, payload)
            self.completed.emit({"action": action, "value": value})
        except Exception as error:
            self.completed.emit({
                "action": action,
                "error": str(error),
                "error_type": type(error).__name__,
            })

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        if self.application is not None:
            event = self.application.pause_event
            event.set() if paused else event.clear()

    @Slot()
    def begin_run(self) -> None:
        self._stop_requested.clear()

    @Slot()
    def request_stop(self) -> None:
        self._stop_requested.set()
        if self.application is not None:
            self.application.stop_event.set()

    def _execute_action(self, action: str, payload: dict[str, Any]) -> Any:
        if action == "initialize":
            return self._initialize_application()
        if self.application is None:
            raise RuntimeError("The application has not finished loading.")
        if action == "snapshot":
            return self.application.snapshot()
        if action == "preview":
            return self.application.preview(payload["source"])
        if action == "enqueue":
            job_id = self.application.enqueue(payload["preview"])
            return {"job_id": job_id, "queue": self.application.snapshot()}
        if action == "candidates":
            return self.application.candidates(payload["job_id"], payload["idx"])
        if action == "decide":
            return self._decide(payload)
        if action == "run":
            return self._run(payload)
        if action == "delivery_plans":
            return self.application.delivery_plans(payload["job_id"])
        if action == "deliver":
            return self._deliver(payload)
        if action == "save":
            return self._save(payload)
        if action == "runtime_status":
            return self.application.runtime.status()
        if action == "runtime_start":
            return self.application.runtime.start()
        if action == "runtime_stop":
            return self.application.runtime.stop()
        if action == "shutdown":
            self.application.shutdown()
            return None
        raise ValueError(f"Unknown desktop operation: {action}")

    def _initialize_application(self) -> dict[str, Any]:
        from .application import Application

        self.application = Application()
        return {
            "config": self.application.config,
            "credential_error": self.application.credential_error,
            "queue": self.application.snapshot(),
            "runtime": self.application.runtime.status(),
        }

    def _decide(self, payload: dict[str, Any]) -> list[Any]:
        self.application.decide(
            payload["job_id"], payload["idx"], payload["action"], payload.get("candidate")
        )
        return self.application.snapshot()

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_snapshot = [0.0]

        def emit_progress(event: object) -> None:
            self.progress.emit(event)
            if time.monotonic() - last_snapshot[0] >= 2.0:
                last_snapshot[0] = time.monotonic()
                self.queue_changed.emit(self.application.snapshot())
            if self._stop_requested.is_set():
                self.application.stop_event.set()

        result = self.application.run(payload["job_id"], emit=emit_progress)
        return {
            "job_id": payload["job_id"],
            "result": result,
            "queue": self.application.snapshot(),
        }

    def _deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.application.deliver(payload["job_id"], emit=self.progress.emit)
        return {"job_id": payload["job_id"], "result": result, "queue": self.application.snapshot()}

    def _save(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.application.save(payload["config"], remember=payload["remember"])
        try:
            runtime = self.application.runtime.apply_settings(self.application.config)
            runtime_error = ""
        except Exception as error:
            runtime = None
            runtime_error = str(error)
        return {
            "config": self.application.config,
            "runtime": runtime,
            "runtime_error": runtime_error,
        }


class DesktopWindow(QMainWindow):
    command_requested = Signal(object)
    pause_requested = Signal(bool)
    stop_requested = Signal()
    begin_run_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Music Agent")
        self.resize(1180, 780)
        self._config: Any = None
        self._queue: list[Any] = []
        self._preview: Any = None
        self._busy = False
        self._active_action = ""
        self._paused = False
        self._closing = False
        self._shutdown_complete = False
        self._shutdown_requested = False
        self._review_key: tuple[str, int] | None = None
        self._last_output_dir: Path | None = None
        self._admission_summary = ""
        self._rendered_queue: list[tuple[str, ...]] = []
        self._pending_config: Any = None
        self._thread = QThread(self)
        self._worker = _ApplicationWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.initialize)
        self.command_requested.connect(self._worker.execute)
        self._worker.queue_changed.connect(self._set_queue)
        self.pause_requested.connect(self._worker.set_paused, Qt.ConnectionType.DirectConnection)
        self.stop_requested.connect(self._worker.request_stop, Qt.ConnectionType.DirectConnection)
        self._worker.completed.connect(self._on_complete)
        self.begin_run_requested.connect(self._worker.begin_run, Qt.ConnectionType.DirectConnection)
        self._worker.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._worker.deleteLater)
        self._build_window()
        self._thread.start()
        self._busy = True
        self._active_action = "initialize"
        self._set_busy_state()
        self._set_status("Loading saved settings and offline queue…")

    def _build_window(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.addLayout(self._build_controls())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_queue_page(), "Queue")
        self.tabs.addTab(self._build_add_page(), "Add playlist")
        self.tabs.addTab(self._build_settings_page(), "Settings")
        layout.addWidget(self.tabs, 1)
        self.status_label = QLabel("Loading application…")
        self.status_label.setAccessibleName("Application status")
        layout.addWidget(self.status_label)

    def _build_controls(self) -> QHBoxLayout:
        controls = QHBoxLayout()
        self.runtime_label = QLabel("Soulseek: checking status")
        self.runtime_label.setAccessibleName("Soulseek connection status")
        self.runtime_label.setMinimumWidth(230)
        controls.addWidget(self.runtime_label, 1)
        self.start_button = QPushButton("Start Soulseek")
        self.start_button.clicked.connect(lambda: self._submit("runtime_start"))
        controls.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop Soulseek")
        self.stop_button.clicked.connect(lambda: self._submit("runtime_stop"))
        controls.addWidget(self.stop_button)
        self.refresh_status_button = QPushButton("Refresh Soulseek status")
        self.refresh_status_button.clicked.connect(lambda: self._submit("runtime_status"))
        controls.addWidget(self.refresh_status_button)
        self.pause_button = QPushButton("Pause queue")
        self.pause_button.clicked.connect(self._toggle_pause)
        controls.addWidget(self.pause_button)
        self.stop_work_button = QPushButton("Stop current work")
        self.stop_work_button.clicked.connect(self._request_work_stop)
        controls.addWidget(self.stop_work_button)
        return controls

    def _build_queue_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        self.queue_filter = QComboBox()
        self.queue_filter.addItems(("All", "Pending", "Active", "Review", "Completed", "Failed", "History"))
        self.queue_filter.currentIndexChanged.connect(self._populate_queue)
        actions.addWidget(QLabel("Show"))
        actions.addWidget(self.queue_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self._submit("snapshot"))
        actions.addWidget(self.refresh_button)
        self.process_button = QPushButton("Process selected submission")
        self.process_button.clicked.connect(self._process_selected)
        actions.addWidget(self.process_button)
        self.review_button = QPushButton("Review selected track")
        self.review_button.clicked.connect(self._review_selected)
        actions.addWidget(self.review_button)
        self.delivery_button = QPushButton("Preview delivery plans")
        self.delivery_button.clicked.connect(self._preview_deliveries)
        actions.addWidget(self.delivery_button)
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.clicked.connect(self._open_selected_output)
        actions.addWidget(self.open_output_button)
        layout.addLayout(actions)
        self.queue_table = self._table((
            "Track", "Playlist / source", "Stage", "Decision", "Peer",
            "Peer's own queue position", "Progress", "Action / error", "Output",
        ))
        self.queue_table.setAccessibleName("Queue of requested tracks")
        self.queue_table.itemSelectionChanged.connect(self._update_queue_actions)
        layout.addWidget(self.queue_table, 1)
        self.result_label = QLabel("No submission has run in this session.")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.progress_log = QPlainTextEdit()
        self.progress_log.setReadOnly(True)
        self.progress_log.setMaximumBlockCount(400)
        self.progress_log.setAccessibleName("Processing progress and results")
        self.progress_log.setMaximumHeight(150)
        layout.addWidget(self.progress_log)
        self._update_queue_actions()
        return page

    def _build_add_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Public playlist URL or tracklist file"))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Paste a public Spotify, Deezer, or YouTube playlist URL")
        self.source_edit.setAccessibleName("Playlist URL or tracklist file path")
        source_row.addWidget(self.source_edit, 1)
        self.source_file_button = QPushButton("Choose file…")
        self.source_file_button.clicked.connect(self._browse_source)
        source_row.addWidget(self.source_file_button)
        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self._preview_source)
        source_row.addWidget(self.preview_button)
        layout.addLayout(source_row)
        self.preview_summary = QLabel("Preview before adding work to the persistent queue.")
        self.preview_summary.setWordWrap(True)
        layout.addWidget(self.preview_summary)
        self.preview_table = self._table(("#", "Track", "Admission", "Work ID / held path"))
        layout.addWidget(self.preview_table, 1)
        self.enqueue_button = QPushButton("Add preview to queue")
        self.enqueue_button.setEnabled(False)
        self.enqueue_button.clicked.connect(self._enqueue_preview)
        layout.addWidget(self.enqueue_button, 0, Qt.AlignmentFlag.AlignRight)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QVBoxLayout(body)
        form.addWidget(self._build_credentials_group())
        form.addWidget(self._build_directories_group())
        form.addWidget(self._build_connectivity_group())
        form.addWidget(self._build_processing_group())
        form.addWidget(self._build_exports_group())
        self.settings_status = QLabel("Settings have not loaded.")
        self.settings_status.setWordWrap(True)
        self.settings_status.setAccessibleName("Saved and effective settings")
        form.addWidget(self.settings_status)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        save_row = QHBoxLayout()
        self.remember_secrets = QCheckBox("Remember secrets in the OS credential store")
        self.remember_secrets.setChecked(True)
        save_row.addWidget(self.remember_secrets, 1)
        save_row.addWidget(QLabel("Unchecked means session-only; secrets are not written to settings."))
        self.save_button = QPushButton("Validate and save settings")
        self.save_button.clicked.connect(self._save_settings)
        save_row.addWidget(self.save_button)
        layout.addLayout(save_row)
        return page

    def _build_credentials_group(self) -> QGroupBox:
        group = QGroupBox("Credentials")
        form = QFormLayout(group)
        self.username_edit = QLineEdit()
        form.addRow("Soulseek username (required)", self.username_edit)
        self.password_edit = self._secret_edit("Leave blank to keep the saved password")
        form.addRow("Soulseek password (required to connect)", self.password_edit)
        self.musicbrainz_edit = QLineEdit()
        self.musicbrainz_edit.setPlaceholderText("Required for MusicBrainz requests")
        form.addRow("MusicBrainz contact (required)", self.musicbrainz_edit)
        self.acoustid_edit = self._secret_edit("Required AcoustID key for fingerprint matching")
        form.addRow("AcoustID key (required)", self.acoustid_edit)
        self.discogs_edit = self._secret_edit("Optional Discogs token")
        form.addRow("Discogs token (optional)", self.discogs_edit)
        note = QLabel(
            "Stored secrets stay hidden. Blank secret fields retain current values. Processing also requires "
            "the bundled normalizer; missing prerequisites stop before acquisition."
        )
        note.setWordWrap(True)
        form.addRow(note)
        return group

    def _build_directories_group(self) -> QGroupBox:
        group = QGroupBox("Directories")
        layout = QVBoxLayout(group)
        form = QFormLayout()
        self.staging_edit = QLineEdit()
        form.addRow("Download / staging (required)", self._path_control(self.staging_edit))
        self.lossless_edit = QLineEdit()
        form.addRow("Finished lossless output (required)", self._path_control(self.lossless_edit))
        self.lossy_edit = QLineEdit()
        form.addRow("Finished lossy output (required)", self._path_control(self.lossy_edit))
        layout.addLayout(form)
        layout.addWidget(QLabel("Shared folders are explicit. Staging and private application data cannot be shared."))
        shares_row = QHBoxLayout()
        self.shares_list = QListWidget()
        self.shares_list.setAccessibleName("Explicit Soulseek shared folders")
        shares_row.addWidget(self.shares_list, 1)
        share_actions = QVBoxLayout()
        add_share = QPushButton("Add shared folder…")
        add_share.clicked.connect(self._add_share)
        share_actions.addWidget(add_share)
        remove_share = QPushButton("Remove selected share")
        remove_share.clicked.connect(self._remove_share)
        share_actions.addWidget(remove_share)
        share_actions.addStretch(1)
        shares_row.addLayout(share_actions)
        layout.addLayout(shares_row)
        return group

    def _build_connectivity_group(self) -> QGroupBox:
        group = QGroupBox("Connectivity")
        form = QFormLayout(group)
        self.server_host_edit = QLineEdit()
        self.server_host_edit.setPlaceholderText("Soulseek server host; blank uses the runtime default")
        self.server_port = QSpinBox()
        self.server_port.setRange(1, 65535)
        form.addRow("Server host", self.server_host_edit)
        form.addRow("Server port", self.server_port)
        self.listen_port = QSpinBox()
        self.listen_port.setRange(1, 65535)
        form.addRow("Peer listening port", self.listen_port)
        self.control_endpoint = QLabel()
        self.control_endpoint.setWordWrap(True)
        form.addRow("Private local control endpoint (never forward)", self.control_endpoint)
        self.upload_slots = QSpinBox()
        self.upload_slots.setRange(1, 1000)
        form.addRow("Upload slots", self.upload_slots)
        self.max_downloads = QSpinBox()
        self.max_downloads.setRange(1, 1000)
        form.addRow("Simultaneous downloads", self.max_downloads)
        return group

    def _build_processing_group(self) -> QGroupBox:
        group = QGroupBox("Processing and taxonomy")
        form = QFormLayout(group)
        self.workers = QSpinBox()
        self.workers.setRange(1, 1000)
        form.addRow("Track workers", self.workers)
        self.search_interval = QDoubleSpinBox()
        self.search_interval.setRange(0, 86400)
        self.search_interval.setDecimals(1)
        self.search_interval.setSuffix(" seconds")
        form.addRow("Search interval", self.search_interval)
        warning = QLabel("Lower search intervals may trigger Soulseek rate limits. Zero disables pacing.")
        warning.setWordWrap(True)
        form.addRow(warning)
        self.taxonomy_edit = QLineEdit()
        taxonomy_row = QHBoxLayout()
        taxonomy_row.addWidget(self.taxonomy_edit, 1)
        taxonomy_browse = QPushButton("Choose JSON…")
        taxonomy_browse.clicked.connect(self._browse_taxonomy)
        taxonomy_row.addWidget(taxonomy_browse)
        taxonomy_clear = QPushButton("Clear")
        taxonomy_clear.clicked.connect(self.taxonomy_edit.clear)
        taxonomy_row.addWidget(taxonomy_clear)
        form.addRow("Optional taxonomy", taxonomy_row)
        return group

    def _build_exports_group(self) -> QGroupBox:
        group = QGroupBox("Optional DJ collection exports")
        layout = QVBoxLayout(group)
        self.rekordbox_export = QCheckBox("Deliver to Rekordbox")
        layout.addWidget(self.rekordbox_export)
        layout.addWidget(QLabel("File output works without exports. Collection changes are previewed before delivery."))
        return group

    def _table(self, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _path_control(self, edit: QLineEdit) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("Browse…")
        button.clicked.connect(lambda: self._browse_directory(edit))
        row.addWidget(button)
        open_button = QPushButton("Open")
        open_button.clicked.connect(lambda: self._open_directory(edit.text()))
        row.addWidget(open_button)
        return container

    def _secret_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(placeholder)
        return edit

    def _submit(self, action: str, payload: dict[str, Any] | None = None) -> None:
        if self._busy or (self._closing and action != "shutdown"):
            return
        if action == "run":
            self.begin_run_requested.emit()
        self._busy = True
        self._active_action = action
        self._set_busy_state()
        self.command_requested.emit((action, payload or {}))

    def _set_busy_state(self) -> None:
        ready = self._config is not None and not self._closing
        self.tabs.setEnabled(ready)
        self.start_button.setEnabled(ready and not self._busy)
        self.stop_button.setEnabled(ready and not self._busy)
        self.pause_button.setEnabled(ready)
        self.pause_button.setText("Resume queue" if self._paused else "Pause queue")
        self.stop_work_button.setEnabled(ready and (self._busy and self._active_action == "run" or self._closing))
        self.refresh_status_button.setEnabled(ready and not self._busy)
        if hasattr(self, "process_button"):
            self._update_queue_actions()
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(ready and not self._busy)
        if hasattr(self, "preview_button"):
            self.preview_button.setEnabled(ready and not self._busy)
            self.enqueue_button.setEnabled(ready and not self._busy and self._preview is not None)

    def _on_complete(self, response: object) -> None:
        action = response["action"]
        self._busy = False
        self._active_action = ""
        if "error" in response:
            self._handle_error(response)
        else:
            self._handle_success(action, response.get("value"))
        self._set_busy_state()
        if self._closing:
            if action == "shutdown":
                self._shutdown_complete = True
                self._thread.quit()
            elif self._shutdown_requested:
                self._shutdown_requested = False
                self._submit("shutdown")

    def _handle_error(self, response: dict[str, Any]) -> None:
        action = response["action"]
        message = response["error"]
        if self._closing:
            self._set_status(f"Shutdown continuing after {action} failed: {message}")
        elif action == "initialize":
            self._set_status(f"Application could not load: {message}")
        elif action == "save" and response.get("error_type") == "CredentialStoreError":
            self._offer_session_only_save()
        else:
            QMessageBox.critical(self, "Operation failed", message)
            self._set_status(f"{action.replace('_', ' ').title()} failed: {message}")

    def _handle_success(self, action: str, value: Any) -> None:
        if action == "initialize":
            self._finish_initialization(value)
        elif action == "preview":
            self._show_preview(value)
        elif action == "enqueue":
            self._finish_enqueue(value)
        elif action in ("snapshot", "decide"):
            self._set_queue(value)
        elif action == "candidates":
            self._show_candidates(value)
        elif action == "delivery_plans":
            self._show_delivery_plans(value)
        elif action in ("run", "deliver"):
            self._finish_processing(action, value)
        elif action == "save":
            self._finish_settings_save(value)
        elif action.startswith("runtime_"):
            self._finish_runtime_action(value)
        elif action == "shutdown":
            self._set_status("Shutdown complete.")

    def _finish_initialization(self, value: dict[str, Any]) -> None:
        self._config = value["config"]
        self._set_settings(self._config)
        self._set_queue(value["queue"])
        self._show_runtime(value["runtime"])
        credential_error = value["credential_error"]
        if credential_error:
            self.remember_secrets.setChecked(False)
            self.settings_status.setText(
                f"{self.settings_status.text()}\nNative credential storage unavailable: {credential_error}. "
                "Saving secrets requires an explicit session-only choice."
            )
        self._set_status("Ready. Configure required credentials before processing; no network work starts automatically.")
        if not (self._config.soulseek_username and self._config.musicbrainz_contact):
            self.tabs.setCurrentWidget(self.tabs.widget(2))
            self._set_status(
                "First run: Soulseek username, MusicBrainz contact, and AcoustID key are required before processing."
            )

    def _finish_enqueue(self, value: dict[str, Any]) -> None:
        self._set_queue(value["queue"])
        self._preview = None
        self.enqueue_button.setEnabled(False)
        summary = self._admission_summary
        self.preview_summary.setText(
            f"Submission {value['job_id']} was added. {summary} Start processing explicitly."
        )
        self._set_status(f"Added submission {value['job_id']} to the queue.")

    def _set_queue(self, rows: list[Any]) -> None:
        self._queue = rows
        self._populate_queue()

    def _finish_processing(self, action: str, value: dict[str, Any]) -> None:
        self._set_queue(value["queue"])
        self._show_result(value["result"], action, value["job_id"])

    def _finish_settings_save(self, value: dict[str, Any]) -> None:
        self._config = value["config"]
        self._set_settings(self._config)
        if value["runtime"] is not None:
            self._show_runtime(value["runtime"])
        runtime_error = value["runtime_error"]
        if runtime_error:
            self.settings_status.setText(f"Settings saved. Runtime apply failed: {runtime_error}")
        self._set_status("Settings saved. Restart Soulseek explicitly if the status reports pending changes.")

    def _finish_runtime_action(self, status: Any) -> None:
        self._show_runtime(status)
        self._set_status(f"Soulseek state: {status.state}.")

    def _on_progress(self, progress: object) -> None:
        stage = getattr(progress, "stage", "Working")
        message = getattr(progress, "message", str(progress))
        job_id = getattr(progress, "job_id", "")
        idx = getattr(progress, "idx", None)
        location = f"{job_id} · track {idx}" if job_id and idx is not None else job_id
        line = " · ".join(part for part in (str(stage), location, str(message)) if part)
        self._set_status(line)
        self.progress_log.appendPlainText(line)

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _show_runtime(self, status: Any) -> None:
        self.runtime_label.setText(f"Soulseek: {status.state}")
        saved = status.saved
        effective = status.effective
        restart = ", ".join(status.restart_required) or "none"
        deferred = ", ".join(getattr(status, "deferred", ())) or "none"
        download = (
            f"Download folder saved: {getattr(saved, 'download_dir', '')}; "
            f"effective: {getattr(effective, 'download_dir', '')}"
        )
        shares = "; ".join(status.effective.shared_dirs or ()) or "none"
        reindex = "in progress" if status.reindexing else "idle or complete"
        error = status.error or getattr(status, "session_loss", None) or "none"
        self.settings_status.setText(
            f"{download}\nSaved: {saved}\nEffective: {effective}\nRestart required: {restart}\n"
            f"Deferred changes: {deferred}\nEffective shared folders: {shares}\n"
            f"Share reindexing: {reindex}\nRuntime error or session loss: {error}"
        )

    def _set_settings(self, config: Any) -> None:
        self.staging_edit.setText(str(config.staging))
        self.lossless_edit.setText(str(config.lossless))
        self.lossy_edit.setText(str(config.lossy))
        self.shares_list.clear()
        for path in config.shared_dirs or ():
            self.shares_list.addItem(str(path))
        server = config.soulseek_server or ""
        host, separator, port = server.rpartition(":")
        self.server_host_edit.setText(host if separator else server)
        self.server_port.setValue(int(port) if separator and port.isdecimal() else 2242)
        self.listen_port.setValue(config.soulseek_listen_port)
        self.upload_slots.setValue(config.upload_slots)
        self.max_downloads.setValue(config.max_downloads)
        self.workers.setValue(config.workers)
        self.search_interval.setValue(config.search_interval)
        self.username_edit.setText(config.soulseek_username)
        self.musicbrainz_edit.setText(config.musicbrainz_contact)
        self.password_edit.clear()
        self.acoustid_edit.clear()
        self.discogs_edit.clear()
        self.taxonomy_edit.setText(str(config.taxonomy) if config.taxonomy else "")
        targets = set(config.export_targets)
        self.rekordbox_export.setChecked("rekordbox" in targets)
        self.control_endpoint.setText(config.daemon_address or slsk.managed_address(config)
                                      or "Managed locally on loopback")

    def _build_config(self) -> Any:
        secrets = dict(self._config.secrets)
        for name, edit in (
            (self._config.slsk_password_env, self.password_edit),
            (self._config.acoustid_key_env, self.acoustid_edit),
            (self._config.discogs_token_env, self.discogs_edit),
        ):
            if edit.text():
                secrets[name] = edit.text()
        host = self.server_host_edit.text().strip()
        server = f"{host}:{self.server_port.value()}" if host else ""
        taxonomy = Path(self.taxonomy_edit.text()).expanduser().resolve() if self.taxonomy_edit.text().strip() else None
        shares = tuple(
            Path(self.shares_list.item(index).text()).expanduser().resolve()
            for index in range(self.shares_list.count())
        )
        targets = settings.merged_targets(self._config.export_targets,
                                          self.rekordbox_export.isChecked())
        return replace(
            self._config,
            staging=self._required_path(self.staging_edit, "Download / staging folder"),
            lossless=self._required_path(self.lossless_edit, "Finished lossless folder"),
            lossy=self._required_path(self.lossy_edit, "Finished lossy folder"),
            shared_dirs=shares or None,
            soulseek_server=server,
            soulseek_listen_port=self.listen_port.value(),
            upload_slots=self.upload_slots.value(),
            max_downloads=self.max_downloads.value(),
            workers=self.workers.value(),
            search_interval=self.search_interval.value(),
            soulseek_username=self.username_edit.text().strip(),
            musicbrainz_contact=self.musicbrainz_edit.text().strip(),
            taxonomy=taxonomy,
            export_targets=targets,
            secrets=secrets,
        )

    def _required_path(self, edit: QLineEdit, label: str) -> Path:
        value = edit.text().strip()
        if not value:
            raise ValueError(f"{label} is required.")
        return Path(value).expanduser().resolve()

    def _save_settings(self) -> None:
        if self._config is None:
            return
        try:
            config = self._build_config()
        except (TypeError, ValueError) as error:
            QMessageBox.warning(self, "Invalid settings", str(error))
            return
        self._pending_config = config
        self._pending_remember = self.remember_secrets.isChecked()
        self._submit("save", {"config": config, "remember": self._pending_remember})

    def _offer_session_only_save(self) -> None:
        answer = QMessageBox.question(
            self,
            "Credential storage unavailable",
            "Native credential storage failed. Save nonsecret settings and keep entered secrets only for this session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._submit("save", {"config": self._pending_config, "remember": False})
        else:
            self._set_status("Settings were not saved. Secrets were not written to a plaintext file.")

    def _browse_directory(self, edit: QLineEdit) -> None:
        current = edit.text() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Choose folder", current)
        if selected:
            edit.setText(selected)

    def _browse_source(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose tracklist", str(Path.home()), "Tracklists (*.txt *.csv);;All files (*)"
        )
        if selected:
            self.source_edit.setText(selected)

    def _browse_taxonomy(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Choose genre taxonomy", str(Path.home()), "JSON files (*.json);;All files (*)"
        )
        if selected:
            self.taxonomy_edit.setText(selected)

    def _add_share(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Add explicit shared folder", str(Path.home()))
        if selected and not self._share_exists(selected):
            self.shares_list.addItem(selected)

    def _share_exists(self, path: str) -> bool:
        return any(self.shares_list.item(index).text() == path for index in range(self.shares_list.count()))

    def _remove_share(self) -> None:
        row = self.shares_list.currentRow()
        if row >= 0:
            self.shares_list.takeItem(row)

    def _preview_source(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.information(self, "Playlist source required", "Enter a public playlist URL or choose a tracklist file.")
            return
        self._submit("preview", {"source": source})

    def _show_preview(self, preview: Any) -> None:
        self._preview = preview
        self.preview_table.setRowCount(0)
        matches = {match.idx: match for match in preview.matches}
        held = getattr(preview, "held", {})
        held_indices = set(held)
        accepted = sum(1 for match in preview.matches if match.created and match.idx not in held_indices)
        linked = sum(1 for match in preview.matches if match.existing and not match.created and match.idx not in held_indices)
        held_count = len(held_indices)
        unreported = max(0, len(preview.records) - accepted - linked - held_count)
        self._admission_summary = (
            f"New work accepted: {accepted}; linked to existing work: {linked}; "
            f"already held: {held_count}; admission not reported: {unreported}."
        )
        for index, record in enumerate(preview.records):
            match = matches.get(record.idx)
            held_path = held.get(record.idx)
            if held_path:
                admission, work_id = "Already held", str(held_path)
            elif match is None:
                admission, work_id = "Admission not reported", ""
            elif match.created:
                admission, work_id = "Accepted as new work", str(match.work_id)
            elif match.existing:
                admission, work_id = "Linked to existing work", str(match.work_id)
            else:
                admission, work_id = "Admission not reported", str(match.work_id or "")
            self._insert_row(self.preview_table, index, (str(record.idx), record.label, admission, work_id))
        self.preview_summary.setText(
            f"{preview.name} · {preview.source} · {len(preview.records)} entries. "
            f"{self._admission_summary} Review before adding."
        )
        self.enqueue_button.setEnabled(bool(preview.records))

    def _enqueue_preview(self) -> None:
        if self._preview is not None:
            self._submit("enqueue", {"preview": self._preview})

    def _insert_row(self, table: QTableWidget, row: int, values: tuple[str, ...]) -> None:
        table.insertRow(row)
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(str(value)))

    def _populate_queue(self) -> None:
        if not hasattr(self, "queue_table"):
            return
        selected_filter = self.queue_filter.currentText().lower()
        rows = [row for row in self._queue if self._queue_filter_matches(row, selected_filter)]
        values = [
            (
                str(row.label), str(getattr(row, "source", "")), str(row.status), str(row.decision),
                str(getattr(row, "peer", "")), self._peer_queue_value(row), str(getattr(row, "progress", "")),
                str(row.notes), str(row.local_path or ""),
            )
            for row in rows
        ]
        if values == self._rendered_queue:
            return
        self._rendered_queue = values
        chosen = {
            self.queue_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in self.queue_table.selectionModel().selectedRows()
            if self.queue_table.item(index.row(), 0) is not None
        }
        self.queue_table.setRowCount(0)
        for index, (row, cells) in enumerate(zip(rows, values)):
            self._insert_row(self.queue_table, index, cells)
            self.queue_table.item(index, 0).setData(Qt.ItemDataRole.UserRole, (row.job_id, row.idx))
            if (row.job_id, row.idx) in chosen:
                self.queue_table.selectRow(index)
        self._update_queue_actions()

    def _queue_filter_matches(self, row: Any, selected_filter: str) -> bool:
        if selected_filter == "all":
            return True
        category = self._queue_category(row)
        if selected_filter == "history":
            return category in ("completed", "failed")
        return category == selected_filter

    def _queue_category(self, row: Any) -> str:
        status = str(row.status).casefold()
        decision = str(row.decision).casefold()
        if status in FAILED_STATUSES:
            return "failed"
        if status in FINISHED_STATUSES:
            return "completed"
        if getattr(row, "needs_review", False) or (status in LIVE_STATUSES and decision == "undecided"):
            return "review"
        if status in ACTIVE_STATUSES:
            return "active"
        if getattr(row, "peer", "") or getattr(row, "progress", ""):
            return "active"
        return "pending"

    def _peer_queue_value(self, row: Any) -> str:
        position = getattr(row, "peer_queue", None)
        return "" if position is None else str(position)

    def _selected_key(self) -> tuple[str, int] | None:
        selected = self.queue_table.selectionModel().selectedRows()
        if not selected:
            return None
        item = self.queue_table.item(selected[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_queue_actions(self) -> None:
        if not hasattr(self, "process_button"):
            return
        key = self._selected_key()
        enabled = self._config is not None and not self._busy and not self._closing
        has_selection = enabled and key is not None
        self.process_button.setEnabled(has_selection)
        self.review_button.setEnabled(has_selection)
        self.delivery_button.setEnabled(has_selection)
        self.open_output_button.setEnabled(has_selection)
        self.refresh_button.setEnabled(enabled)

    def _process_selected(self) -> None:
        key = self._selected_key()
        if key:
            self._submit("run", {"job_id": key[0]})

    def _review_selected(self) -> None:
        key = self._selected_key()
        if key:
            self._review_key = key
            self._submit("candidates", {"job_id": key[0], "idx": key[1]})

    def _show_candidates(self, candidates: list[Any]) -> None:
        if not candidates:
            row = self._selected_queue_row()
            status = str(row.status) if row else "unknown"
            notes = str(row.notes) if row else ""
            QMessageBox.information(
                self, "No review candidate", f"This track has no candidate requiring a decision. State: {status}. {notes}"
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Review transfer candidates")
        dialog.resize(850, 420)
        layout = QVBoxLayout(dialog)
        table = self._table(("#", "User", "Remote path", "Format", "Selection / rejection reason", "Label"))
        for index, candidate in enumerate(candidates):
            self._insert_row(table, index, (
                str(candidate.number), str(candidate.user), str(candidate.path), str(candidate.format),
                str(candidate.reason), str(candidate.label),
            ))
            table.item(index, 0).setData(Qt.ItemDataRole.UserRole, candidate)
        layout.addWidget(table, 1)
        buttons = QHBoxLayout()
        accept = QPushButton("Accept selected candidate")
        skip = QPushButton("Skip track")
        not_found = QPushButton("Not found")
        cancel = QPushButton("Cancel")
        buttons.addWidget(accept)
        buttons.addWidget(skip)
        buttons.addWidget(not_found)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        accept.clicked.connect(lambda: self._candidate_decision(dialog, table, "accept"))
        skip.clicked.connect(lambda: self._submit_decision(dialog, "skip"))
        not_found.clicked.connect(lambda: self._submit_decision(dialog, "not_found"))
        cancel.clicked.connect(dialog.reject)
        dialog.exec()

    def _candidate_decision(self, dialog: QDialog, table: QTableWidget, action: str) -> None:
        selected = table.selectionModel().selectedRows()
        if not selected:
            QMessageBox.information(dialog, "Select a candidate", "Choose a candidate row before accepting it.")
            return
        candidate = table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self._submit_decision(dialog, action, candidate.number)

    def _submit_decision(self, dialog: QDialog, action: str, candidate: Any = None) -> None:
        if self._review_key is None:
            dialog.reject()
            return
        job_id, idx = self._review_key
        dialog.accept()
        self._submit("decide", {"job_id": job_id, "idx": idx, "action": action, "candidate": candidate})

    def _selected_queue_row(self) -> Any:
        key = self._selected_key()
        return next((row for row in self._queue if (row.job_id, row.idx) == key), None)

    def _preview_deliveries(self) -> None:
        key = self._selected_key()
        if key:
            self._submit("delivery_plans", {"job_id": key[0]})

    def _show_delivery_plans(self, plans: list[Any]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Delivery plan preview")
        dialog.resize(760, 360)
        layout = QVBoxLayout(dialog)
        table = self._table(("Target", "Destination", "Planned changes", "Requires confirmation", "Blocker"))
        needs_confirmation = False
        for index, plan in enumerate(plans):
            needs_confirmation = needs_confirmation or bool(plan.requires_confirmation)
            self._insert_row(table, index, (
                str(plan.target), str(plan.destination), str(plan.changes),
                "Yes" if plan.requires_confirmation else "No", str(plan.error or ""),
            ))
        layout.addWidget(QLabel(
            "Review every target and nonzero change before writing to a collection."
            if plans else "No collection targets are enabled. Finished files remain in the output folders."
        ))
        confirmation = QCheckBox("I reviewed the plan and authorize configured deliveries")
        confirmation.setChecked(not needs_confirmation)
        layout.addWidget(confirmation)
        buttons = QHBoxLayout()
        deliver = QPushButton("Deliver configured targets")
        deliver.setEnabled(bool(plans) and confirmation.isChecked())
        confirmation.toggled.connect(
            lambda checked: deliver.setEnabled(bool(plans) and checked)
        )
        cancel = QPushButton("Cancel")
        buttons.addWidget(deliver)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        deliver.clicked.connect(lambda: self._deliver_from_plan(dialog))
        cancel.clicked.connect(dialog.reject)
        dialog.exec()

    def _deliver_from_plan(self, dialog: QDialog) -> None:
        key = self._selected_key()
        if key:
            dialog.accept()
            self._submit("deliver", {"job_id": key[0]})

    def _show_result(self, result: Any, action: str, job_id: str) -> None:
        if action == "deliver":
            lines = self._delivery_result_lines(result)
        else:
            missing_genres = ", ".join(result.ungenred) or "none"
            pending_targets = ", ".join(result.delivery_pending) or "none reported"
            lines = [
                f"Usable files: {result.completed}",
                f"Already-held work: {result.already_held}",
                f"Outstanding requests: {result.outstanding}",
                f"Review actions: {result.review}",
                f"Failed work: {result.failed}",
                f"Missing genres: {missing_genres}",
                f"Destination delivery pending: {pending_targets}",
            ]
        text = "\n".join(lines)
        title = "Delivery result" if action == "deliver" else "Processing result"
        self.result_label.setText(f"{title}\n{text}")
        self.progress_log.appendPlainText(text)
        if not self._closing:
            QMessageBox.information(self, title, text)
        output = next((row.local_path for row in self._queue if row.job_id == job_id and row.local_path), "")
        if output:
            self._last_output_dir = Path(output).parent

    def _delivery_result_lines(self, outcomes: list[Any]) -> list[str]:
        lines = []
        for outcome in outcomes:
            verdict = "Delivered" if outcome.success else "Failed"
            lines.append(f"{outcome.target}: {verdict}. {outcome.message}")
            for artifact in getattr(outcome, "artifacts", ()):
                lines.append(f"Track {artifact.idx}: {artifact.status}. {artifact.message}")
        return lines or ["No configured delivery targets returned an outcome."]

    def _open_selected_output(self) -> None:
        row = self._selected_queue_row()
        if row is not None and row.local_path:
            self._open_directory(str(Path(row.local_path).parent))
        elif self._last_output_dir is not None:
            self._open_directory(str(self._last_output_dir))
        else:
            QMessageBox.information(self, "No output file", "The selected queue item has no finished output path.")

    def _open_directory(self, value: str) -> None:
        path = Path(value).expanduser()
        if not path.exists():
            QMessageBox.warning(self, "Folder unavailable", f"Folder does not exist: {path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "Could not open folder", str(path))

    def _toggle_pause(self) -> None:
        if self._config is None:
            return
        self._paused = not self._paused
        self.pause_requested.emit(self._paused)
        self._set_busy_state()
        self._set_status("Queue paused; no new work will start." if self._paused else "Queue resumed.")

    def _request_work_stop(self) -> None:
        self.stop_requested.emit()
        self._set_status("Stop requested. Current work will stop at a safe boundary.")

    def closeEvent(self, event: Any) -> None:
        if self._shutdown_complete:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        self._closing = True
        self.tabs.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        self.stop_work_button.setEnabled(False)
        self._set_status("Shutting down owned work and Soulseek. The window will close when shutdown finishes.")
        if self._busy:
            self._shutdown_requested = True
            self.stop_requested.emit()
        else:
            self._submit("shutdown")

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._shutdown_complete:
            self.close()


def _run_normalizer(argv: list[str]) -> int:
    """Run the shipped normalizer in this process: a frozen build has no interpreter to spawn."""
    import importlib.util

    script = Path(argv[0])
    spec = importlib.util.spec_from_file_location("loudness_normalizer", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot read the normalizer at {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [str(script), *argv[1:]]
    return module.main() or 0


def main() -> int:
    if sys.argv[1:2] == ["--normalize"]:
        return _run_normalizer(sys.argv[2:])
    application = QApplication(sys.argv)
    application.setApplicationName("Music Agent")
    application.setApplicationDisplayName("Music Agent")
    window = DesktopWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
