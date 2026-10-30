"""The tracklist reader: one reading of the input, and the shapes it deliberately refuses to guess."""

from __future__ import annotations

from music_agent import tracklist


def test_lines_give_an_artist_and_a_title():
    requests = tracklist.parse("Kiasmos - Blurred\nNtogn - Mines\n")
    assert [(r.artist, r.title) for r in requests] == [("Kiasmos", "Blurred"), ("Ntogn", "Mines")]


def test_a_mix_suffix_becomes_the_mix_not_the_title():
    """A candidate's title is compared with its own mix taken off, so a title that kept the mix would
    match nothing."""
    (request,) = tracklist.parse("Kiasmos - Blurred (Extended Mix)")
    assert (request.title, request.mix) == ("Blurred", "Extended Mix")


def test_a_dash_written_version_becomes_the_mix():
    """A line written the way a shop writes it carries its version after a dash, and the version has to
    reach the mix or the file that is the entry is refused as a different version."""
    (request,) = tracklist.parse("Kiasmos - Faded - Extended Mix")
    assert (request.title, request.mix) == ("Faded", "Extended Mix")
    (master,) = tracklist.parse("Kiasmos - Dreams - 2004 Remaster")
    assert (master.title, master.mix) == ("Dreams", "")


def test_a_label_code_tail_is_dropped_from_the_title():
    (request,) = tracklist.parse("Lask - Black Ibis [PRA005]")
    assert request.title == "Black Ibis"


def test_a_numbered_list_entry_is_read():
    assert tracklist.parse("01. Kiasmos - Blurred")[0].artist == "Kiasmos"
    assert tracklist.parse("7) Kiasmos - Blurred")[0].artist == "Kiasmos"
    assert tracklist.parse("A1 - Kiasmos - Blurred")[0].artist == "Kiasmos"


def test_a_bare_number_is_left_alone_because_it_can_be_an_artist():
    """`808 State` is an artist. The asymmetry is deliberate: missing a track number costs a lookup that
    finds nothing and leaves the track review-only, while stripping digits off an artist asks
    MusicBrainz about a different artist — and a wrong reference rejects every correct file."""
    assert tracklist.parse("808 State - Pacific")[0].artist == "808 State"
    assert tracklist.parse("12 Kiasmos - Blurred")[0].artist == "12 Kiasmos"


def test_comments_and_blank_lines_are_skipped():
    text = "\n# a note\nKiasmos - Blurred\n\n   \n# another\n"
    assert len(tracklist.parse(text)) == 1


def test_a_line_with_no_artist_keeps_its_title_and_no_more():
    (request,) = tracklist.parse("Untitled Dubplate")
    assert (request.artist, request.title) == ("", "Untitled Dubplate")


def test_a_csv_export_is_read_by_its_header():
    text = "Track Name,Artist Name,Album\nBlurred,Kiasmos,Blurred\nMines,Ntogn,Dust\n"
    requests = tracklist.parse(text)
    assert [(r.artist, r.title) for r in requests] == [("Kiasmos", "Blurred"), ("Ntogn", "Mines")]


def test_a_quoted_comma_survives_the_csv():
    (request,) = tracklist.parse('Title,Artist\n"Blurred, Pt. 2",Kiasmos\n')
    assert (request.artist, request.title) == ("Kiasmos", "Blurred, Pt. 2")


def test_a_header_needs_both_named_columns_before_the_reader_switches():
    """Otherwise an ordinary line that happens to contain a comma is silently reinterpreted."""
    (request,) = tracklist.parse("Kiasmos - Blurred, Pt. 2")
    assert (request.artist, request.title) == ("Kiasmos", "Blurred, Pt. 2")


def test_album_artist_is_not_mistaken_for_the_artist_column():
    """Exact column names, so an export carrying both reads the right one."""
    text = "Title,Album Artist,Artist\nBlurred,Various Artists,Kiasmos\n"
    assert tracklist.parse(text)[0].artist == "Kiasmos"


def test_a_short_csv_row_is_skipped_rather_than_guessed():
    text = "Title,Artist\nBlurred,Kiasmos\nOrphan\n"
    assert len(tracklist.parse(text)) == 1


def test_a_csv_row_with_no_title_is_skipped():
    text = "Title,Artist\n,Kiasmos\nBlurred,Kiasmos\n"
    assert len(tracklist.parse(text)) == 1


def test_an_empty_file_asks_for_nothing():
    assert tracklist.parse("") == []
    assert tracklist.parse("# only a comment\n") == []


def test_reading_a_file(tmp_path):
    path = tmp_path / "tracks.txt"
    path.write_text("Kiasmos - Blurred\n", encoding="utf-8")
    assert tracklist.read(path)[0].title == "Blurred"


def test_reading_a_lane_directory(tmp_path):
    """The lane is already the list: `--tracks "~/Music/Losy Tracks"` asks for every lossy copy it holds,
    named the way the pipeline named it, with nothing exported in between."""
    (tmp_path / "Kiasmos - Blurred.mp3").write_bytes(b"")
    (tmp_path / "Ntogn - Mines (Original Mix).flac").write_bytes(b"")
    (tmp_path / ".DS_Store").write_bytes(b"")
    (tmp_path / "cover.jpg").write_bytes(b"")

    requests = tracklist.read(tmp_path)

    assert [(r.artist, r.title, r.mix) for r in requests] == [
        ("Kiasmos", "Blurred", ""), ("Ntogn", "Mines", "Original Mix")]


def test_a_tracklist_written_with_a_byte_order_mark_loses_it(tmp_path):
    """A spreadsheet export carries `\ufeff` on the first line, and `strip()` does not remove it."""
    path = tmp_path / "list.txt"
    path.write_text("Kiasmos - Blurred\n", encoding="utf-8-sig")

    (request,) = tracklist.read(path)

    assert (request.artist, request.title) == ("Kiasmos", "Blurred")
