"""Writing tags onto a file, in the frame set that file already reads.

rekordbox reads ID3v2 for AIFF, so an AIFF gets ID3 frames; the lossy lane keeps the container it already
is — MP4 atoms for an m4a, ID3 for an mp3. The writer sets only the frames this project owns and leaves
every other one exactly as it found it, never rewriting the whole tag, so Traktor's `TKEY`/`TBPM` survive.
An existing genre is never replaced unless `write` is told to.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

# The frames this project owns in an ID3 tag; the freeform ones follow MusicBrainz's tagging guidelines
# so that anything else reading these files understands them.
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

    The MusicBrainz ids it returns are the cache: a file that already carries them is not identified again.
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    tags = getattr(audio, "tags", None)
    if tags is None:
        return Tags()

    def first(keys: list[str]) -> str:
        for key in keys:
            try:
                value = tags[key]
            except (KeyError, ValueError):
                # A container that will not take the key at all reads the same as one that does not carry
                # it: Vorbis raises ValueError for ID3's names rather than saying "not here".
                continue
            text = getattr(value, "text", None)
            if text:
                return str(text[0])
            if isinstance(value, list) and value:
                return str(value[0])
            if isinstance(value, str):
                return value
        return ""

    # Each list holds the container's own spellings: ID3 for AIFF, `©nam` for MP4, and the plain names a
    # Vorbis comment carries — which is what an ogg, an opus and a flac are written with.
    return Tags(
        title=first(["TIT2", "\xa9nam", "title"]),
        artist=first(["TPE1", "\xa9ART", "artist"]),
        album=first(["TALB", "\xa9alb", "album"]),
        album_artist=first(["TPE2", "aART", "albumartist"]),
        year=first(["TDRC", "\xa9day", "date"]),
        label=first(["TPUB", "----:com.apple.iTunes:LABEL", "label"]),
        genre=first(["TCON", "\xa9gen", "genre"]),
        catalog_number=first(["TXXX:CATALOGNUMBER", "----:com.apple.iTunes:CATALOGNUMBER", "catalognumber"]),
        release_id=first(["TXXX:MusicBrainz Album Id", "----:com.apple.iTunes:MusicBrainz Album Id"]),
        release_group_id=first(["TXXX:MusicBrainz Release Group Id",
                                "----:com.apple.iTunes:MusicBrainz Release Group Id"]),
    )


def write(path: Path, tags: Tags, *, replace_genre: bool = False, log=lambda *_: None) -> list[str]:
    """Set the frames this project owns. Returns the names of the ones that changed something.

    A run never replaces a genre, because a genre cannot be undone after import, so only `music-agent
    retag` passes `replace_genre` — which, when the tags carry no genre at all, takes an existing one *off*
    rather than leaving a name the collection has not got.
    """
    suffix = path.suffix.lower()
    if suffix in {".aiff", ".aif", ".mp3"}:
        return _write_id3(path, tags, replace_genre=replace_genre)
    if suffix in {".m4a", ".mp4"}:
        return _write_mp4(path, tags, replace_genre=replace_genre)
    return _write_vorbis(path, tags, replace_genre=replace_genre, log=log)


def _write_id3(path: Path, tags: Tags, *, replace_genre: bool = False) -> list[str]:
    from mutagen import File as MutagenFile
    from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPUB, TXXX, UFID

    frames = {"title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2,
              "year": TDRC, "label": TPUB, "genre": TCON}
    audio = MutagenFile(path)
    if audio is None:
        raise TagError(f"mutagen cannot open {path}")
    if audio.tags is None:
        audio.add_tags()

    filled = tags.filled()
    written = []
    if replace_genre and "genre" not in filled and "TCON" in audio.tags:
        del audio.tags["TCON"]                  # nothing resolves now: take the old name off
        written.append("genre")
    for name, value in filled.items():
        if name in _ID3_FRAMES:
            if name == "genre" and not replace_genre and "TCON" in audio.tags:
                continue                        # an existing genre is never replaced
            audio.tags[_ID3_FRAMES[name]] = frames[name](encoding=3, text=[value])
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


def _write_mp4(path: Path, tags: Tags, *, replace_genre: bool = False) -> list[str]:
    from mutagen.mp4 import MP4, MP4FreeForm

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()

    filled = tags.filled()
    written = []
    if replace_genre and "genre" not in filled and "\xa9gen" in audio.tags:
        del audio.tags["\xa9gen"]               # nothing resolves now: take the old name off
        written.append("genre")
    for name, value in filled.items():
        if name in _MP4_ATOMS:
            if name == "genre" and not replace_genre and "\xa9gen" in audio.tags:
                continue                        # an existing genre is never replaced
            audio.tags[_MP4_ATOMS[name]] = [value]
            written.append(name)
        elif name in _MP4_FREEFORM:
            key = f"----:com.apple.iTunes:{_MP4_FREEFORM[name]}"
            audio.tags[key] = [MP4FreeForm(value.encode("utf-8"))]
            written.append(name)
    audio.save()
    return written


def _write_vorbis(path: Path, tags: Tags, *, replace_genre: bool = False, log=lambda *_: None) -> list[str]:
    """Everything else the library holds — an ogg, an opus, a flac that never needed converting.

    The generic interface writes the ordinary fields and not the ids, a smaller cache than the other two
    formats keep, so a file in this shape is tagged rather than refused.
    """
    from mutagen import File as MutagenFile

    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise TagError(f"no tagger knows how to write {path.suffix} files ({path})")
    if audio.tags is None:
        audio.add_tags()

    keys = {"title": "title", "artist": "artist", "album": "album", "album_artist": "albumartist",
            "year": "date", "genre": "genre", "catalog_number": "catalognumber", "label": "label"}
    filled = tags.filled()
    written = []
    if replace_genre and "genre" not in filled and "genre" in audio.tags:
        del audio.tags["genre"]                 # nothing resolves now: take the old name off
        written.append("genre")
    for name, value in filled.items():
        if name not in keys:
            continue
        if name == "genre" and not replace_genre and "genre" in audio.tags:
            continue                            # an existing genre is never replaced
        audio.tags[keys[name]] = [value]
        written.append(name)
    audio.save()
    log(f"      wrote {sorted(written)} without the MusicBrainz ids ({path.suffix} has no place this "
        "writer knows for them)")
    return written
