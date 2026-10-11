"""Writing tags onto a file, in the frame set that file already reads.

Ticket 07 fixes the fields: title, artist, album, album artist, year, label, catalogue number, genre, and
the MusicBrainz ids — and never BPM or key, whose source is rekordbox's own analysis.

Ticket 02 decides the frame set rather than taste: rekordbox reads **ID3v2** for AIFF, so an AIFF gets ID3
frames. The lossy lane keeps the container it already is — MP4 atoms for an m4a, ID3 for an mp3 — because
that is what those files are.

Two things the library itself taught, from reading real files rather than assuming them:

- The AIFFs already carry `TKEY` and `TBPM` — Traktor's key and tempo, beside a `PRIV:TRAKTOR4` blob — and
  the design notes says those are never ours to write. So the writer sets the frames it owns and leaves every
  other one exactly as it found it, which is why losing one is not a risk it takes: it never rewrites the
  whole tag.
- An existing genre is **never replaced** (the design notes). A run across the library therefore cannot re-genre
  a track, which is right: a genre cannot be undone after import, so changing one has to be deliberate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

# The frames this project owns in an ID3 tag, and the freeform ones it adds beside them. They follow
# MusicBrainz's own tagging guidelines, so anything else reading these files understands them.
_ID3_FRAMES = {
    "title": "TIT2", "artist": "TPE1", "album": "TALB", "album_artist": "TPE2",
    "year": "TDRC", "label": "TPUB", "genre": "TCON",
}
_ID3_FREEFORM = {
    "catalog_number": "CATALOGNUMBER",
    "release_id": "MusicBrainz Album Id",
    "release_group_id": "MusicBrainz Release Group Id",
}
_ID3_RECORDING = "http://musicbrainz.org"

# The equivalent atoms in an MP4 container.
_MP4_ATOMS = {
    "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART",
    "year": "\xa9day", "genre": "\xa9gen",
}
_MP4_FREEFORM = {
    "catalog_number": "CATALOGNUMBER",
    "label": "LABEL",
    "release_id": "MusicBrainz Album Id",
    "release_group_id": "MusicBrainz Release Group Id",
    "recording_id": "MusicBrainz Track Id",
}


class TagError(RuntimeError):
    """A file could not be tagged."""


@dataclass(frozen=True)
class Tags:
    """What to write. Empty fields are left alone rather than written blank."""

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    label: str = ""
    catalog_number: str = ""
    genre: str = ""
    recording_id: str = ""
    release_id: str = ""
    release_group_id: str = ""

    def filled(self) -> dict[str, str]:
        return {name: value for name, value in asdict(self).items() if value}


def read(path: Path) -> Tags:
    """What the file already says, as far as this project's own fields go.

    The MusicBrainz ids written here are the cache the design notes asked for: a file that already carries them
    is not identified again, so nothing is asked for something already known.
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return Tags()

    def first(keys: list[str]) -> str:
        for key in keys:
            if key in tags:
                value = tags[key]
                text = getattr(value, "text", None)
                if text:
                    return str(text[0])
                if isinstance(value, list) and value:
                    return str(value[0])
                if isinstance(value, str):
                    return value
        return ""

    return Tags(
        title=first(["TIT2", "\xa9nam"]),
        artist=first(["TPE1", "\xa9ART"]),
        album=first(["TALB", "\xa9alb"]),
        album_artist=first(["TPE2", "aART"]),
        year=first(["TDRC", "\xa9day"]),
        label=first(["TPUB", "----:com.apple.iTunes:LABEL"]),
        genre=first(["TCON", "\xa9gen"]),
        catalog_number=first(["TXXX:CATALOGNUMBER", "----:com.apple.iTunes:CATALOGNUMBER"]),
        release_id=first(["TXXX:MusicBrainz Album Id", "----:com.apple.iTunes:MusicBrainz Album Id"]),
        release_group_id=first(["TXXX:MusicBrainz Release Group Id",
                                "----:com.apple.iTunes:MusicBrainz Release Group Id"]),
    )


def write(path: Path, tags: Tags, *, log=lambda *_: None) -> list[str]:
    """Set the frames this project owns. Returns the names of the ones that changed something."""
    suffix = path.suffix.lower()
    if suffix in {".aiff", ".aif", ".mp3"}:
        return _write_id3(path, tags)
    if suffix in {".m4a", ".mp4"}:
        return _write_mp4(path, tags)
    return _write_vorbis(path, tags, log)


def _write_id3(path: Path, tags: Tags) -> list[str]:
    from mutagen import File as MutagenFile
    from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPUB, TXXX, UFID

    frames = {"TIT2": TIT2, "TPE1": TPE1, "TALB": TALB, "TPE2": TPE2,
              "TDRC": TDRC, "TPUB": TPUB, "TCON": TCON}
    audio = MutagenFile(path)
    if audio is None:
        raise TagError(f"mutagen cannot open {path}")
    if audio.tags is None:
        audio.add_tags()

    written = []
    for name, value in tags.filled().items():
        if name in _ID3_FRAMES:
            if name == "genre" and "TCON" in audio.tags:
                continue                        # the design notes: an existing genre is never replaced
            audio.tags[_ID3_FRAMES[name]] = frames[_ID3_FRAMES[name]](encoding=3, text=[value])
            written.append(name)
        elif name in _ID3_FREEFORM:
            key = f"TXXX:{_ID3_FREEFORM[name]}"
            audio.tags[key] = TXXX(encoding=3, desc=_ID3_FREEFORM[name], text=[value])
            written.append(name)
        elif name == "recording_id":
            audio.tags[f"UFID:{_ID3_RECORDING}"] = UFID(owner=_ID3_RECORDING, data=value.encode())
            written.append(name)
    audio.save()
    return written


def _write_mp4(path: Path, tags: Tags) -> list[str]:
    from mutagen.mp4 import MP4, MP4FreeForm

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()

    written = []
    for name, value in tags.filled().items():
        if name in _MP4_ATOMS:
            if name == "genre" and "\xa9gen" in audio.tags:
                continue                        # the design notes, as above
            audio.tags[_MP4_ATOMS[name]] = [value]
            written.append(name)
        elif name in _MP4_FREEFORM:
            key = f"----:com.apple.iTunes:{_MP4_FREEFORM[name]}"
            audio.tags[key] = [MP4FreeForm(value.encode("utf-8"))]
            written.append(name)
    audio.save()
    return written


def _write_vorbis(path: Path, tags: Tags, log) -> list[str]:
    """Everything else the library holds — an ogg, an opus, a flac that never needed converting.

    The generic interface writes the ordinary fields and not the ids, which is a smaller cache than the
    other two formats keep. Nothing in the library is in this shape today; it is here so that a file in it
    is tagged rather than refused.
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise TagError(f"no tagger knows how to write {path.suffix} files ({path})")
    if audio.tags is None:
        audio.add_tags()

    keys = {"title": "title", "artist": "artist", "album": "album", "album_artist": "albumartist",
            "year": "date", "genre": "genre", "catalog_number": "catalognumber", "label": "label"}
    written = []
    for name, value in tags.filled().items():
        if name not in keys:
            continue
        if name == "genre" and "genre" in audio.tags:
            continue
        audio.tags[keys[name]] = [value]
        written.append(name)
    audio.save()
    log(f"      wrote {sorted(written)} without the MusicBrainz ids ({path.suffix} has no place this "
        "writer knows for them)")
    return written
