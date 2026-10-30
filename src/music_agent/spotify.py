"""A public Spotify playlist, read as the tracklist a run starts from.

Spotify is worth having as *a tracklist source — already in scope*, and it was blocked only by
a developer app whose loopback redirect the dashboard would not take. Nothing here needs an app. The web
player reads a playlist from its own backend with a token it mints anonymously, and both halves of that are
reachable from a plain HTTP client: the token endpoint wants a TOTP the player's *bundle* carries, and the
playlist itself is a GraphQL *persisted query* — a hash, not a document.

So this module owns fetching, and nothing else. What it produces is the text `tracklist` reads, so a playlist
and a file reach a run the same way — `acquire --tracks <file|URL>` — and are read once, by `tracklist`.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .parsing import track_title
from .playlists import Playlist, PlaylistError, id_in

TOKEN_URL = "https://open.spotify.com/api/token"
GRAPHQL_URL = "https://api-partner.spotify.com/pathfinder/v2/query"

# The web player's own identity. These two endpoints answer a browser user-agent; the token endpoint is
# edge-filtered, and a client that does not look like the player is refused before its cookie is read.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

# `totpVer` 61, as the bundle at open.spotifycdn.com writes it: a 26-character secret, each character XORed
# with `position % 33 + 9`, the resulting decimals concatenated, and that string's UTF-8 bytes taken as the
# HMAC key over the 30-second counter. A token request with no code, or a wrong one, is `400 Unauthorized
# request`, which is the whole gate on this flow — and the one part of it that cannot be derived rather than
# read. Spotify moves it by adding a row to the table (`61`, `60` and `59` were all in the bundle at once)
# and shipping a new one; when this stops answering, that bundle is where the new row is.
_TOTP_SECRET = ',7/*F("rLJ2oxaKL^f+E1xvP@N'
_TOTP_VERSION = "61"
_TOTP_PERIOD = 30
_TOTP_DIGITS = 6

# `fetchPlaylist` as a persisted query. The operation name picks it out of the document; the variables carry
# the paging, so a playlist of any length arrives a hundred entries at a time.
_FETCH_PLAYLIST = "8964e8eafb21aa992a7d951d256d83285c04be2105d209262901de70cb97584a"
_PAGE = 100

_TIMEOUT = 20.0

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://open.spotify.com/",
    "Origin": "https://open.spotify.com",
    "Accept": "application/json",
}

_ID = re.compile(r"[A-Za-z0-9]{22}\Z")


def names_a_playlist(source: str) -> bool:
    """Whether an argument is a playlist rather than a path — a URL, or a `spotify:` URI.

    `--tracks` has to decide this from the argument alone, so a bare id deliberately does not count: it is a
    legal filename as well as a legal playlist, and reading a file is what `--tracks` did first.
    """
    try:
        playlist_id(source)
    except PlaylistError:
        return False
    return True


def playlist_id(source: str) -> str:
    """The id in a playlist URL or a `spotify:playlist:` URI."""
    identifier = id_in(source, host="spotify.com", kind="playlist", pattern=_ID)
    if identifier is None:
        raise PlaylistError(
            f"{source!r} is not a playlist — give its URL or its spotify:playlist: URI"
        )
    return identifier


def playlist(source: str, timeout: float = _TIMEOUT) -> Playlist:
    """Read one public playlist. Nothing has to be configured, and no account is involved."""
    identifier = playlist_id(source)
    token = _token(timeout)
    lines: list[str] = []
    name = ""
    offset = 0
    while True:
        body = _fetch_playlist(token, identifier, offset, timeout)
        name = body.get("name") or name
        content = body.get("content") or {}
        entries = content.get("items") or []
        lines.extend(line for line in (entry_line(entry) for entry in entries) if line)
        offset += _PAGE
        # A short page is the end of the playlist, and the count is the end sooner than that. Both are
        # checked: a playlist that reports no count still has to stop, and one that reports one has to be
        # followed to it rather than to the page it happens to fit in.
        if len(entries) < _PAGE or offset >= int(content.get("totalCount") or 0) > 0:
            return Playlist(name=name, lines=tuple(lines))


def entry_line(entry: dict) -> str:
    """One entry as `Artist - Title`, or '' when it is not a track — an episode, a local file, a gap.

    Artists are joined the way a credit is written, because that is the shape `parsing.credits` splits and
    the primary artist is the one a search leads with.
    """
    track = ((entry.get("itemV2") or {}).get("data")) or {}
    title = track_title(str(track.get("name") or ""))
    if not title:
        return ""
    artists = ((track.get("artists") or {}).get("items")) or []
    credit = ", ".join(
        name for name in (str((artist.get("profile") or {}).get("name") or "").strip() for artist in artists)
        if name
    )
    return f"{credit} - {title}" if credit else title


def _key() -> bytes:
    """The TOTP key: the secret's characters XORed with their own position, written out as decimals."""
    return "".join(
        str(ord(character) ^ (position % 33 + 9))
        for position, character in enumerate(_TOTP_SECRET)
    ).encode("utf-8")


def _totp(now: float | None = None) -> str:
    """The code for `now`, which is the clock in tests and `time.time()` on a run."""
    counter = int(time.time() if now is None else now) // _TOTP_PERIOD
    digest = hmac.new(_key(), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF) % 10 ** _TOTP_DIGITS
    return f"{code:0{_TOTP_DIGITS}d}"


def _token(timeout: float) -> str:
    """An anonymous web-player token — what the player itself asks for, and all this needs.

    The code is sent as the server's counter as well as its own: the player does that when it has no other
    clock to trust, and the endpoint accepts it.
    """
    code = _totp()
    query = urllib.parse.urlencode({
        "reason": "init", "productType": "web-player",
        "totp": code, "totpServer": code, "totpVer": _TOTP_VERSION,
    })
    payload = _json(urllib.request.Request(f"{TOKEN_URL}?{query}", headers=_HEADERS), timeout)
    token = payload.get("accessToken")
    if not token:
        raise PlaylistError(
            "Spotify minted no token for the web player — its TOTP version has probably moved"
        )
    return str(token)


def _fetch_playlist(token: str, identifier: str, offset: int, timeout: float) -> dict:
    """One page of the playlist, as the `playlistV2` body the query returns."""
    body = {
        "variables": {
            "uri": f"spotify:playlist:{identifier}",
            "offset": offset,
            "limit": _PAGE,
            "enableWatchFeedEntrypoint": False,
            "includeEpisodeContentRatingsV2": True,
        },
        "operationName": "fetchPlaylist",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _FETCH_PLAYLIST}},
    }
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json;charset=UTF-8",
                 "app-platform": "WebPlayer", "Authorization": f"Bearer {token}"},
    )
    payload = _json(request, timeout)
    body = payload.get("data", {}).get("playlistV2") if isinstance(payload, dict) else None
    if body is None:
        raise PlaylistError(f"Spotify answered without a playlist: {json.dumps(payload)[:200]}")
    return body


def _json(request: urllib.request.Request, timeout: float) -> dict:
    """One request, with every way it can fail turned into a sentence rather than a traceback."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise PlaylistError(
            f"Spotify answered {error.code} for {request.full_url.split('?')[0]}: {detail[:200]}"
        ) from error
    except urllib.error.URLError as error:
        raise PlaylistError(f"cannot reach Spotify: {error.reason}") from error
    try:
        return json.loads(raw)
    except ValueError as error:
        raise PlaylistError(f"Spotify answered something that is not JSON: {raw[:200]!r}") from error
