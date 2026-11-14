from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from . import parsing, spotify
from .playlists import Playlist, PlaylistError

_PAGE_SIZE = 50
_MAX_PAGES = 1000
_TIMEOUT = 30
_TRACK_URI = re.compile(r"spotify:track:[A-Za-z0-9]{22}\Z")


class SpotifyAccountError(RuntimeError):
    """A spogo account operation failed with an actionable reason."""


@dataclass(frozen=True)
class AccountPlaylist:
    id: str
    name: str
    uri: str
    tracks: tuple[dict[str, Any], ...]

    @property
    def tracklist(self) -> Playlist:
        lines = []
        for track in self.tracks:
            artists = track.get("artists", [])
            if track.get("type") != "track" or not isinstance(track.get("name"), str):
                continue
            if not isinstance(artists, list) or not all(isinstance(artist, str) for artist in artists):
                continue
            title = parsing.track_title(track["name"])
            artist = ", ".join(artists)
            if artist and title:
                lines.append(f"{artist} - {title}")
        return Playlist(self.name, tuple(lines))


def _command(arguments: list[str]) -> Any:
    if shutil.which("spogo") is None:
        raise SpotifyAccountError("spogo is not installed or not on PATH. Install spogo and retry.")
    command = ["spogo", "--engine", "connect", "--auth", "cookies", "--json", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except FileNotFoundError as error:
        raise SpotifyAccountError("spogo is not installed or not on PATH. Install spogo and retry.") from error
    except subprocess.TimeoutExpired as error:
        raise SpotifyAccountError("spogo timed out; check connectivity and retry after confirming the result.") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        lowered = detail.lower()
        if any(word in lowered for word in ("cookie", "auth", "unauthorized", "401", "token")):
            raise SpotifyAccountError("Spotify authentication failed. Refresh the imported spogo cookie session.")
        raise SpotifyAccountError(f"spogo failed ({result.returncode}): {detail or 'no error details'}")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise SpotifyAccountError("spogo returned invalid JSON; check the playlist before repeating any write.") from error


def _page(payload: Any, label: str) -> tuple[list[dict], int]:
    if not isinstance(payload, dict):
        raise SpotifyAccountError(f"spogo returned an invalid {label} page (expected an items list).")
    total = payload.get("total")
    if not isinstance(total, int) or total < 0:
        raise SpotifyAccountError(f"spogo returned an invalid {label} total.")
    items = payload.get("items")
    if items is None and total == 0:
        items = []
    if not isinstance(items, list):
        raise SpotifyAccountError(f"spogo returned an invalid {label} page (expected an items list).")
    if any(not isinstance(item, dict) for item in items):
        raise SpotifyAccountError(f"spogo returned an invalid item in the {label} page.")
    return items, total


def _read_pages(arguments: list[str], label: str) -> list[dict]:
    entries: list[dict] = []
    expected_total: int | None = None
    for page_number in range(_MAX_PAGES):
        offset = len(entries)
        payload = _command([*arguments, "--limit", str(_PAGE_SIZE), "--offset", str(offset)])
        items, total = _page(payload, label)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise SpotifyAccountError(f"The Spotify {label} changed while reading; no mixed result was returned.")
        if len(items) > _PAGE_SIZE or len(entries) + len(items) > total:
            raise SpotifyAccountError(f"spogo returned an inconsistent {label} page.")
        entries.extend(items)
        if len(entries) == total:
            return entries
        if not items:
            raise SpotifyAccountError(f"spogo returned an incomplete {label} page.")
    raise SpotifyAccountError(f"Spotify {label} exceeds the safe pagination limit of {_MAX_PAGES * _PAGE_SIZE} entries.")


def list_playlists() -> list[dict]:
    return _read_pages(["library", "playlists", "list"], "playlist list")


def read_playlist(source: str) -> AccountPlaylist:
    playlist_id = spotify.playlist_id(source)
    metadata = _command(["playlist", "info", playlist_id])
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
        raise SpotifyAccountError("spogo returned invalid playlist metadata.")
    raw_tracks = _read_pages(["playlist", "tracks", playlist_id], "playlist tracks")
    total_tracks = metadata.get("total_tracks")
    if isinstance(total_tracks, int) and total_tracks != len(raw_tracks):
        raise SpotifyAccountError("The Spotify playlist changed while reading; no mixed result was returned.")
    tracks = tuple({**item, "position": position} for position, item in enumerate(raw_tracks))
    return AccountPlaylist(playlist_id, metadata["name"], metadata.get("uri", f"spotify:playlist:{playlist_id}"),
                          tracks)


def validate_mutation(playlist: str, uris: list[str]) -> str:
    try:
        playlist_id = spotify.playlist_id(playlist)
    except (ValueError, PlaylistError) as error:
        raise SpotifyAccountError(str(error)) from error
    if not uris:
        raise SpotifyAccountError("Provide at least one Spotify track URI.")
    if any(not isinstance(uri, str) or not _TRACK_URI.fullmatch(uri) for uri in uris):
        raise SpotifyAccountError("Each track must be a spotify:track: URI with a valid 22-character ID.")
    return playlist_id


def add_tracks(playlist: str, uris: list[str]) -> None:
    playlist_id = validate_mutation(playlist, uris)
    _command(["playlist", "add", playlist_id, *uris])


def remove_tracks(playlist: str, uris: list[str]) -> None:
    playlist_id = validate_mutation(playlist, uris)
    _command(["playlist", "remove", playlist_id, *dict.fromkeys(uris)])
