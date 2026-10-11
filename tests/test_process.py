"""The processing stage: the two lanes, the house naming, and the order the canonical tools run in.

The runner is injected, so these tests read the exact command lines the stage would issue — and one of
them checks the thing most likely to go wrong quietly: a conversion that fails must put the file back,
because the real converter deletes its input rather than leaving it behind.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from music_agent import config as config_module
from music_agent import process, state


def make_config(tmp_path: Path) -> config_module.Config:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""[paths]
staging = "{tmp_path}/staging"
lossless = "{tmp_path}/Loseless Tracks"
lossy = "{tmp_path}/Losy Tracks"
state = "state"

[loudness]
target_lufs = -14.0
true_peak_db = -1.0
tolerance_lu = 0.5

[tools]
convert = "{tmp_path}/fake-convert.sh"
normalize = "{tmp_path}/fake-normalize.py"
""",
        encoding="utf-8",
    )
    (tmp_path / "staging").mkdir()
    return config_module.load(path)


class FakeRunner:
    """Records the commands and stands in for both tools.

    The conversion stands in faithfully in one respect that matters: the real script removes its input, so
    this does too. A failure leaves the input where it is, which is how the real one behaves as well.
    """

    def __init__(self, *, fails_conversion: bool = False, fails_normalize: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.fails_conversion = fails_conversion
        self.fails_normalize = fails_normalize

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(argv)
        if any("fake-convert" in part for part in argv):
            if self.fails_conversion:
                return subprocess.CompletedProcess(argv, 1, "", "ffmpeg: no such codec")
            source = next(Path(argv[1]).iterdir())
            (Path(argv[2]) / f"{source.stem}.aiff").write_bytes(b"aiff")
            source.unlink()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if any("fake-normalize" in part for part in argv):
            if self.fails_normalize:
                return subprocess.CompletedProcess(argv, 1, "", "ffmpeg not found")
            return subprocess.CompletedProcess(argv, 0, "normalized 1 file", "")
        raise AssertionError(f"unexpected command: {argv}")

    def conversions(self) -> list[list[str]]:
        return [argv for argv in self.commands if any("fake-convert" in part for part in argv)]

    def normalizations(self) -> list[list[str]]:
        return [argv for argv in self.commands if any("fake-normalize" in part for part in argv)]


def job_with(config: config_module.Config, records: list[state.Record]) -> state.Job:
    job = state.create(config, "test")
    job.write_records(records)
    return job


def staged(tmp_path: Path, name: str, content: bytes = b"audio") -> str:
    path = tmp_path / "staging" / name
    path.write_bytes(content)
    return str(path)


# ── the name the collection uses ─────────────────────────────────────────────


def test_the_name_follows_the_house_convention():
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred")
    assert process.filename_for(record, ".aiff") == "Kiasmos - Blurred.aiff"


def test_the_mix_goes_in_parentheses():
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", mix="Extended Mix")
    assert process.filename_for(record, ".flac") == "Kiasmos - Blurred (Extended Mix).flac"


def test_a_whitelabel_is_named_by_its_title_alone():
    record = state.Record(idx=1, artist="", title="Untitled Dubplate")
    assert process.filename_for(record, ".wav") == "Untitled Dubplate.wav"


def test_characters_a_filesystem_will_not_take_are_replaced():
    """Replaced rather than dropped, so two differently punctuated titles cannot collapse onto one name."""
    record = state.Record(idx=1, artist="AC/DC", title="Who Made Who: Live")
    assert process.filename_for(record, ".aiff") == "AC-DC - Who Made Who- Live.aiff"


def test_a_name_with_nothing_in_it_still_has_one():
    assert process.filename_for(state.Record(idx=1, artist="", title="   "), ".aiff") == "untitled.aiff"


# ── the lanes ────────────────────────────────────────────────────────────────


def test_a_lossy_file_goes_to_the_lossy_lane_untouched(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", local_path=staged(tmp_path, "08 - Blurred.m4a"),
                          status=state.DOWNLOADED, format="m4a")
    job = job_with(config, [record])

    process.run(config, job, runner=runner, log=lambda *_: None)

    assert runner.conversions() == [], "lossy is never converted (the design notes)"
    assert (config.lossy / "Kiasmos - Blurred.m4a").is_file()
    assert not (config.lossless / "Kiasmos - Blurred.aiff").exists()


def test_a_lossless_file_is_converted_into_the_lossless_lane(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    source = staged(tmp_path, "08-ufo95-solution_(original_mix).flac")
    record = state.Record(idx=1, artist="UFO95", title="Solution", mix="Original Mix",
                          local_path=source, status=state.DOWNLOADED, format="flac")
    job = job_with(config, [record])

    process.run(config, job, runner=runner, log=lambda *_: None)

    landed = config.lossless / "UFO95 - Solution (Original Mix).aiff"
    assert landed.is_file()
    assert not Path(source).exists(), "the converter consumes its input"
    assert job.records()[0].local_path == str(landed), "the record follows the file"
    assert runner.conversions()[0][2] == str(Path(source).parent)


def test_a_file_already_in_aiff_is_not_converted_again(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "Kiasmos - Blurred.aiff"),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    process.run(config, job, runner=runner, log=lambda *_: None)

    assert runner.conversions() == []
    assert (config.lossless / "Kiasmos - Blurred.aiff").is_file()


def test_a_name_already_taken_is_refused_rather_than_overwritten(tmp_path):
    """The dedupe should have caught it; if it did not, replacing the file would destroy it."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    existing = config.lossless / "Kiasmos - Blurred.aiff"
    existing.write_bytes(b"the one already there")
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "Kiasmos - Blurred.aiff"),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    summary = process.run(config, job, runner=FakeRunner(), log=lambda *_: None)

    assert summary.failed == [1]
    assert existing.read_bytes() == b"the one already there"
    assert "already there" in job.records()[0].notes


def test_a_staged_file_that_is_gone_is_reported(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(tmp_path / "staging" / "vanished.flac"),
                          status=state.DOWNLOADED, format="flac")
    job = job_with(config, [record])

    summary = process.run(config, job, runner=FakeRunner(), log=lambda *_: None)

    assert summary.failed == [1]
    assert "is gone" in job.records()[0].notes


def test_a_failed_conversion_puts_the_file_back(tmp_path):
    """The real converter deletes its input, so a failure must not be allowed to eat the download."""
    config = make_config(tmp_path)
    runner = FakeRunner(fails_conversion=True)
    source = Path(staged(tmp_path, "Kiasmos - Blurred.flac"))
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", local_path=str(source),
                          status=state.DOWNLOADED, format="flac")
    job = job_with(config, [record])

    summary = process.run(config, job, runner=runner, log=lambda *_: None)

    assert summary.failed == [1]
    assert source.is_file(), "the download must survive a failed conversion"
    assert "conversion failed" in job.records()[0].notes
    assert not list(tmp_path.glob("music-agent-convert-*")), "the pen is cleaned up"


def test_only_downloaded_records_are_processed(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "x.flac"), status=state.PENDING)
    job = job_with(config, [record])

    summary = process.run(config, job, runner=FakeRunner(), log=lambda *_: None)

    assert summary.placed == []
    assert job.records()[0].status == state.PENDING


# ── loudness ─────────────────────────────────────────────────────────────────


def test_the_normalizer_is_given_the_directory_and_the_settings(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "Kiasmos - Blurred.aiff"),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    process.run(config, job, runner=runner, log=lambda *_: None)

    (argv,) = runner.normalizations()
    assert argv[2] == str(config.lossless), "a directory, because that is where it keeps its state"
    assert argv[3:] == ["--target-lufs", "-14.0", "--true-peak", "-1.0", "--tolerance", "0.5"]
    assert job.records()[0].status == state.NORMALIZED


def test_the_normalizer_runs_once_for_everything_in_a_lane(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    records = [
        state.Record(idx=1, artist="Kiasmos", title="Blurred",
                     local_path=staged(tmp_path, "Kiasmos - Blurred.aiff"), status=state.DOWNLOADED,
                     format="aiff"),
        state.Record(idx=2, artist="UFO95", title="Solution",
                     local_path=staged(tmp_path, "UFO95 - Solution.aiff"), status=state.DOWNLOADED,
                     format="aiff"),
        state.Record(idx=3, artist="Villalobos", title="Fizheuer",
                     local_path=staged(tmp_path, "Villalobos - Fizheuer.m4a"), status=state.DOWNLOADED,
                     format="m4a"),
    ]
    job = job_with(config, records)

    summary = process.run(config, job, runner=runner, log=lambda *_: None)

    assert len(runner.normalizations()) == 2, "one per lane that received something"
    assert summary.normalized == [1, 2, 3]
    assert [record.status for record in job.records()] == [state.NORMALIZED] * 3
    assert job.finished()


def test_a_normalize_failure_leaves_those_records_to_retry(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner(fails_normalize=True)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "Kiasmos - Blurred.aiff"),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    summary = process.run(config, job, runner=runner, log=lambda *_: None)

    assert summary.failed == [1]
    assert job.records()[0].status == state.RETRY_LATER
    assert not job.finished()
    assert (config.lossless / "Kiasmos - Blurred.aiff").is_file(), "it is placed either way"
