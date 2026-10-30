"""A public YouTube or YouTube Music playlist, read as the tracklist a run starts from.

YouTube answers a playlist as a page assembled by its own scripts, and that shape is not a contract — it
changes without notice and without a version. `yt-dlp` is the interface that does not: `--flat-playlist`
lists a playlist's entries without fetching a single video, which is all a tracklist needs, and it reads
both youtube.com and music.youtube.com through the same list id.

What comes back is a video: a title that says what the video is, and a channel that says who published it.
The published channel is often not the artist — `Mort Garson - Topic` is Music's own channel for the artist,
`ArtistVEVO` is the label's — so the name the track has is read off the title when the title credits one, and
off the channel otherwise, with the video's label taken off by `parsing.track_title`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse

from .parsing import track_title
from .playlists import Playlist, PlaylistError

# The hosts a playlist link arrives on. `youtube-nocookie.com` is the embed domain, and a playlist link is
# sometimes handed over from it.
_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# A list id is a long base64-ish token. Checked so that a link carrying something else is refused as a link
# rather than shelled out to the tool.
_ID = re.compile(r"[A-Za-z0-9_-]{12,}\Z")

_TOOL = "yt-dlp"

# A flat listing is one request to YouTube for the playlist, so this is generous by orders of magnitude.
_TIMEOUT = 120.0

# What YouTube writes into a title to say the video is gone, rather than naming a track.
_GONE = ("[Private video", "[Deleted video", "[Unavailable video")

# A credit written into the title rather than read from the channel: `Yasmine Hamdan - I Remember I Forget`.
# The guard is the length — `Ricochet - Live On European Tour` would otherwise make a track of the title's
# own second half.
_CREDIT_WORDS = 4

_TOPIC_SUFFIX = " - Topic"
_VEVO = re.compile(r"VEVO\Z", re.IGNORECASE)

_QUOTES = "\"'“”‘’"


def names_a_playlist(source: str) -> bool:
    """Whether an argument is a YouTube playlist rather than a path.

    Read from the argument alone: `--tracks` has to settle this before anything runs, and a link with no list
    id in it is a single video, which is not a tracklist.
    """
    try:
        playlist_id(source)
    except PlaylistError:
        return False
    return True


def playlist_id(source: str) -> str:
    """The list id in a playlist, watch, playlist or youtu.be link."""
    parsed = urllib.parse.urlparse(source.strip())
    host = parsed.netloc.lower()
    if not any(host == name or host.endswith("." + name) for name in _HOSTS):
        raise PlaylistError(f"{source!r} is not a YouTube link")
    lists = urllib.parse.parse_qs(parsed.query).get("list") or []
    if lists and _ID.match(lists[0]):
        return lists[0]
    raise PlaylistError(
        f"{source!r} carries no playlist — a single video is not a tracklist, so give the link with its "
        "`list=`"
    )


def playlist(source: str, timeout: float = _TIMEOUT) -> Playlist:
    """Read one public playlist, without fetching a single video."""
    playlist_id(source)
    tool = shutil.which(_TOOL)
    if tool is None:
        raise PlaylistError(
            f"reading a YouTube playlist needs {_TOOL}, which is not on PATH — brew install {_TOOL}"
        )
    finished = _run(tool, source.strip(), timeout)
    try:
        listing = json.loads(finished.stdout)
    except ValueError as error:
        raise PlaylistError(
            f"{_TOOL} answered something that is not JSON: {finished.stdout[:200]!r}"
        ) from error
    if not isinstance(listing, dict) or not isinstance(listing.get("title"), str):
        raise PlaylistError(f"{_TOOL} answered without a playlist: {json.dumps(listing)[:200]}")
    entries = listing.get("entries")
    videos = [video for video in entries if isinstance(video, dict)] if isinstance(entries, list) else []
    lines = [line for line in (entry_line(video) for video in videos) if line]
    return Playlist(name=listing["title"], lines=tuple(lines))


def entry_line(video: dict) -> str:
    """One entry as `Artist - Title`, or '' when the entry names no track."""
    title = str(video.get("title") or "").strip()
    if not title or title.startswith(_GONE):
        return ""
    credit, name = _credited(title)
    name = _without_quotes(track_title(name))
    if not name:
        return ""
    artist = credit or channel_artist(str(video.get("channel") or video.get("uploader") or ""))
    return f"{artist} - {name}" if artist else name


def channel_artist(channel: str) -> str:
    """The artist a channel stands for: `Mort Garson - Topic` and `ArtistVEVO` both name one, and a channel
    that publishes something other than its own artist keeps the name it wrote."""
    name = channel.strip()
    if name.endswith(_TOPIC_SUFFIX):
        name = name[: -len(_TOPIC_SUFFIX)]
    return _VEVO.sub("", name).strip()


def _credited(title: str) -> tuple[str, str]:
    """The credit the title writes in front of its own name, and the name; or ('', title)."""
    head, separator, tail = title.partition(" - ")
    if separator and tail.strip() and 0 < len(head.split()) <= _CREDIT_WORDS:
        return head.strip(), tail.strip()
    return "", title


def _without_quotes(name: str) -> str:
    """A name a video wrapped in quotes: `"Chaghaybou"` is the track `Chaghaybou`."""
    return name.strip(_QUOTES).strip() or name


def _run(tool: str, source: str, timeout: float) -> subprocess.CompletedProcess:
    """The listing, or a sentence a person can act on.

    `--ignore-config` is not politeness: a config on this machine can change what the tool prints, and a
    listing is read as JSON here.
    """
    command = [tool, "--ignore-config", "--flat-playlist", "--dump-single-json", "--no-warnings", source]
    try:
        finished = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PlaylistError(f"{_TOOL} took longer than {timeout:.0f}s to list {source}") from error
    except OSError as error:
        raise PlaylistError(f"cannot run {tool}: {error.strerror or error}") from error
    if finished.returncode != 0:
        raise PlaylistError(f"{_TOOL} could not read {source}: {_last_line(finished.stderr)}")
    return finished


def _last_line(text: str) -> str:
    """The one line of a tool's complaint worth carrying: its last is its reason."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no reason given"
