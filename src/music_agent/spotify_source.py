"""Deterministic admission of private Spotify playlists into canonical jobs."""

from __future__ import annotations

from typing import Any

from . import spotify, spotify_account, state, store, tracklist
from .config import Config
from .playlists import PlaylistError


def resolve(source: str) -> spotify_account.AccountPlaylist:
    """Resolve a playlist URL/URI/ID or a unique exact case-insensitive account name."""
    identifier_source = source if source.startswith(("spotify:", "http://", "https://")) else f"spotify:playlist:{source}"
    try:
        playlist_id = spotify.playlist_id(identifier_source)
    except PlaylistError as error:
        if source.strip().startswith(("spotify:", "http://", "https://")):
            raise ValueError(str(error)) from error
        matches = [row for row in spotify_account.list_playlists()
                   if isinstance(row.get("name"), str) and row["name"].casefold() == source.casefold()]
        if not matches:
            raise ValueError(f"No account playlist exactly matches {source!r}.") from error
        if len(matches) != 1:
            raise ValueError(f"Account playlist name {source!r} is ambiguous.") from error
        playlist_id = matches[0].get("id")
        if not isinstance(playlist_id, str):
            raise ValueError(f"Account playlist {source!r} has no valid ID.") from error
        try:
            spotify.playlist_id(f"spotify:playlist:{playlist_id}")
        except PlaylistError as invalid:
            raise ValueError(f"Account playlist {source!r} has an invalid ID.") from invalid
    return spotify_account.read_playlist(f"spotify:playlist:{playlist_id}")


def _source_snapshot(playlist: spotify_account.AccountPlaylist) -> tuple[dict[str, Any], list[state.Record]]:
    entries = []
    records = []
    for position, raw in enumerate(playlist.tracks):
        record = _record_for(raw, len(records) + 1)
        if record is None:
            continue
        try:
            spotify_account.validate_mutation(playlist.uri, [raw["uri"]])
        except spotify_account.SpotifyAccountError:
            continue
        records.append(record)
        entries.append({"idx": record.idx, "position": position, "uri": raw["uri"]})
    return {
        "id": playlist.id,
        "name": playlist.name,
        "uri": playlist.uri,
        "tracks": [dict(track) for track in playlist.tracks],
        "entries": entries,
    }, records


def _record_for(raw: dict[str, Any], idx: int) -> state.Record | None:
    if raw.get("type") != "track" or not isinstance(raw.get("name"), str):
        return None
    artists = raw.get("artists")
    if not isinstance(artists, list) or not artists or not all(isinstance(artist, str) for artist in artists):
        return None
    artist = ", ".join(artists)
    if not artist.strip() or not raw["name"].strip() or not isinstance(raw.get("uri"), str):
        return None
    request = tracklist.request_for(artist, raw["name"])
    if not request.title:
        return None
    return state.Record(idx=idx, artist=request.artist, title=request.title, mix=request.mix)


def _latest_identical(config: Config, payload: dict[str, Any]) -> state.Job | None:
    for job in reversed(state.all_jobs(config)):
        saved = snapshot(job)
        if saved and saved["id"] == payload["id"] and saved == payload:
            return job
    return None


def admit(config: Config, source: str, name: str | None = None) -> state.Job:
    """Snapshot and admit all supported source tracks under the canonical writer lock."""
    playlist = resolve(source)
    payload, records = _source_snapshot(playlist)
    if not records:
        raise ValueError("Spotify playlist has no supported tracks to admit.")
    with store.writer_lock(config.state_root):
        existing = _latest_identical(config, payload)
        if existing is not None:
            return existing
        job = state.create(config, name or playlist.name, records)
        job.append_event("spotify_source", snapshot=payload)
        return job


def snapshot(job: state.Job) -> dict[str, Any] | None:
    """Return the persisted raw source snapshot, if this job has one."""
    events = [event for event in job.events() if event.get("event") == "spotify_source"]
    if not events:
        return None
    payload = events[-1].get("snapshot")
    return payload if isinstance(payload, dict) else None
