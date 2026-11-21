from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_agent import deliveries
from music_agent.config import DEFAULTS


class FakeJob:
    def __init__(self, root: Path, records):
        self.root = root
        self.id = root.name
        self._records = records

    def records(self, *, records=None, work_ids=None):
        return self._records

    def execution(self):
        return self

    def work_id(self, idx):
        return next(record.work_id for record in self._records if record.idx == idx)


class _AlwaysSuccess(dict):
    """A job whose every artifact already has a success receipt."""

    def get(self, key, default=None):
        return "success"


class FakeTarget:
    def __init__(self, name: str):
        self.name = name

    def available(self, config):
        return True


def records(tmp_path: Path):
    first = tmp_path / "one.flac"
    second = tmp_path / "two.flac"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    return [
        SimpleNamespace(idx=1, work_id="work-one", label="One", status="tagged",
                        local_path=str(first), replaces=""),
        SimpleNamespace(idx=2, work_id="work-two", label="Two", status="tagged",
                        local_path=str(second), replaces=""),
    ]


def test_delivery_plans_are_read_only_and_keep_nonzero_target_plans(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox", "traktor"))
    job = FakeJob(tmp_path, records(tmp_path))
    targets = [FakeTarget("rekordbox"), FakeTarget("traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    planned = []

    def plan_target(target, config, job, records):
        planned.append(target.name)
        return deliveries.DeliveryPlan(
            target=target.name,
            destination=f"{target.name}-collection",
            changes="2 to add" if target.name == "rekordbox" else "1 to repoint",
            requires_confirmation=True,
        )

    monkeypatch.setattr(deliveries, "_plan_target", plan_target)

    plans = deliveries.delivery_plans(config, job)

    assert [plan.target for plan in plans] == ["rekordbox", "traktor"]
    assert [plan.changes for plan in plans] == ["2 to add", "1 to repoint"]
    assert planned == ["rekordbox", "traktor"]


@pytest.mark.parametrize("selected", ["rekordbox", "traktor"])
def test_legacy_refresh_verifies_each_file_only_for_selected_targets(tmp_path, monkeypatch, selected):
    config = replace(DEFAULTS, state_root=tmp_path, export_targets=("rekordbox",))
    retained = records(tmp_path)
    retained.append(SimpleNamespace(
        idx=3, work_id="work-missing", label="Missing", status="tagged",
        local_path=str(tmp_path / "missing.flac"),
    ))
    job = FakeJob(tmp_path, retained)
    verified = (retained[0].local_path, retained[2].local_path)
    plans = [deliveries.DeliveryPlan(
        target=target, destination=f"{target}-collection", changes="",
        requires_confirmation=False, verified_artifacts=verified,
    ) for target in ("rekordbox", "traktor")]
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records:
                        next(plan for plan in plans if plan.target == target.name))
    saved = {}
    monkeypatch.setattr(
        deliveries.store, "set_deliveries",
        lambda root, entries: saved.update({(work_id, target): status
                                           for work_id, target, _destination, status, _error in entries}),
    )

    outcomes = deliveries.refresh_receipts(config, job, [selected])

    assert saved == {
        ("work-one", selected): "success",
        ("work-two", selected): "failed",
        ("work-missing", selected): "failed",
    }
    assert outcomes[0].success is False


def test_delivery_failure_is_recorded_per_artifact_and_does_not_skip_other_target(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox", "traktor"), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    targets = [FakeTarget("rekordbox"), FakeTarget("traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records: deliveries.DeliveryPlan(
        target=target.name, destination=f"{target.name}-collection", changes="2 to add",
        requires_confirmation=True,
    ))
    approved_plans = deliveries.delivery_plans(config, job)

    rows = {}
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda root, job_id: dict(rows))
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda root, entries: rows.update(
        {(work_id, target, destination): status
         for work_id, target, destination, status, _error in entries}))
    attempted = []

    def deliver_target(target, config, job, selected, planned, emit):
        attempted.append(target.name)
        if target.name == "rekordbox":
            raise RuntimeError("rekordbox is open")
        return [deliveries.ArtifactOutcome(record.idx, "success", "added") for record in selected]

    monkeypatch.setattr(deliveries, "_execute_target", deliver_target)

    outcomes = deliveries.deliver(config, job, approved_plans=approved_plans,
                                  emit=lambda stage, message: None)

    rekordbox = next(outcome for outcome in outcomes if outcome.target == "rekordbox")
    traktor = next(outcome for outcome in outcomes if outcome.target == "traktor")
    assert not rekordbox.success
    assert [(item.idx, item.status) for item in rekordbox.artifacts] == [(1, "failed"), (2, "failed")]
    assert traktor.success
    assert [(item.idx, item.status) for item in traktor.artifacts] == [(1, "success"), (2, "success")]
    assert attempted == ["rekordbox", "traktor"]

    def saved_status(work_id, target):
        return [status for (saved_work_id, saved_target, _), status in rows.items()
                if (saved_work_id, saved_target) == (work_id, target)]

    assert saved_status("work-one", "rekordbox") == ["failed"]
    assert saved_status("work-two", "rekordbox") == ["failed"]
    assert saved_status("work-one", "traktor") == ["success"]
    assert saved_status("work-two", "traktor") == ["success"]


def test_retry_skips_artifacts_already_delivered_to_same_destination(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    target = FakeTarget("rekordbox")
    present = {str(Path(job.records()[0].local_path).resolve())}
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records: deliveries.DeliveryPlan(
        target=target.name, destination="master.db", changes="2 to add", requires_confirmation=True,
        verified_artifacts=tuple(sorted(present)),
    ))
    approved_plans = deliveries.delivery_plans(config, job)
    rows = {}
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda root, job_id: dict(rows))
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda root, entries: rows.update(
        {(work_id, target, destination): status
         for work_id, target, destination, status, _error in entries}))
    rows[("work-one", "rekordbox",
          deliveries._collection_key("master.db", job.records()[0], "work-one"))] = "success"
    calls = []

    def deliver_target(target, config, job, selected, planned, emit):
        calls.append([record.idx for record in selected])
        present.update(str(Path(record.local_path).resolve()) for record in selected)
        return [deliveries.ArtifactOutcome(record.idx, "success", "added") for record in selected]

    monkeypatch.setattr(deliveries, "_execute_target", deliver_target)

    first = deliveries.deliver(config, job, approved_plans=approved_plans,
                               emit=lambda stage, message: None)
    approved_plans = deliveries.delivery_plans(config, job)
    second = deliveries.deliver(config, job, approved_plans=approved_plans,
                                emit=lambda stage, message: None)

    assert calls == [[2]]
    assert [(item.idx, item.status) for item in first[0].artifacts] == [(1, "success"), (2, "success")]
    assert [(item.idx, item.status) for item in second[0].artifacts] == [(1, "success"), (2, "success")]


def test_stale_visible_plan_prevents_collection_write(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    target = FakeTarget("rekordbox")
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    changes = iter(["2 to add", "3 to add"])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records: deliveries.DeliveryPlan(
        target=target.name, destination="master.db", changes=next(changes), requires_confirmation=True,
    ))
    approved_plans = deliveries.delivery_plans(config, job)
    monkeypatch.setattr(deliveries, "_execute_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a changed plan must not write the collection")))

    (outcome,) = deliveries.deliver(config, job, approved_plans=approved_plans,
                                    emit=lambda stage, message: None)

    assert not outcome.success
    assert "preview again" in outcome.message.lower()
    assert all(item.status == "failed" for item in outcome.artifacts)


def test_replaced_artifact_gets_a_new_durable_delivery_key(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    target = FakeTarget("rekordbox")
    present = set()
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records: deliveries.DeliveryPlan(
        target=target.name, destination="master.db", changes="2 to add", requires_confirmation=True,
        verified_artifacts=tuple(sorted(present)),
    ))
    rows = {}
    destinations = []
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda root, job_id: dict(rows))

    def set_deliveries(root, entries):
        for work_id, target, destination, status, _error in entries:
            rows[(work_id, target, destination)] = status
            destinations.append((work_id, destination))

    monkeypatch.setattr(deliveries.store, "set_deliveries", set_deliveries)
    calls = []

    def deliver_target(target, config, job, selected, planned, emit):
        calls.append([record.idx for record in selected])
        present.update(str(Path(record.local_path).resolve()) for record in selected)
        return [deliveries.ArtifactOutcome(record.idx, "success", "added") for record in selected]

    monkeypatch.setattr(deliveries, "_execute_target", deliver_target)
    approved_plans = deliveries.delivery_plans(config, job)
    deliveries.deliver(config, job, approved_plans=approved_plans, emit=lambda stage, message: None)

    replacement = tmp_path / "one-replaced.flac"
    replacement.write_bytes(b"replacement")
    job._records[0].local_path = str(replacement)
    approved_plans = deliveries.delivery_plans(config, job)
    deliveries.deliver(config, job, approved_plans=approved_plans, emit=lambda stage, message: None)

    first_keys = [destination for work_id, destination in destinations if work_id == "work-one"]
    assert len(set(first_keys)) == 2
    assert calls == [[1, 2], [1]]


def test_no_configured_targets_has_nothing_to_plan_or_write(tmp_path):
    config = replace(DEFAULTS, export_targets=(), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))

    assert deliveries.delivery_plans(config, job) == []
    assert deliveries.deliver(config, job, approved_plans=[],
                              emit=lambda stage, message: None) == []


def test_unavailable_target_reports_each_artifact_without_writing(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    target = FakeTarget("traktor")
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job, records: deliveries.DeliveryPlan(
        target=target.name, destination="collection", changes="", requires_confirmation=False,
        error="traktor-nml is not installed on this machine", blocked=True,
    ))
    monkeypatch.setattr(deliveries, "_execute_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a blocked target must not write")))
    saved = []
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda root, entries: saved.extend(entries))
    approved_plans = deliveries.delivery_plans(config, job)

    (outcome,) = deliveries.deliver(config, job, approved_plans=approved_plans,
                                    emit=lambda stage, message: None)

    assert not outcome.success
    assert [(item.idx, item.status) for item in outcome.artifacts] == [(1, "failed"), (2, "failed")]
    assert saved == []


def test_rekordbox_plan_classifies_added_repointed_present_and_unsupported(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from music_agent import importing

    config = replace(DEFAULTS, state_root=tmp_path)
    new = tmp_path / "new.flac"
    upgrade = tmp_path / "upgrade.flac"
    present = tmp_path / "present.flac"
    unsupported = tmp_path / "unsupported.ogg"
    old = tmp_path / "old.flac"
    for path in (new, upgrade, present, unsupported, old):
        path.write_bytes(b"x")
    master = tmp_path / "master.db"
    master.write_bytes(b"db")

    records = [
        SimpleNamespace(idx=1, work_id="w1", label="New", status="tagged", local_path=str(new), replaces=""),
        SimpleNamespace(idx=2, work_id="w2", label="Upgrade", status="tagged",
                        local_path=str(upgrade), replaces=str(old)),
        SimpleNamespace(idx=3, work_id="w3", label="Present", status="tagged",
                        local_path=str(present), replaces=""),
        SimpleNamespace(idx=4, work_id="w4", label="Ogg", status="tagged",
                        local_path=str(unsupported), replaces=""),
    ]
    job = FakeJob(tmp_path, records)
    rows = [SimpleNamespace(FolderPath=str(present)), SimpleNamespace(FolderPath=str(old))]

    @contextmanager
    def fake_database(config):
        yield SimpleNamespace(get_content=lambda: rows)

    monkeypatch.setattr(importing, "is_running", lambda: False)
    monkeypatch.setattr(importing, "database", fake_database)
    monkeypatch.setattr(importing, "database_path", lambda db: master)

    planned = importing.plan(config, job)

    assert planned.added == [1]
    assert planned.repointed == [2]
    assert planned.already == [3]
    assert list(planned.failed) == [4]
    assert planned.requires_confirmation


def test_traktor_plan_is_read_only(tmp_path, monkeypatch):
    from music_agent import traktor

    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml"])

    def run(argv, **kwargs):
        if "verify" in argv:
            payload = {"collection": str(tmp_path / "collection.nml"),
                       "paths": {str(Path(record.local_path).resolve()): False for record in job.records()}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if argv[-1] != "plan":
            raise AssertionError("A read-only plan must not apply collection changes")
        return SimpleNamespace(returncode=0, stdout="2 to repoint · 1 to add", stderr="")

    monkeypatch.setattr(traktor.subprocess, "run", run)

    (plan,) = deliveries.delivery_plans(config, job)

    assert plan.target == "traktor"
    assert plan.changes == "2 to repoint · 1 to add"
    assert plan.requires_confirmation


def test_open_rekordbox_is_a_plan_error_before_database_open(tmp_path, monkeypatch):
    from music_agent import importing

    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    monkeypatch.setattr(importing, "is_running", lambda: True)
    monkeypatch.setattr(importing, "database", lambda config: (_ for _ in ()).throw(
        AssertionError("an open rekordbox must be refused before opening its database")))

    (plan,) = deliveries.delivery_plans(config, job)

    assert plan.target == "rekordbox"
    assert plan.error
    assert "close" in plan.error.lower()
    assert not plan.requires_confirmation


@pytest.mark.parametrize("classification, expected", [
    ("already", "success"), ("added", "pending"), ("repointed", "pending"),
])
def test_saved_success_requires_current_collection_membership(tmp_path, monkeypatch, classification, expected):
    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path)[:1])
    target = FakeTarget("rekordbox")
    current = deliveries.importing.ImportPlan("master.db", (), **{classification: [1]})
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries.importing, "plan", lambda config, job, records=None: current)
    rows = {("work-one", "rekordbox",
             deliveries._collection_key("master.db", job.records()[0], "work-one")): "success"}
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda root, job_id: dict(rows))
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda root, entries: rows.update(
        {(work_id, target, destination): status
         for work_id, target, destination, status, _error in entries}))
    selected = []

    def execute(target, config, job, records, planned, emit):
        selected.extend(record.idx for record in records)
        return [deliveries.ArtifactOutcome(record.idx, "success", "restored") for record in records]

    monkeypatch.setattr(deliveries, "_execute_target", execute)
    planned = deliveries.delivery_plans(config, job)
    receipt = deliveries.delivery_receipts(config, job, planned)[1]["rekordbox"]
    assert receipt["status"] == expected

    deliveries.deliver(config, job, planned)
    assert selected == ([] if classification == "already" else [1])


def test_traktor_zero_change_plan_cannot_verify_unindexed_external_file(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path)[:1])
    target = FakeTarget("traktor")
    collection = str(tmp_path / "collection.nml")
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries.traktor, "plan", lambda config:
                        deliveries.traktor.Plan("configured-command", "0 to repoint · 0 to add", False))
    monkeypatch.setattr(deliveries.traktor, "verify", lambda config, paths:
                        {"collection": collection, "paths": {path: False for path in paths}}, raising=False)
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda *args: _AlwaysSuccess())
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda *args, **kwargs: None)

    planned = deliveries.delivery_plans(config, job)
    outcome = deliveries.deliver(config, job, planned)[0]

    assert outcome.success is False
    assert outcome.artifacts[0].status == "failed"
    assert deliveries.delivery_receipts(config, job)[1]["traktor"]["status"] != "success"


@pytest.fixture
def sqlite_collection(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    config = replace(DEFAULTS, export_targets=("rekordbox",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path)[:1])
    master = tmp_path / "master.db"
    setup = sqlite3.connect(master)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE tracks (path TEXT)")
    setup.execute("INSERT INTO tracks VALUES (?)", (job.records()[0].local_path,))
    setup.commit()
    setup.close()
    readers = []

    @contextmanager
    def database(config):
        reader = sqlite3.connect(master)
        readers.append(reader)
        yield SimpleNamespace(get_content=lambda: [
            SimpleNamespace(FolderPath=row[0]) for row in reader.execute("SELECT path FROM tracks")
        ])

    monkeypatch.setattr(deliveries.importing, "database", database)
    monkeypatch.setattr(deliveries.importing, "database_path", lambda db: master)
    monkeypatch.setattr(deliveries.importing, "is_running", lambda: False)
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [FakeTarget("rekordbox")])
    monkeypatch.setattr(deliveries.store, "delivery_statuses", lambda *args: _AlwaysSuccess())
    yield config, job, master
    for reader in readers:
        reader.close()


def test_read_only_wal_sidecars_do_not_revoke_delivery_approval(sqlite_collection):
    config, job, master = sqlite_collection
    approved = deliveries.delivery_plans(config, job)

    assert Path(f"{master}-wal").stat().st_size == 0
    outcomes = deliveries.deliver(config, job, approved)

    assert outcomes[0].success
    assert outcomes[0].artifacts[0].status == "success"


def test_committed_wal_changes_revoke_delivery_approval(sqlite_collection):
    import sqlite3

    config, job, master = sqlite_collection
    approved = deliveries.delivery_plans(config, job)
    with sqlite3.connect(master) as writer:
        writer.execute("INSERT INTO tracks VALUES (?)", ("another-track.flac",))
    outcomes = deliveries.deliver(config, job, approved)

    assert not outcomes[0].success
    assert "changed" in outcomes[0].message


def test_traktor_alone_stages_the_file_it_replaces(tmp_path, monkeypatch):
    """traktor-nml repoints onto a file whose predecessor is gone, so the plan moves it first."""
    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    old = tmp_path / "held.mp3"
    old.write_bytes(b"held")
    new = tmp_path / "upgrade.aiff"
    new.write_bytes(b"upgrade")
    job = FakeJob(tmp_path, [SimpleNamespace(idx=1, work_id="w1", label="Upgrade", status="tagged",
                                             local_path=str(new), replaces=str(old))])
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [FakeTarget("traktor")])
    monkeypatch.setattr(deliveries.traktor, "replacements",
                        lambda config, records: {old.resolve(): new.resolve()})
    monkeypatch.setattr(deliveries.traktor, "plan", lambda config:
                        deliveries.traktor.Plan("configured-command", "0 to repoint · 1 to add", False))
    monkeypatch.setattr(deliveries.traktor, "verify", lambda config, paths:
                        {"collection": "collection", "paths": {path: True for path in paths}},
                        raising=False)

    planned = deliveries.delivery_plans(config, job)[0]

    assert not planned.blocked
    assert planned.replacements == ((str(old.resolve()), str(new.resolve()),
                                     old.stat().st_size, old.stat().st_mtime_ns),)
    assert planned.requires_confirmation
    assert "repointing" in planned.changes


def test_traktor_waits_for_the_collection_that_removes_the_old_file(tmp_path, monkeypatch):
    """With another target delivering after it, removing the old file first would break that target."""
    config = replace(DEFAULTS, export_targets=("rekordbox", "traktor"), state_root=tmp_path)
    old = tmp_path / "held.mp3"
    old.write_bytes(b"held")
    new = tmp_path / "upgrade.aiff"
    new.write_bytes(b"upgrade")
    job = FakeJob(tmp_path, [SimpleNamespace(idx=1, work_id="w1", label="Upgrade", status="tagged",
                                             local_path=str(new), replaces=str(old))])
    monkeypatch.setattr(deliveries.exporting, "configured",
                        lambda config: [FakeTarget("rekordbox"), FakeTarget("traktor")])

    plan = deliveries.delivery_plans(config, job)[1]

    assert plan.blocked
    assert "replaces" in plan.error


def test_an_undeliverable_record_fails_alone_and_does_not_block_traktor(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    old = tmp_path / "held.mp3"
    old.write_bytes(b"held")
    new = tmp_path / "upgrade.aiff"
    new.write_bytes(b"upgrade")
    orphan_old = tmp_path / "orphan-old.mp3"
    orphan_old.write_bytes(b"orphan-old")
    orphan = tmp_path / "orphan.aiff"
    orphan.write_bytes(b"orphan")
    job = FakeJob(tmp_path, [
        SimpleNamespace(idx=1, work_id="w1", label="Upgrade", status="tagged",
                        local_path=str(new), replaces=str(old)),
        SimpleNamespace(idx=2, label="No work", status="tagged",
                        local_path=str(orphan), replaces=str(orphan_old)),
    ])
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [FakeTarget("traktor")])
    monkeypatch.setattr(deliveries.traktor, "replacements", lambda config, records: {
        Path(record.replaces).resolve(): Path(record.local_path).resolve() for record in records})
    monkeypatch.setattr(deliveries.traktor, "plan", lambda config:
                        deliveries.traktor.Plan("configured-command", "1 to repoint", True))
    monkeypatch.setattr(deliveries.traktor, "verify", lambda config, paths:
                        {"collection": "collection", "paths": {path: True for path in paths}},
                        raising=False)
    synced = []

    def sync_replacements(config, originals, paths, *, collection=None, log=print):
        synced.append(originals)
        return "1 to repoint", {"collection": collection,
                                "paths": {str(Path(path).resolve()): True for path in paths}}

    monkeypatch.setattr(deliveries.traktor, "sync_replacements", sync_replacements)
    monkeypatch.setattr(deliveries.store, "set_deliveries", lambda *args, **kwargs: None)

    planned = deliveries.delivery_plans(config, job)[0]
    assert planned.replacements == ((str(old.resolve()), str(new.resolve()),
                                     old.stat().st_size, old.stat().st_mtime_ns),)
    assert dict(planned.artifact_errors) == {2: "record has no canonical work_id"}

    (outcome,) = deliveries.deliver(config, job, [planned], emit=lambda stage, message: None)

    assert [(item.idx, item.status) for item in outcome.artifacts] == [(1, "success"), (2, "failed")]
    assert synced == [{old.resolve(): new.resolve()}]


def test_a_state_read_error_is_a_delivery_failure_with_its_cause(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("traktor",), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [FakeTarget("traktor")])
    monkeypatch.setattr(deliveries.store, "work_ids", lambda root, job_id: (_ for _ in ()).throw(
        RuntimeError("database is locked")))

    (outcome,) = deliveries.deliver(config, job, approved_plans=[], emit=lambda stage, message: None)

    assert not outcome.success
    assert "database is locked" in outcome.message
    assert all("record has no canonical work_id" not in item.message for item in outcome.artifacts)
