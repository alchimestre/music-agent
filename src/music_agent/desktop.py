from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtQuickWidgets import QQuickWidget
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
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import settings, slsk, state
from .config import DEFAULTS, listen_port
from .resources import expose_payload_on_path

FAILED_STATUSES = frozenset({state.FAILED, state.NOT_FOUND})
ACTIVE_STATUSES = frozenset({state.DOWNLOADED, state.CONVERTED, state.NORMALIZED})
STOPPED_DECISIONS = frozenset({state.SKIPPED, state.DECLINED})
DEFAULT_SERVER_PORT = 2242
QUEUE_ROLES = {
    role: name.encode()
    for role, name in enumerate(
        ("trackName", "trackSource", "stage", "peer", "peerQueue", "percent", "readout", "notes", "nextStep"),
        Qt.ItemDataRole.UserRole + 1,
    )
}

# Where each pipeline position sits on the bar. A transfer in flight fills the band between a search that
# found a peer and a file that landed, so the bar moves during the download rather than after it.
STAGE_PERCENT = {
    state.PENDING: 0, state.RETRY_LATER: 0, state.DOWNLOADED: 25, state.CONVERTED: 45,
    state.NORMALIZED: 70, state.TAGGED: 90, state.IMPORTED: 100, state.FOUND_IN_LIBRARY: 100,
    state.FAILED: 100, state.NOT_FOUND: 100,
}
STAGE_LABEL = {
    state.PENDING: "Searching", state.RETRY_LATER: "Retrying", state.DOWNLOADED: "Downloaded",
    state.CONVERTED: "Converted", state.NORMALIZED: "Normalized", state.TAGGED: "Tagged",
    state.IMPORTED: "Imported", state.FOUND_IN_LIBRARY: "In collection",
    state.FAILED: "Failed", state.NOT_FOUND: "Not found",
}

# Each queue reason appears once here; the stage label and the next step both read this, so adding a
# reason the pipeline reports needs one entry rather than a second branch.
QUEUE_REASON_TEXT = {
    "review": ("Needs a decision",
               "Needs a decision. Use the CLI review command with the desktop settings file."),
    "not_searched": ("Waiting to search", "This track has not been searched yet."),
    "nothing_offered": ("No offers", "No peer offered this track. The queue can search again."),
    "nothing_usable": ("No usable offers", "No usable offer. The queue can search again."),
}

# A transfer is the band between "a peer was found" and "the file landed".
TRANSFER_FLOOR = 5
TRANSFER_SPAN = 20

AUTO_FIRST_DELAY = 10.0
AUTO_MAX_DELAY = 900.0


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
        if action == "run":
            return self._run(payload)
        if action == "delivery_plans":
            job_id = payload["job_id"]
            return job_id, self.application.delivery_plans(job_id)
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

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = payload["job_id"]
        last_snapshot = [0.0]

        def emit_progress(event: object) -> None:
            self.progress.emit(event)
            if time.monotonic() - last_snapshot[0] >= 2.0:
                last_snapshot[0] = time.monotonic()
                self.queue_changed.emit((job_id, self.application.snapshot(job_id)))
            if self._stop_requested.is_set():
                self.application.stop_event.set()

        result = self.application.run(job_id, emit=emit_progress)
        return {
            "job_id": job_id,
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
        self._last_output_dir: Path | None = None
        self._admission_summary = ""
        self._queue_by_key: dict[tuple[str, int], Any] = {}
        self._rendered_keys: list[tuple[str, int]] = []
        self._queue_items: dict[tuple[str, int], QStandardItem] = {}
        self._transfer_fraction: dict[tuple[str, int], float] = {}
        self._auto_delay = AUTO_FIRST_DELAY
        self._auto_next = 0.0
        self._auto_running = False
        self._auto_before: dict[int, int] = {}
        self._auto_last_job: str | None = None
        self._plan_open = False
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(2000)
        self._auto_timer.timeout.connect(self._auto_advance)
        self._pending_config: Any = None
        self._build_window()
        self._busy = True
        self._active_action = "initialize"
        self._set_busy_state()
        self._set_status("Loading saved settings and offline queue…")
        self._start_worker()
        self._auto_timer.start()

    def _start_worker(self) -> None:
        self._thread = QThread(self)
        self._worker = _ApplicationWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.initialize)
        self.command_requested.connect(self._worker.execute)
        self._worker.queue_changed.connect(self._set_job_queue)
        self.pause_requested.connect(self._worker.set_paused, Qt.ConnectionType.DirectConnection)
        self.stop_requested.connect(self._worker.request_stop, Qt.ConnectionType.DirectConnection)
        self._worker.completed.connect(self._on_complete)
        self.begin_run_requested.connect(self._worker.begin_run, Qt.ConnectionType.DirectConnection)
        self._worker.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _build_window(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._build_controls())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_queue_page(), "Queue")
        self.tabs.addTab(self._build_add_page(), "Add playlist")
        self.tabs.addTab(self._build_settings_page(), "Settings")
        layout.addWidget(self.tabs, 1)
        self.status_label = QLabel("Loading application…")
        self.status_label.setAccessibleName("Application status")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

    def _build_controls(self) -> QWidget:
        runtime_group = QGroupBox("Soulseek runtime")
        controls = QHBoxLayout(runtime_group)
        controls.setContentsMargins(12, 12, 12, 12)
        controls.setSpacing(8)
        self.runtime_label = QLabel("Soulseek: checking status")
        self.runtime_label.setAccessibleName("Soulseek connection status")
        self.runtime_label.setMinimumWidth(160)
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
        return runtime_group

    def _build_queue_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addLayout(self._queue_view_controls())
        layout.addWidget(self._queue_work_controls())
        layout.addWidget(self._selected_track_controls())
        self.queue_state_label = QLabel()
        self.queue_state_label.setWordWrap(True)
        self.queue_state_label.setAccessibleName("Queue selection and empty state")
        layout.addWidget(self.queue_state_label)
        self.queue_model = QStandardItemModel(self)
        self.queue_model.setItemRoleNames(QUEUE_ROLES)
        self.queue_view = QQuickWidget()
        self.queue_view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.queue_view.rootContext().setContextProperty("queueModel", self.queue_model)
        qml = Path(__file__).with_name("QueueView.qml")
        self.queue_view.setSource(QUrl.fromLocalFile(str(qml)))
        self.queue_root = self.queue_view.rootObject()
        if self.queue_root is None:
            errors = "\n".join(error.toString() for error in self.queue_view.errors())
            raise RuntimeError(f"Cannot load QueueView.qml:\n{errors}")
        self.queue_root.selectedIndexChanged.connect(self._update_queue_actions)
        self.queue_root.previewRequested.connect(self._preview_from_queue)
        layout.addWidget(self.queue_view, 1)
        self._add_activity_controls(layout)
        self._update_queue_actions()
        return page

    def _queue_view_controls(self) -> QHBoxLayout:
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.queue_filter = QComboBox()
        self.queue_filter.addItems(("All", "Pending", "Active", "Completed", "Failed", "History"))
        self.queue_filter.currentIndexChanged.connect(self._populate_queue)
        filter_label = QLabel("Show")
        filter_label.setBuddy(self.queue_filter)
        controls.addWidget(filter_label)
        controls.addWidget(self.queue_filter)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(lambda: self._submit("snapshot"))
        controls.addWidget(self.refresh_button)
        controls.addStretch(1)
        return controls

    def _queue_work_controls(self) -> QGroupBox:
        group = QGroupBox("Queue actions")
        controls = QHBoxLayout(group)
        controls.setContentsMargins(12, 12, 12, 12)
        controls.setSpacing(8)
        self.pause_button = QPushButton("Pause queue")
        self.pause_button.clicked.connect(self._toggle_pause)
        controls.addWidget(self.pause_button)
        self.stop_work_button = QPushButton("Stop current work")
        self.stop_work_button.clicked.connect(self._request_work_stop)
        controls.addWidget(self.stop_work_button)
        controls.addStretch(1)
        return group

    def _selected_track_controls(self) -> QGroupBox:
        group = QGroupBox("Selected track")
        controls = QHBoxLayout(group)
        controls.setContentsMargins(12, 12, 12, 12)
        controls.setSpacing(8)
        self.process_button = QPushButton("Process submission")
        self.process_button.setAccessibleName("Process selected submission")
        self.process_button.setToolTip("Process the submission containing the selected track.")
        self.process_button.clicked.connect(self._process_selected)
        controls.addWidget(self.process_button)
        self.delivery_button = QPushButton("Preview delivery")
        self.delivery_button.setAccessibleName("Preview delivery plans for selected submission")
        self.delivery_button.clicked.connect(self._preview_deliveries)
        controls.addWidget(self.delivery_button)
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.setAccessibleName("Open selected track output folder")
        self.open_output_button.clicked.connect(self._open_selected_output)
        controls.addWidget(self.open_output_button)
        controls.addStretch(1)
        return group

    def _add_activity_controls(self, layout: QVBoxLayout) -> None:
        self.result_label = QLabel("No submission has run in this session.")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.activity_button = QPushButton("Show activity")
        self.activity_button.setCheckable(True)
        self.activity_button.setAccessibleName("Show or hide processing activity")
        self.activity_button.setVisible(False)
        self.activity_button.toggled.connect(self._toggle_activity)
        layout.addWidget(self.activity_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.progress_log = QPlainTextEdit()
        self.progress_log.setReadOnly(True)
        self.progress_log.setMaximumBlockCount(400)
        self.progress_log.setAccessibleName("Processing progress and results")
        self.progress_log.setMaximumHeight(112)
        self.progress_log.hide()
        layout.addWidget(self.progress_log)

    def _build_add_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_label = QLabel("Public playlist URL or tracklist file")
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Paste a public Spotify, Deezer, or YouTube playlist URL")
        self.source_edit.setAccessibleName("Playlist URL or tracklist file path")
        source_label.setBuddy(self.source_edit)
        source_row.addWidget(source_label)
        source_row.addWidget(self.source_edit, 1)
        self.source_file_button = QPushButton("Choose file…")
        self.source_file_button.clicked.connect(self._browse_source)
        source_row.addWidget(self.source_file_button)
        layout.addLayout(source_row)
        preview_actions = QHBoxLayout()
        self.preview_summary = QLabel("Preview before adding work to the persistent queue.")
        self.preview_summary.setWordWrap(True)
        preview_actions.addWidget(self.preview_summary, 1)
        self.preview_button = QPushButton("Preview playlist")
        self.preview_button.setAccessibleName("Preview playlist before adding work")
        self.preview_button.clicked.connect(self._preview_source)
        preview_actions.addWidget(self.preview_button)
        layout.addLayout(preview_actions)
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
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        self.settings_tabs = QTabWidget()
        for title, group in (
            ("Credentials", self._build_credentials_group()),
            ("Folders", self._build_directories_group()),
            ("Connection", self._build_connectivity_group()),
            ("Processing", self._build_processing_group()),
            ("Delivery", self._build_exports_group()),
        ):
            self.settings_tabs.addTab(self._settings_category(group), title)
        layout.addWidget(self.settings_tabs, 1)
        layout.addWidget(self._settings_footer())
        return page

    def _settings_footer(self) -> QWidget:
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        self.settings_status = QLabel("Settings have not loaded.")
        self.settings_status.setWordWrap(True)
        self.settings_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.settings_status.setAccessibleName("Saved and effective settings status")
        footer_layout.addWidget(self.settings_status)
        details_button = QPushButton("Connection details")
        details_button.setCheckable(True)
        details_button.setAccessibleName("Show or hide connection diagnostics")
        footer_layout.addWidget(details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.settings_details = QPlainTextEdit()
        self.settings_details.setReadOnly(True)
        self.settings_details.setMaximumHeight(112)
        self.settings_details.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.settings_details.setAccessibleName("Saved and effective runtime settings details")
        self.settings_details.hide()
        details_button.toggled.connect(self.settings_details.setVisible)
        footer_layout.addWidget(self.settings_details)
        remember_note = QLabel("Unchecked means session-only; secrets are not written to settings.")
        remember_note.setWordWrap(True)
        footer_layout.addWidget(remember_note)
        save_row = QHBoxLayout()
        self.remember_secrets = QCheckBox("Remember secrets in the OS credential store")
        self.remember_secrets.setChecked(True)
        save_row.addWidget(self.remember_secrets, 1)
        self.save_button = QPushButton("Validate and save settings")
        self.save_button.clicked.connect(self._save_settings)
        save_row.addWidget(self.save_button)
        footer_layout.addLayout(save_row)
        return footer

    def _build_credentials_group(self) -> QGroupBox:
        group = QGroupBox("Credentials")
        form = QFormLayout(group)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self._soulseek_fields(form)
        self._metadata_fields(form)
        note = QLabel(
            "Blank secret fields retain current values. Stored secrets stay hidden. Processing also requires "
            "the bundled normalizer; missing prerequisites stop before acquisition."
        )
        note.setWordWrap(True)
        form.addRow(note)
        return group

    def _soulseek_fields(self, form: QFormLayout) -> None:
        self.username_edit = QLineEdit()
        username_help = QLabel(
            "Use SoulseekQt to set up or confirm the network login used here. This app cannot create network accounts."
        )
        username_help.setWordWrap(True)
        form.addRow(username_help)
        self.soulseek_setup_button = QPushButton("Soulseek client setup")
        self.soulseek_setup_button.setAccessibleName("Open official Soulseek client download page")
        self.soulseek_setup_button.setToolTip("Official Soulseek client downloads and setup information.")
        self.soulseek_setup_button.clicked.connect(
            lambda: self._open_external_url("https://www.slsknet.org/news/node/1")
        )
        form.addRow("Soulseek username (required)", self._linked_field(
            self.username_edit, self.soulseek_setup_button
        ))
        self.password_edit = self._secret_edit("Leave blank to keep the saved password")
        form.addRow("Soulseek password (required to connect)", self.password_edit)

    def _metadata_fields(self, form: QFormLayout) -> None:
        self.musicbrainz_edit = QLineEdit()
        self.musicbrainz_edit.setPlaceholderText("Contact email or URL; not an API key")
        musicbrainz_button = QPushButton("API contact guidance")
        musicbrainz_button.setAccessibleName("Read official MusicBrainz API contact guidance")
        musicbrainz_button.setToolTip("MusicBrainz asks API clients to include a maintainer contact.")
        musicbrainz_button.clicked.connect(
            lambda: self._open_external_url("https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting")
        )
        form.addRow("MusicBrainz contact (required)", self._linked_field(
            self.musicbrainz_edit, musicbrainz_button
        ))
        self.acoustid_edit = self._secret_edit("Required for fingerprint matching")
        acoustid_button = QPushButton("Create AcoustID key")
        acoustid_button.setAccessibleName("Create an official AcoustID application key")
        acoustid_button.setToolTip("Register this application with AcoustID to receive an application API key.")
        acoustid_button.clicked.connect(
            lambda: self._open_external_url("https://acoustid.org/new-application")
        )
        form.addRow("AcoustID application key (required)", self._linked_field(
            self.acoustid_edit, acoustid_button
        ))
        self.discogs_edit = self._secret_edit("Optional Discogs token")
        discogs_button = QPushButton("Create Discogs token")
        discogs_button.setAccessibleName("Create an optional Discogs API token")
        discogs_button.setToolTip("Open Discogs developer settings and generate a personal access token.")
        discogs_button.clicked.connect(
            lambda: self._open_external_url("https://www.discogs.com/settings/developers")
        )
        form.addRow("Discogs token (optional)", self._linked_field(
            self.discogs_edit, discogs_button
        ))

    def _build_directories_group(self) -> QGroupBox:
        group = QGroupBox("Directories")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.staging_edit = QLineEdit()
        form.addRow("Download / staging (required)", self._path_control(self.staging_edit))
        self.lossless_edit = QLineEdit()
        form.addRow("Finished lossless output (required)", self._path_control(self.lossless_edit))
        self.lossy_edit = QLineEdit()
        form.addRow("Finished lossy output (required)", self._path_control(self.lossy_edit))
        layout.addLayout(form)
        share_help = QLabel(
            "Finished output folders are shared by default. Add folders to replace that default. "
            "Staging and private application data cannot be shared."
        )
        share_help.setWordWrap(True)
        layout.addWidget(share_help)
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
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.server_host_edit = QLineEdit()
        self.server_host_edit.setPlaceholderText("Soulseek server host; blank uses the runtime default")
        self.server_port = QSpinBox()
        self.server_port.setRange(1, 65535)
        form.addRow("Server host", self.server_host_edit)
        form.addRow("Server port", self.server_port)
        self.listen_port = QSpinBox()
        self.listen_port.setRange(1, 65535)
        self.listen_port.setToolTip("SLSK_LISTEN_PORT overrides this setting when present.")
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
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
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

    def _settings_category(self, group: QGroupBox) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(group, 0)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _table(self, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumSectionSize(42)
        header.setStretchLastSection(True)
        for column, title in enumerate(headers):
            table.horizontalHeaderItem(column).setToolTip(title)
        return table

    def _insert_row(self, table: QTableWidget, row: int, values: tuple[str, ...]) -> None:
        table.insertRow(row)
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            table.setItem(row, column, item)

    def _build_exports_group(self) -> QGroupBox:
        group = QGroupBox("Optional DJ collection exports")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        self.rekordbox_export = QCheckBox("Deliver to Rekordbox")
        layout.addWidget(self.rekordbox_export)
        layout.addWidget(QLabel("File output works without exports. Collection changes are previewed before delivery."))
        return group

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
        self._set_status("Ready.")
        if not (self._config.soulseek_username and self._config.musicbrainz_contact):
            self.tabs.setCurrentWidget(self.tabs.widget(2))
            self._set_status(
                "First run: Soulseek username, MusicBrainz contact, and AcoustID key are required before processing."
            )
        elif not value["queue"]:
            self.tabs.setCurrentWidget(self.tabs.widget(1))

    def _finish_enqueue(self, value: dict[str, Any]) -> None:
        self._set_queue(value["queue"])
        self._preview = None
        self.enqueue_button.setEnabled(False)
        summary = self._admission_summary
        self.preview_summary.setText(
            f"Submission {value['job_id']} was added. {summary}"
        )
        self.tabs.setCurrentWidget(self.tabs.widget(0))
        self._set_status(f"Added submission {value['job_id']} to the queue.")

    def _on_progress(self, progress: object) -> None:
        stage = getattr(progress, "stage", "Working")
        message = getattr(progress, "message", str(progress))
        job_id = getattr(progress, "job_id", "")
        idx = getattr(progress, "idx", None)
        location = f"{job_id} · track {idx}" if job_id and idx is not None else job_id
        line = " · ".join(part for part in (str(stage), location, str(message)) if part)
        self._set_status(line)
        self.activity_button.setVisible(True)
        self.progress_log.appendPlainText(line)
        fraction = getattr(progress, "fraction", None)
        if fraction is not None and job_id and idx is not None:
            key = (job_id, idx)
            self._transfer_fraction[key] = float(fraction)
            row = self._queue_by_key.get(key)
            item = self._queue_items.get(key)
            if row is not None and item is not None:
                self._render_queue_row(item.row(), key, row)

    def _toggle_activity(self, expanded: bool) -> None:
        self.progress_log.setVisible(expanded)
        self.activity_button.setText("Hide activity" if expanded else "Show activity")

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
        summary = f"Soulseek runtime: {status.state}. Restart required: {restart}. Deferred changes: {deferred}."
        if error != "none":
            summary += f" Runtime error or session loss: {error}"
        self.settings_status.setText(summary)
        self.settings_details.setPlainText(
            f"{download}\nSaved: {saved}\nEffective: {effective}\n"
            f"Restart required: {restart}\nDeferred changes: {deferred}\n"
            f"Effective shared folders: {shares}\nShare reindexing: {reindex}\n"
            f"Runtime error or session loss: {error}"
        )

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

    def _linked_field(self, edit: QLineEdit, button: QPushButton) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        row.addWidget(button)
        return container

    def _open_external_url(self, url: str) -> None:
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(
                self,
                "Could not open link",
                f"The system could not open this URL in a browser:\n\n{url}",
            )

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
        if action == "run":
            self._auto_after("error" in response)
            if not self._closing:
                self._submit("runtime_status")
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
        elif self._auto_running:
            reason = "Automatic pass stopped" if action == "run" else f"{action.replace('_', ' ').title()} failed"
            self._set_status(f"{reason}: {message}")
            self.result_label.setText(f"{reason}: {message}")
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
        elif action == "snapshot":
            self._set_queue(value)
        elif action == "delivery_plans":
            self._show_delivery_plans(*value)
        elif action in ("run", "deliver"):
            self._finish_processing(action, value)
        elif action == "save":
            self._finish_settings_save(value)
        elif action.startswith("runtime_"):
            self._finish_runtime_action(value)
        elif action == "shutdown":
            self._set_status("Shutdown complete.")

    def _set_queue(self, rows: list[Any]) -> None:
        self._queue = rows
        self._queue_by_key = {(row.job_id, row.idx): row for row in rows}
        self._populate_queue()

    @Slot(object)
    def _set_job_queue(self, snapshot: tuple[str, list[Any]]) -> None:
        job_id, rows = snapshot
        start = next((index for index, row in enumerate(self._queue) if row.job_id == job_id), len(self._queue))
        stop = start
        while stop < len(self._queue) and self._queue[stop].job_id == job_id:
            stop += 1
        self._queue[start:stop] = rows
        self._set_queue(self._queue)

    def _finish_processing(self, action: str, value: dict[str, Any]) -> None:
        self._set_queue(value["queue"])
        self._show_result(value["result"], action, value["job_id"])

    def _auto_advance(self) -> None:
        """Work the queue without being asked, for as long as the window is open."""
        if self._config is None or self._busy or self._closing or self._paused or self._plan_open:
            return
        if time.monotonic() < self._auto_next:
            return
        job_id = self._next_job()
        if job_id is None:
            return
        self._auto_last_job = job_id
        self._auto_before = self._job_progress(job_id)
        self._auto_running = True
        self._start_run(job_id)

    def _start_run(self, job_id: str) -> None:
        self.runtime_label.setText("Soulseek: running a pass")
        self._submit("run", {"job_id": job_id})

    def _next_job(self) -> str | None:
        jobs = dict.fromkeys((row.job_id for row in self._queue), False)
        for row in self._queue:
            status = str(row.status)
            decision = str(row.decision)
            if (status in state.LIVE and decision not in state.DECIDED
                    or status in ACTIVE_STATUSES and decision not in STOPPED_DECISIONS):
                jobs[row.job_id] = True
        order = list(jobs)
        start = order.index(self._auto_last_job) + 1 if self._auto_last_job in jobs else 0
        return next((job_id for job_id in order[start:] + order[:start] if jobs[job_id]), None)

    def _job_progress(self, job_id: str | None) -> dict[int, int]:
        return {row.idx: STAGE_PERCENT[state.FAILED] if row.decision in STOPPED_DECISIONS
                else STAGE_PERCENT.get(row.status, 0) for row in self._queue if row.job_id == job_id}

    def _auto_after(self, failed: bool) -> None:
        """Space the next automatic pass: sooner after one that moved something, later after one that did not."""
        if not self._auto_running:
            return
        self._auto_running = False
        progress = self._job_progress(self._auto_last_job)
        advanced = any(value > self._auto_before.get(idx, 0) for idx, value in progress.items())
        if failed or not advanced:
            self._auto_delay = min(self._auto_delay * 2, AUTO_MAX_DELAY)
        else:
            self._auto_delay = AUTO_FIRST_DELAY
        self._auto_next = time.monotonic() + self._auto_delay

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
        self.server_port.setValue(int(port) if separator and port.isdecimal() else DEFAULT_SERVER_PORT)
        port = config.soulseek_configured_listen_port
        self.listen_port.setValue(port if port is not None else DEFAULTS.soulseek_listen_port)
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
        host = self.server_host_edit.text().strip()
        server = f"{host}:{self.server_port.value()}" if host else ""
        taxonomy = Path(self.taxonomy_edit.text()).expanduser().resolve() if self.taxonomy_edit.text().strip() else None
        shares = tuple(
            Path(self.shares_list.item(index).text()).expanduser().resolve()
            for index in range(self.shares_list.count())
        )
        targets = settings.merged_targets(self._config.export_targets,
                                          self.rekordbox_export.isChecked())
        port = self.listen_port.value()
        configured_port = self._config.soulseek_configured_listen_port
        if configured_port is not None or port != DEFAULTS.soulseek_listen_port:
            configured_port = port
        return replace(
            self._config,
            staging=self._required_path(self.staging_edit, "Download / staging folder"),
            lossless=self._required_path(self.lossless_edit, "Finished lossless folder"),
            lossy=self._required_path(self.lossy_edit, "Finished lossy folder"),
            shared_dirs=shares if shares or self._config.shared_dirs == () else None,
            soulseek_server=server,
            soulseek_listen_port=listen_port(port),
            soulseek_configured_listen_port=configured_port,
            upload_slots=self.upload_slots.value(),
            max_downloads=self.max_downloads.value(),
            workers=self.workers.value(),
            search_interval=self.search_interval.value(),
            soulseek_username=self.username_edit.text().strip(),
            musicbrainz_contact=self.musicbrainz_edit.text().strip(),
            taxonomy=taxonomy,
            export_targets=targets,
            secrets=self._entered_secrets(),
        )

    def _entered_secrets(self) -> dict[str, str]:
        secrets = dict(self._config.secrets)
        for name, edit in (
            (self._config.slsk_password_env, self.password_edit),
            (self._config.acoustid_key_env, self.acoustid_edit),
            (self._config.discogs_token_env, self.discogs_edit),
        ):
            value = edit.text()
            if value:
                secrets[name] = value
        return secrets

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
        self._submit("save", {"config": config, "remember": self.remember_secrets.isChecked()})

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

    @Slot(str)
    def _preview_from_queue(self, source: str) -> None:
        self._preview = None
        self.preview_table.setRowCount(0)
        self.preview_summary.setText("Loading playlist preview…")
        self.source_edit.setText(source)
        self.tabs.setCurrentWidget(self.tabs.widget(1))
        self._preview_source()

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
        accepted = 0
        linked = 0
        repeated = 0
        unreported = 0
        held_count = len(held)
        for index, record in enumerate(preview.records):
            match = matches.get(record.idx)
            held_path = held.get(record.idx)
            if held_path:
                admission, work_id = "Already held", str(held_path)
            elif match is None:
                admission, work_id = "Admission not reported", ""
                unreported += 1
            elif match.created:
                admission, work_id = "Accepted as new work", str(match.work_id)
                accepted += 1
            elif match.existing:
                admission, work_id = "Linked to existing work", str(match.work_id)
                linked += 1
            else:
                admission, work_id = "Linked within this submission", str(match.work_id)
                repeated += 1
            self._insert_row(self.preview_table, index, (str(record.idx), record.label, admission, work_id))
        self._admission_summary = (
            f"New work accepted: {accepted}; linked to existing work: {linked}; "
            f"linked within this submission: {repeated}; already held: {held_count}; "
            f"admission not reported: {unreported}."
        )
        self.preview_summary.setText(
            f"{preview.name} · {preview.source} · {len(preview.records)} entries. "
            f"{self._admission_summary} Review before adding."
        )
        self.enqueue_button.setEnabled(bool(preview.records))

    def _enqueue_preview(self) -> None:
        if self._preview is not None:
            self._submit("enqueue", {"preview": self._preview})

    def _populate_queue(self) -> None:
        self._forget_settled_transfers()
        selected_filter = self.queue_filter.currentText().lower()
        rows = [row for row in self._queue if self._queue_filter_matches(row, selected_filter)]
        keys = [(row.job_id, row.idx) for row in rows]
        chosen = self._selected_key()
        self._prune_queue_rows(keys)
        for index, (key, row) in enumerate(zip(keys, rows)):
            self._render_queue_row(index, key, row)
        self._rendered_keys = keys
        selected = self._queue_items[chosen].row() if chosen in self._queue_items else -1
        self.queue_root.setProperty("selectedIndex", selected)
        self._update_queue_actions()

    def _forget_settled_transfers(self) -> None:
        # A fraction belongs to one attempt, not to a track that has since moved on.
        for key in tuple(self._transfer_fraction):
            row = self._queue_by_key.get(key)
            if row is None or row.status not in state.LIVE or row.decision in STOPPED_DECISIONS:
                del self._transfer_fraction[key]

    def _prune_queue_rows(self, keys: list[tuple[str, int]]) -> None:
        if keys == self._rendered_keys:
            return
        positions = {key: index for index, key in enumerate(keys)}
        next_index = len(keys)
        for index in range(len(self._rendered_keys) - 1, -1, -1):
            key = self._rendered_keys[index]
            position = positions.get(key)
            if position is not None and position < next_index:
                next_index = position
                continue
            self.queue_model.removeRow(index)
            del self._queue_items[key]

    def _render_queue_row(self, index: int, key: tuple[str, int], row: Any) -> None:
        values = (
            str(row.label), str(getattr(row, "source", "")), self._stage_text(row),
            str(getattr(row, "peer", "")), self._peer_queue_value(row),
            self._row_percent(row), self._progress_text(row), str(getattr(row, "notes", "")),
            self._queue_next_step(row, key),
        )
        item = self._queue_items.get(key)
        if item is None:
            item = QStandardItem()
            for role, value in zip(QUEUE_ROLES, values):
                item.setData(value, role)
            self.queue_model.insertRow(index, item)
            self._queue_items[key] = item
            return
        changed = {role: value for role, value in zip(QUEUE_ROLES, values) if item.data(role) != value}
        if changed:
            self.queue_model.setItemData(item.index(), changed)

    def _queue_next_step(self, row: Any, key: tuple[str, int]) -> str:
        if key in self._transfer_fraction:
            return ""
        return QUEUE_REASON_TEXT.get(getattr(row, "queue_reason", ""), ("", ""))[1]

    def _stage_text(self, row: Any) -> str:
        status = str(row.status)
        if row.decision in STOPPED_DECISIONS:
            return "Not found" if row.decision == state.DECLINED else "Skipped"
        if status in state.LIVE and (row.job_id, row.idx) in self._transfer_fraction:
            return "Downloading"
        reason = QUEUE_REASON_TEXT.get(getattr(row, "queue_reason", ""))
        if reason:
            return reason[0]
        return STAGE_LABEL.get(status, status.replace("_", " ").title())

    def _row_percent(self, row: Any) -> int:
        if row.decision in STOPPED_DECISIONS:
            return STAGE_PERCENT[state.FAILED]
        status = str(row.status)
        live = self._transfer_fraction.get((row.job_id, row.idx))
        if live is not None and status in state.LIVE:
            return min(100, TRANSFER_FLOOR + int(live * TRANSFER_SPAN))
        return STAGE_PERCENT.get(status, 0)

    def _progress_text(self, row: Any) -> str:
        status = str(row.status)
        if status in FAILED_STATUSES or str(row.decision) in STOPPED_DECISIONS:
            return "stopped"
        if status in state.FINISHED:
            return "done"
        return f"{self._row_percent(row)}%"

    def _queue_filter_matches(self, row: Any, selected_filter: str) -> bool:
        if selected_filter == "all":
            return True
        category = self._queue_category(row)
        if selected_filter == "history":
            return category in ("completed", "failed")
        return category == selected_filter

    def _queue_category(self, row: Any) -> str:
        status = str(row.status).casefold()
        if status in FAILED_STATUSES or str(row.decision) in STOPPED_DECISIONS:
            return "failed"
        if status in state.FINISHED:
            return "completed"
        if status in ACTIVE_STATUSES:
            return "active"
        if getattr(row, "peer", "") or (row.job_id, row.idx) in self._transfer_fraction:
            return "active"
        return "pending"

    def _peer_queue_value(self, row: Any) -> str:
        position = getattr(row, "peer_queue", None)
        return "" if position is None else str(position)

    def _selected_key(self) -> tuple[str, int] | None:
        selected = self.queue_root.property("selectedIndex")
        if 0 <= selected < len(self._rendered_keys):
            return self._rendered_keys[selected]
        return None

    def _update_queue_actions(self) -> None:
        if not hasattr(self, "process_button"):
            return
        key = self._selected_key()
        enabled = self._config is not None and not self._busy and not self._closing
        has_selection = enabled and key is not None
        self.process_button.setEnabled(has_selection)
        self.delivery_button.setEnabled(has_selection)
        self.open_output_button.setEnabled(has_selection)
        self.refresh_button.setEnabled(enabled)
        self.queue_root.setProperty("canPreview", enabled)
        if not self._queue:
            message = "Queue is empty. Add a playlist to add tracks."
        elif self.queue_model.rowCount() == 0:
            message = f"No tracks match the {self.queue_filter.currentText()} filter."
        elif key is None:
            message = "Select a track. Process and delivery actions apply to its whole submission."
        else:
            row = self._selected_queue_row()
            label = str(getattr(row, "label", "selected track"))
            message = f"Selected track: {label}. Process and delivery actions apply to submission {key[0]}."
        self.queue_state_label.setText(message)

    def _process_selected(self) -> None:
        key = self._selected_key()
        if key:
            self._start_run(key[0])

    def _selected_queue_row(self) -> Any:
        return self._queue_by_key.get(self._selected_key())

    def _preview_deliveries(self) -> None:
        key = self._selected_key()
        if key:
            self._submit("delivery_plans", {"job_id": key[0]})

    def _show_delivery_plans(self, job_id: str, plans: list[Any]) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Delivery plan: {job_id}")
        dialog.resize(760, 360)
        layout = QVBoxLayout(dialog)
        table = self._table(("Target", "Destination", "Planned changes", "Requires confirmation", "Blocker"))
        deliverable = bool(plans) and all(
            not getattr(plan, "blocked", False) and not plan.error for plan in plans
        )
        needs_confirmation = any(plan.requires_confirmation for plan in plans)
        for index, plan in enumerate(plans):
            self._insert_row(table, index, (
                str(plan.target), str(plan.destination), str(plan.changes),
                "Yes" if plan.requires_confirmation else "No", str(plan.error or ""),
            ))
        layout.addWidget(table)
        layout.addWidget(QLabel(
            "Review every target and nonzero change before writing to a collection."
            if plans else "No collection targets are enabled. Finished files remain in the output folders."
        ))
        confirmation = QCheckBox("I reviewed the plan and authorize configured deliveries")
        confirmation.setChecked(deliverable and not needs_confirmation)
        layout.addWidget(confirmation)
        buttons = QHBoxLayout()
        deliver = QPushButton("Deliver configured targets")
        deliver.setEnabled(bool(plans) and confirmation.isChecked())
        confirmation.toggled.connect(lambda checked: deliver.setEnabled(bool(plans) and checked))
        cancel = QPushButton("Cancel")
        buttons.addWidget(deliver)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        deliver.clicked.connect(lambda: self._deliver_from_plan(dialog, job_id))
        cancel.clicked.connect(dialog.reject)
        self._plan_open = True
        try:
            dialog.exec()
        finally:
            self._plan_open = False

    def _deliver_from_plan(self, dialog: QDialog, job_id: str) -> None:
        if self._busy or self._closing:
            QMessageBox.warning(dialog, "Delivery unavailable", "Finish the current operation before delivering.")
            return
        dialog.accept()
        self._submit("deliver", {"job_id": job_id})

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
                f"Failed work: {result.failed}",
                f"Missing genres: {missing_genres}",
                f"Destination delivery pending: {pending_targets}",
            ]
        text = "\n".join(lines)
        title = "Delivery result" if action == "deliver" else "Processing result"
        self.result_label.setText(f"{title}\n{text}")
        self.progress_log.appendPlainText(text)
        if not self._closing and not self._auto_running:
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
        self._auto_timer.stop()
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


def main() -> int:
    expose_payload_on_path()
    application = QApplication(sys.argv)
    application.setApplicationName("Music Agent")
    application.setApplicationDisplayName("Music Agent")
    window = DesktopWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
