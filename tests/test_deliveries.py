from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from music_agent import deliveries
from music_agent.config import DEFAULTS


class FakeJob:
    def __init__(self, root: Path, records):
        self.root = root
        self.id = root.name
        self._records = records

    def records(self):
        return self._records

    def work_id(self, idx):
        return next(record.work_id for record in self._records if record.idx == idx)


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
        SimpleNamespace(idx=1, work_id="work-one", label="One", status="tagged", local_path=str(first)),
        SimpleNamespace(idx=2, work_id="work-two", label="Two", status="tagged", local_path=str(second)),
    ]


def test_delivery_plans_are_read_only_and_keep_nonzero_target_plans(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox", "traktor"))
    job = FakeJob(tmp_path, records(tmp_path))
    targets = [FakeTarget("rekordbox"), FakeTarget("traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    planned = []

    def plan_target(target, config, job):
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


def test_delivery_failure_is_recorded_per_artifact_and_does_not_skip_other_target(tmp_path, monkeypatch):
    config = replace(DEFAULTS, export_targets=("rekordbox", "traktor"), state_root=tmp_path)
    job = FakeJob(tmp_path, records(tmp_path))
    targets = [FakeTarget("rekordbox"), FakeTarget("traktor")]
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: targets)
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job: deliveries.DeliveryPlan(
        target=target.name, destination=f"{target.name}-collection", changes="2 to add",
        requires_confirmation=True,
    ))
    approved_plans = deliveries.delivery_plans(config, job)

    rows = {}
    monkeypatch.setattr(deliveries.store, "delivery_status", lambda root, work_id, target, destination:
                    rows.get((work_id, target, destination)))
    monkeypatch.setattr(deliveries.store, "set_delivery", lambda root, work_id, target, destination,
                        status, error="": rows.__setitem__((work_id, target, destination), status))
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
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job: deliveries.DeliveryPlan(
        target=target.name, destination="master.db", changes="2 to add", requires_confirmation=True,
    ))
    approved_plans = deliveries.delivery_plans(config, job)
    rows = {}

    def delivery_status(root, work_id, target, destination):
        if work_id == "work-one":
            return rows.get((work_id, target), "success")
        return rows.get((work_id, target))

    monkeypatch.setattr(deliveries.store, "delivery_status", delivery_status)
    monkeypatch.setattr(deliveries.store, "set_delivery", lambda root, work_id, target, destination,
                        status, error="": rows.__setitem__((work_id, target), status))
    calls = []

    def deliver_target(target, config, job, selected, planned, emit):
        calls.append([record.idx for record in selected])
        return [deliveries.ArtifactOutcome(record.idx, "success", "added") for record in selected]

    monkeypatch.setattr(deliveries, "_execute_target", deliver_target)

    first = deliveries.deliver(config, job, approved_plans=approved_plans,
                               emit=lambda stage, message: None)
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
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job: deliveries.DeliveryPlan(
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
    monkeypatch.setattr(deliveries.exporting, "configured", lambda config: [target])
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job: deliveries.DeliveryPlan(
        target=target.name, destination="master.db", changes="2 to add", requires_confirmation=True,
    ))
    rows = {}
    destinations = []

    def delivery_status(root, work_id, target, destination):
        destinations.append((work_id, destination))
        return rows.get((work_id, target, destination))

    monkeypatch.setattr(deliveries.store, "delivery_status", delivery_status)
    monkeypatch.setattr(deliveries.store, "set_delivery", lambda root, work_id, target, destination,
                        status, error="": rows.__setitem__((work_id, target, destination), status))
    calls = []

    def deliver_target(target, config, job, selected, planned, emit):
        calls.append([record.idx for record in selected])
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
    monkeypatch.setattr(deliveries, "_plan_target", lambda target, config, job: deliveries.DeliveryPlan(
        target=target.name, destination="collection", changes="", requires_confirmation=False,
        error="traktor-nml is not installed on this machine", blocked=True,
    ))
    monkeypatch.setattr(deliveries, "_execute_target", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a blocked target must not write")))
    saved = []
    monkeypatch.setattr(deliveries.store, "set_delivery", lambda *args: saved.append(args))
    approved_plans = deliveries.delivery_plans(config, job)

    (outcome,) = deliveries.deliver(config, job, approved_plans=approved_plans,
                                    emit=lambda stage, message: None)

    assert not outcome.success
    assert [(item.idx, item.status) for item in outcome.artifacts] == [(1, "failed"), (2, "failed")]
    assert len(saved) == 2


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
    calls = []
    monkeypatch.setattr(traktor, "command", lambda config: ["traktor-nml"])

    def run(argv, **kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="2 to repoint · 1 to add", stderr="")

    monkeypatch.setattr(traktor.subprocess, "run", run)

    (plan,) = deliveries.delivery_plans(config, job)

    assert plan.target == "traktor"
    assert plan.changes == "2 to repoint · 1 to add"
    assert plan.requires_confirmation
    assert calls == ["plan"]


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
