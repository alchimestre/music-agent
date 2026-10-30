"""A public Deezer playlist, read as the tracklist a run starts from.

Deezer's public API answers a playlist with no account and no key, so there is nothing here to keep alive
and no token to mint: one request for the playlist's name, and the entries a hundred at a time.

A share link is the form a phone hands over — `link.deezer.com/s/34NKfa1yXrklFXuYlrljx` — and it carries no
id at all: the id is behind a redirect, which is followed once before the API is asked. `--tracks` has to
decide what it was given without touching the network, so the host is what tells a share link apart.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .parsing import track_title
from .playlists import Playlist, PlaylistError, id_in

API_URL = "https://api.deezer.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

# Where a shared playlist lands: the phone's share sheet, and the older page.link form it grew out of.
_SHARE_HOSTS = ("link.deezer.com", "deezer.page.link")

_ID = re.compile(r"\d+\Z")

# The API's own ceiling for one page of a playlist's tracks.
_PAGE = 100

# Longer than any playlist, so a walk that reaches it is the API failing to end rather than a playlist.
_CEILING = 20000

_TIMEOUT = 20.0

_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def names_a_playlist(source: str) -> bool:
    """Whether an argument is a Deezer playlist rather than a path — a URL, a share link, or a `deezer:` URI.

    Read from the argument alone: `--tracks` has to settle this before anything is fetched, and following a
    share link to find out would spend a request on every file it was handed.
    """
    return _is_share_link(source) or _names_an_id(source)


def playlist_id(source: str) -> str:
    """The numeric id in a playlist URL or a `deezer:playlist:` URI. A share link carries none — see `playlist`."""
    identifier = id_in(source, host="deezer.com", kind="playlist", pattern=_ID)
    if identifier is None:
        raise PlaylistError(
            f"{source!r} is not a Deezer playlist — give its URL, its share link or its deezer:playlist: URI"
        )
    return identifier


def playlist(source: str, timeout: float = _TIMEOUT) -> Playlist:
    """Read one public playlist, following a share link first when that is all the argument carries."""
    identifier = playlist_id(source) if not _is_share_link(source) else playlist_id(
        _share_target(source, timeout)
    )
    listing = _json(_request(f"{API_URL}/playlist/{identifier}"), timeout)
    if not isinstance(listing.get("title"), str):
        raise PlaylistError(f"Deezer answered without a playlist: {json.dumps(listing)[:200]}")
    lines = [line for line in (entry_line(track) for track in _tracks(identifier, timeout)) if line]
    return Playlist(name=listing["title"], lines=tuple(lines))


def entry_line(track: dict) -> str:
    """One entry as `Artist - Title`, or '' when the entry names no track.

    Deezer credits one artist per entry and puts the featured ones in the title, so the credit is copied
    and the featured names come off with the rest of the version, in `entry_title`.
    """
    title = entry_title(track)
    if not title:
        return ""
    artist = str(((track.get("artist") or {}).get("name")) or "").strip()
    return f"{artist} - {title}" if artist else title


def entry_title(track: dict) -> str:
    """The entry's name as a request reads it — the version kept only when it is its own edit."""
    return track_title(str(track.get("title") or track.get("title_short") or ""))


def _names_an_id(source: str) -> bool:
    try:
        playlist_id(source)
    except PlaylistError:
        return False
    return True


def _is_share_link(source: str) -> bool:
    host = urllib.parse.urlparse(source.strip()).netloc.lower()
    return any(host == name or host.endswith("." + name) for name in _SHARE_HOSTS)


def _share_target(source: str, timeout: float) -> str:
    """Where a share link leads, refused unless Deezer answered.

    A link is somebody else's to write: nothing stops one from redirecting off the service, and a reader
    that followed it would fetch whatever host the link named on this machine's behalf.
    """
    target = _followed(source, timeout)
    host = urllib.parse.urlparse(target).netloc.lower()
    if host != "deezer.com" and not host.endswith(".deezer.com"):
        raise PlaylistError(
            f"the share link {source} leads to {host or 'no host'}, not to Deezer"
        )
    return target


def _followed(source: str, timeout: float) -> str:
    """Where a share link leads — the one place its id is written."""
    try:
        with urllib.request.urlopen(_request(source.strip()), timeout=timeout) as response:
            return response.geturl()
    except urllib.error.HTTPError as error:
        raise PlaylistError(
            f"Deezer answered {error.code} for {source}: a link that expired or was revoked leads nowhere"
        ) from error
    except urllib.error.URLError as error:
        raise PlaylistError(f"cannot reach Deezer: {error.reason}") from error


def _tracks(identifier: str, timeout: float):
    """Every entry, in playlist order, a page at a time."""
    offset = 0
    while True:
        page = _json(
            _request(f"{API_URL}/playlist/{identifier}/tracks?index={offset}&limit={_PAGE}"), timeout
        )
        entries = page.get("data")
        if not isinstance(entries, list) or not entries:
            return
        yield from entries
        offset += len(entries)
        if offset >= _CEILING:
            raise PlaylistError(
                f"Deezer kept answering past {_CEILING} entries for one playlist — its paging is not "
                "ending, so there is no end to read to"
            )
        # A short page is the end, and the count is the end sooner than that. Both are checked: a page that
        # reports no count still has to stop, and one that reports a count has to be followed to it rather
        # than to the page it happens to fit in.
        if len(entries) < _PAGE or offset >= int(page.get("total") or 0) > 0:
            return


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers=_HEADERS)


def _json(request: urllib.request.Request, timeout: float) -> dict:
    """One request, with every way it can fail turned into a sentence rather than a traceback."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise PlaylistError(
            f"Deezer answered {error.code} for {request.full_url.split('?')[0]}: {detail[:200]}"
        ) from error
    except urllib.error.URLError as error:
        raise PlaylistError(f"cannot reach Deezer: {error.reason}") from error
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise PlaylistError(f"Deezer answered something that is not JSON: {raw[:200]!r}") from error
    if not isinstance(payload, dict):
        raise PlaylistError(f"Deezer answered something that is not a playlist: {raw[:200]!r}")
    # A refusal arrives as a 200 with an `error` object — a deleted playlist, an id that never existed.
    if isinstance(payload.get("error"), dict):
        message = str(payload["error"].get("message") or "no reason given")
        raise PlaylistError(f"Deezer refused the playlist: {message}")
    return payload
