"""The processing stage: the two lanes, the house naming, and the order the canonical tools run in.

The runner is injected, so these tests read the exact command lines the stage would issue — and one of
them checks the thing most likely to go wrong quietly: a conversion that fails must put the file back,
because the real converter deletes its input rather than leaving it behind.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from audio_files import aiff, mp4

from music_agent import config as config_module
from music_agent import identify, process, state, tag
from music_agent.parsing import fold


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
            aiff(Path(argv[2]) / f"{source.stem}.aiff")   # real: the tagger opens it afterwards
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
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(mp4(tmp_path / "staging" / "08 - Blurred.m4a")),
                          status=state.DOWNLOADED, format="m4a")
    job = job_with(config, [record])

    process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert runner.conversions() == [], "lossy is never converted"
    assert (config.lossy / "Kiasmos - Blurred.m4a").is_file()
    assert not (config.lossless / "Kiasmos - Blurred.aiff").exists()


def test_a_lossless_file_is_converted_into_the_lossless_lane(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    source = staged(tmp_path, "08-ufo95-solution_(original_mix).flac")
    record = state.Record(idx=1, artist="UFO95", title="Solution", mix="Original Mix",
                          local_path=source, status=state.DOWNLOADED, format="flac")
    job = job_with(config, [record])

    process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    landed = config.lossless / "UFO95 - Solution (Original Mix).aiff"
    assert landed.is_file()
    assert not Path(source).exists(), "the converter consumes its input"
    assert job.records()[0].local_path == str(landed), "the record follows the file"
    assert runner.conversions()[0][2] == str(Path(source).parent)


def test_a_file_already_in_aiff_is_not_converted_again(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Kiasmos - Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert runner.conversions() == []
    assert (config.lossless / "Kiasmos - Blurred.aiff").is_file()


def test_a_file_named_aif_is_placed_as_aiff(tmp_path):
    """One extension for one format, because pyrekordbox's own `add_content` derives its file type from the
    upper-cased suffix and there is no `AIF` member — a `.aif` cannot be added at all.

    Renamed, never converted: `.aif` is an AIFF by another name, so the audio is already what the collection
    wants and the converter would only re-encode it.
    """
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Maryam Saleh", title="El Fetra",
                          local_path=str(aiff(tmp_path / "staging" / "Maryam Saleh - El Fetra.aif")),
                          status=state.DOWNLOADED, format="aif")
    job = job_with(config, [record])

    process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert runner.conversions() == [], "an .aif is an AIFF, so there is nothing to convert"
    assert (config.lossless / "Maryam Saleh - El Fetra.aiff").is_file()
    assert not (config.lossless / "Maryam Saleh - El Fetra.aif").exists()
    assert job.records()[0].local_path == str(config.lossless / "Maryam Saleh - El Fetra.aiff"), \
        "the record follows the file"


def test_a_name_the_lane_already_holds_deletes_the_download_instead_of_dangling_in_staging(tmp_path):
    """The dedupe should have caught it, and `held` will not skip a track whose length was never resolved, so
    a duplicate can arrive. The file already there is never overwritten, and the download is not left sitting
    in staging either: nothing keeps a second copy of anything."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True)
    existing = config.lossless / "Kiasmos - Blurred.aiff"
    existing.write_bytes(b"the one already there")
    source = Path(staged(tmp_path, "Kiasmos - Blurred.aiff"))
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", local_path=str(source),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    summary = process.run(config, job, tools=process.Tools(runner=FakeRunner()), log=lambda *_: None)

    assert summary.already == [1]
    assert summary.failed == []
    assert existing.read_bytes() == b"the one already there", "nothing was overwritten"
    assert not source.exists(), "the duplicate is deleted rather than left dangling in staging"
    assert job.records()[0].status == state.FOUND_IN_LIBRARY
    assert job.records()[0].local_path == ""
    assert "already there" in job.records()[0].notes


def test_a_staged_file_that_is_gone_is_reported(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(tmp_path / "staging" / "vanished.flac"),
                          status=state.DOWNLOADED, format="flac")
    job = job_with(config, [record])

    summary = process.run(config, job, tools=process.Tools(runner=FakeRunner()), log=lambda *_: None)

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

    summary = process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert summary.failed == [1]
    assert source.is_file(), "the download must survive a failed conversion"
    assert "conversion failed" in job.records()[0].notes
    assert not list(tmp_path.glob("music-agent-convert-*")), "the pen is cleaned up"


def test_only_downloaded_records_are_processed(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=staged(tmp_path, "x.flac"), status=state.PENDING)
    job = job_with(config, [record])

    summary = process.run(config, job, tools=process.Tools(runner=FakeRunner()), log=lambda *_: None)

    assert summary.placed == []
    assert job.records()[0].status == state.PENDING


# ── loudness ─────────────────────────────────────────────────────────────────


def test_the_normalizer_is_given_the_directory_and_the_settings(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Kiasmos - Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    (argv,) = runner.normalizations()
    assert argv[2] == str(config.lossless), "a directory, because that is where it keeps its state"
    assert argv[3:] == ["--target-lufs", "-14.0", "--true-peak", "-1.0", "--tolerance", "0.5"]
    assert job.records()[0].status == state.TAGGED


def test_the_normalizer_runs_once_for_everything_in_a_lane(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    records = [
        state.Record(idx=1, artist="Kiasmos", title="Blurred",
                     local_path=str(aiff(tmp_path / "staging" / "Kiasmos - Blurred.aiff")), status=state.DOWNLOADED,
                     format="aiff"),
        state.Record(idx=2, artist="UFO95", title="Solution",
                     local_path=str(aiff(tmp_path / "staging" / "UFO95 - Solution.aiff")), status=state.DOWNLOADED,
                     format="aiff"),
        state.Record(idx=3, artist="Villalobos", title="Fizheuer",
                     local_path=str(mp4(tmp_path / "staging" / "Villalobos - Fizheuer.m4a")), status=state.DOWNLOADED,
                     format="m4a"),
    ]
    job = job_with(config, records)

    summary = process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert len(runner.normalizations()) == 2, "one per lane that received something"
    assert summary.normalized == [1, 2, 3]
    assert [record.status for record in job.records()] == [state.TAGGED] * 3
    assert not job.finished(), "the run is finished by the import, not here"



def test_a_normalize_failure_leaves_those_records_to_retry(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner(fails_normalize=True)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Kiasmos - Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])

    summary = process.run(config, job, tools=process.Tools(runner=runner), log=lambda *_: None)

    assert summary.failed == [1]
    assert job.records()[0].status == state.RETRY_LATER
    assert not job.finished()
    assert (config.lossless / "Kiasmos - Blurred.aiff").is_file(), "it is placed either way"


# ── identifying, genring and tagging ─────────────────────────────────────────


def identification(artist: str = "Kiasmos", title: str = "Blurred", *, styles: tuple[str, ...] = (),
                   genres: tuple[str, ...] = (), seconds: int = 449, broken: bool = False):
    """Seams that answer at once, so the stage runs without any service being reachable."""

    class Hearing:
        def listen(self, path):
            if broken:
                raise identify.IdentifyError("fpcalc is not installed")
            return "FINGERPRINT", seconds

    class Matches:
        def match(self, fingerprint, seconds_):
            return [identify.Match(score=0.98, recordings=(identify.Candidate(
                id="rec-1", title=title, artists=(artist,), duration=float(seconds),
                album="Blurred", album_artists=(artist,)),))]

    class Brainz:
        """Shaped like the live one, which is the point: the release and the genres are only asked about
        when the recording names them, so a thinner fake would make the stage look like it skips them."""

        def recording(self, recording_id):
            return identify.Candidate(id=recording_id, title=title, artists=(artist,),
                                      releases=(identify.Release(id="rel-1",
                                                                 release_group_id="rg-1"),))

        def search(self, request):
            if broken:
                raise RuntimeError("musicbrainz is unreachable from here")
            return []

        def release(self, release_id):
            return identify.Release(id="rel-1", title="Blurred", date="2017-01-02",
                                    label="Erased Tapes", catalog_number="ERATP001",
                                    release_group_id="rg-1")

        def genres(self, release_group_id):
            return genres

    class Discogs:
        def styles(self, artist_, title_):
            return styles

    return Hearing(), Matches(), Brainz(), Discogs()


def with_identification(runner, *, styles: tuple[str, ...] = (), genres: tuple[str, ...] = (),
                        vocabulary: list[str] | None = None, mappings=None, broken: bool = False):
    hearing, matches, brainz, discogs = identification(styles=styles, genres=genres, broken=broken)
    return process.Tools(runner=runner, hearing=hearing, matches=matches, brainz=brainz,
                         discogs=discogs, vocabulary=vocabulary or ["Techno", "Jazz"],
                         mappings=mappings or {})


def test_a_track_is_identified_genred_and_tagged_on_its_way_through(tmp_path):
    """The whole stage, with the services faked and everything else real — including the file."""
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner(), styles=("Minimal Techno",),
                                mappings={fold("minimal techno"): "Techno"})

    summary = process.run(config, job, tools=tools, log=lambda *_: None)

    landed = config.lossless / "Kiasmos - Blurred.aiff"
    assert summary.tagged == [1], summary.report()
    assert job.records()[0].status == state.TAGGED
    assert job.records()[0].genre == "Techno"

    written = tag.read(landed)
    assert (written.title, written.artist) == ("Blurred", "Kiasmos")
    assert written.album == "Blurred"
    assert written.genre == "Techno"
    assert (written.label, written.catalog_number, written.year) == ("Erased Tapes", "ERATP001", "2017")
    assert written.release_id == "rel-1", "the ids are the cache a re-run reads"


def test_a_genre_nothing_maps_is_left_for_the_skill_and_said_so(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner(), styles=("Krautrock",), genres=("Field Recording",))

    summary = process.run(config, job, tools=tools, log=lambda *_: None)

    assert summary.ungenred == [1]
    assert job.records()[0].status == state.TAGGED, "tagged, just without a genre"
    assert job.records()[0].genre == ""
    assert tag.read(config.lossless / "Kiasmos - Blurred.aiff").genre == ""


def test_a_track_the_services_know_nothing_about_says_so(tmp_path):
    """2026-10-13: with nothing offered the stage logged no genre line at all, so a reader of the run could
    not tell "no genre" from "no answer". The log is the only record of a track's source genres — the written
    tag keeps the family and nothing else — and a rule that reads sources off the run needs every track's own
    line, including this one."""
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner())
    said: list[str] = []

    summary = process.run(config, job, tools=tools, log=said.append)

    assert summary.ungenred == [1], summary.report()
    assert any("no genre offered" in line for line in said), said


def test_a_genre_the_table_knows_is_taken_from_the_services(tmp_path):
    """MusicBrainz's genres are the weak second opinion behind Discogs styles."""
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner(), genres=("jazz",), mappings={fold("jazz"): "Jazz"})

    process.run(config, job, tools=tools, log=lambda *_: None)

    assert job.records()[0].genre == "Jazz"


def test_a_service_that_fails_leaves_the_name_doing_the_work(tmp_path):
    """The last resort, reached because a service was unreachable rather than because it was asked and
    had nothing."""
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner(), broken=True)

    summary = process.run(config, job, tools=tools, log=lambda *_: None)

    assert summary.failed == []
    assert job.records()[0].status == state.TAGGED
    written = tag.read(config.lossless / "Kiasmos - Blurred.aiff")
    assert (written.title, written.artist) == ("Blurred", "Kiasmos"), "the request's own name"
    assert written.album == "", "and nothing invented beyond it"


# ── re-tagging what a changed table left behind ──────────────────────────────


# ── rebuilding a record from its file ────────────────────────────


def test_reconcile_reads_a_record_back_off_the_file_this_run_downloaded(tmp_path):
    """A status is memory and the file is the truth. The trap it must not fall into is the one that
    matched by name across the whole library — a track already held is not this run's."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True, exist_ok=True)
    placed = aiff(config.lossless / "Kiasmos - Blurred.aiff")
    tag.write(placed, tag.Tags(title="Blurred", artist="Kiasmos", genre="Techno"))
    his_own = config.lossy / "Somebody - Elsewhere.mp3"
    his_own.parent.mkdir(parents=True, exist_ok=True)
    his_own.write_bytes(b"audio")
    fetched = state.Record(idx=1, artist="Kiasmos", title="Blurred", status=state.PENDING)
    never = state.Record(idx=2, artist="Somebody", title="Elsewhere", status=state.PENDING)
    job = job_with(config, [fetched, never])
    job.append_event("downloaded", idx=1, path=str(config.staging / "Kiasmos - Blurred.flac"))

    summary = process.reconcile(config, job, log=lambda *_: None)

    assert summary.reconciled == [1]
    assert job.records()[0].status == state.TAGGED
    assert job.records()[0].local_path == str(placed)
    assert job.records()[0].genre == "Techno"
    assert job.records()[1].status == state.PENDING, "a track the run never fetched is left alone"
    assert job.records()[1].local_path == "", "and not adopted by name"


def test_reconcile_leaves_an_imported_track_imported(tmp_path):
    """The file in the lane is not the whole truth about a track the import has finished: the collection's row
    is. Rebuilt from the file alone, 49 imported tracks came back as merely tagged."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True, exist_ok=True)
    placed = aiff(config.lossless / "Kiasmos - Blurred.aiff")
    tag.write(placed, tag.Tags(title="Blurred", artist="Kiasmos", genre="Techno"))
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", status=state.IMPORTED,
                          local_path=str(placed), genre="Techno")
    job = job_with(config, [record])
    job.append_event("downloaded", idx=1, path=str(config.staging / "Kiasmos - Blurred.flac"))

    summary = process.reconcile(config, job)

    assert summary.already == [1], "nothing was rebuilt, so nothing was written"
    assert summary.reconciled == []
    assert job.records()[0].status == state.IMPORTED


def test_reconcile_still_repoints_an_imported_record_whose_path_was_lost(tmp_path):
    """The other half: the status is kept, the path is repaired."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True, exist_ok=True)
    placed = aiff(config.lossless / "Kiasmos - Blurred.aiff")
    tag.write(placed, tag.Tags(title="Blurred", artist="Kiasmos"))
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", status=state.IMPORTED,
                          local_path=str(config.staging / "stale.flac"))
    job = job_with(config, [record])
    job.append_event("downloaded", idx=1, path=str(config.staging / "Kiasmos - Blurred.flac"))

    summary = process.reconcile(config, job)

    assert summary.reconciled == [1]
    after = job.records()[0]
    assert after.local_path == str(placed), "the record follows the file"
    assert after.status == state.IMPORTED, "and the collection's row is still what it points at"


def test_reconcile_names_what_it_cannot_find_rather_than_guessing(tmp_path):
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", status=state.RETRY_LATER)
    job = job_with(config, [record])
    job.append_event("downloaded", idx=1, path=str(config.staging / "Kiasmos - Blurred.flac"))

    summary = process.reconcile(config, job, log=lambda *_: None)

    assert summary.missing == [1]
    assert summary.reconciled == []
    assert job.records()[0].status == state.RETRY_LATER, "nothing is written about a file that is not there"


def test_a_re_tag_replaces_the_genre_the_file_already_carries(tmp_path):
    """The table moved under a tagged file, and a deliberate pass is what reaches the file."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True, exist_ok=True)
    path = aiff(config.lossless / "Kiasmos - Blurred.aiff")
    tag.write(path, tag.Tags(title="Blurred", artist="Kiasmos", genre="Pop"))
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred", local_path=str(path),
                          status=state.TAGGED)
    job = job_with(config, [record])
    hearing, matches, brainz, discogs = identification(genres=("jazz",))
    tools = process.Tools(hearing=hearing, matches=matches, brainz=brainz, discogs=discogs,
                          vocabulary=["Jazz & Soul"], mappings={"jazz": "Jazz & Soul"})

    summary = process.retag(job, tools=tools, log=lambda *_: None)

    assert tag.read(path).genre == "Jazz & Soul", "written over the name the old table left"
    assert job.records()[0].genre == "Jazz & Soul", "the record follows the file, as it does in a run"
    assert summary.retagged == [1]


def test_a_re_tag_that_resolves_to_nothing_takes_the_old_name_off(tmp_path):
    """The import reads the file, so a name the collection has not got has to come off."""
    config = make_config(tmp_path)
    config.lossless.mkdir(parents=True, exist_ok=True)
    path = aiff(config.lossless / "Ivete Sangalo - Raiz.aiff")
    tag.write(path, tag.Tags(title="Raiz", artist="Ivete Sangalo", genre="Baiuca;Galicia;Music"))
    record = state.Record(idx=1, artist="Ivete Sangalo", title="Raiz", local_path=str(path),
                          status=state.TAGGED)
    job = job_with(config, [record])
    hearing, matches, brainz, discogs = identification(styles=("krautrock",))
    tools = process.Tools(hearing=hearing, matches=matches, brainz=brainz, discogs=discogs,
                          vocabulary=["Jazz & Soul"], mappings={"jazz": "Jazz & Soul"})

    summary = process.retag(job, tools=tools, log=lambda *_: None)

    assert tag.read(path).genre == ""
    assert summary.cleared == [1]
    assert summary.retagged == []


def test_a_name_that_fitted_nothing_is_recorded_so_it_can_be_counted(tmp_path):
    """Five tracks carrying a name that fits nothing is what turns it into a proposal."""
    config = make_config(tmp_path)
    record = state.Record(idx=1, artist="Kiasmos", title="Blurred",
                          local_path=str(aiff(tmp_path / "staging" / "Blurred.aiff")),
                          status=state.DOWNLOADED, format="aiff")
    job = job_with(config, [record])
    tools = with_identification(FakeRunner(), styles=("Krautrock", "Kosmische"))

    process.run(config, job, tools=tools, log=lambda *_: None)

    recorded = [entry for entry in job.events() if entry["event"] == state.UNGENERED_EVENT]
    assert len(recorded) == 1
    assert recorded[0]["names"] == ["Krautrock", "Kosmische"]
