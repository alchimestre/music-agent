from collections import Counter

from . import operations, receipts, spotify_account, spotify_source, state, store

LIMITATION = (
    "spogo removes every occurrence of a URI and offers no snapshot/CAS control; "
    "a same-URI edit between the fresh read and removal cannot be detected atomically."
)


def reconcile(config, job: state.Job, *, apply: bool = False, scope: str = "delivered") -> dict:
    if scope not in ("acquired", "delivered"):
        raise ValueError("scope must be 'acquired' or 'delivered'")
    with operations.lock(config.state_root):
        with store.writer_lock(config.state_root):
            source = spotify_source.snapshot(job)
            report = _report(source, scope)
            if source is None:
                report["blocker"] = "This job has no persisted Spotify source snapshot."
                return report
            entries, reason = _valid_entries(source)
            if reason:
                report["blocker"] = reason
                return report
            receipt = receipts.build(config, job)
            acquired, reason = _acquired_indices(receipt, entries) if scope == "acquired" else (set(), None)
            if reason:
                report["blocker"] = reason
                return report
            remote = spotify_account.read_playlist(source["uri"])
            if remote.id != source["id"]:
                report["blocker"] = "The current Spotify playlist identity differs from the saved source."
                return report
            current = [track.get("uri") for track in remote.tracks]
            if any(uri is not None and not isinstance(uri, str) for uri in current):
                report["blocker"] = "The current Spotify playlist contains an invalid track URI."
                return report
            report["playlist"] = {"id": remote.id, "name": remote.name, "uri": remote.uri}
            report["remaining"] = current
            events = job.events()
            intent = _unresolved_intent(events)
            if intent is not None:
                if intent.get("scope", "delivered") != scope:
                    report["blocker"] = "A previous Spotify removal used a different scope; no retry was made."
                    return report
                if _recover_prior(job, events, current, report):
                    return report
            if _prior_outcome_uncertain(events):
                report["blocker"] = "A previous Spotify removal changed the playlist unexpectedly; no retry was made."
                return report
            if _completed_uri_reappeared(events, current):
                report["blocker"] = "A removed URI appeared again; spogo cannot distinguish a later same-URI addition."
                return report
            removed = _completed_uris(events)
            entries = [entry for entry in entries if entry["uri"] not in removed]
            eligible, occurrences, reason = _eligible(entries, receipt, current, config, scope, acquired)
            report["eligible_uris"] = eligible
            report["eligible_occurrences"] = occurrences
            report["skipped_held"] = sum(entry["idx"] not in acquired for entry in entries) if scope == "acquired" else 0
            report["counts"].update(
                eligible_uris=len(eligible),
                eligible_occurrences=len(occurrences),
                skipped_held=report["skipped_held"],
            )
            if reason:
                report["blocker"] = reason
                return report
            if not apply or not eligible:
                return report
            return _apply(job, source, current, eligible, occurrences, report, scope)


def _report(source: dict | None, scope: str) -> dict:
    return {
        "scope": scope,
        "playlist": None if source is None else {
            "id": source.get("id"), "name": source.get("name"), "uri": source.get("uri")
        },
        "eligible_uris": [],
        "eligible_occurrences": [],
        "removed": 0,
        "skipped_held": 0,
        "counts": {"eligible_uris": 0, "eligible_occurrences": 0, "removed": 0, "skipped_held": 0},
        "remaining": [],
        "blocker": None,
        "reason": None,
        "limitation": LIMITATION,
    }



def _valid_entries(source: dict) -> tuple[list[dict], str | None]:
    if not all(isinstance(source.get(key), str) and source[key] for key in ("id", "name", "uri")):
        return [], "The persisted Spotify source identity is incomplete."
    if source["uri"] != f"spotify:playlist:{source['id']}":
        return [], "The persisted Spotify source identity does not match its playlist ID."
    tracks = source.get("tracks")
    entries = source.get("entries")
    if not isinstance(tracks, list) or not isinstance(entries, list):
        return [], "The persisted Spotify source membership is incomplete."
    if any(not isinstance(track, dict) for track in tracks):
        return [], "The persisted Spotify source membership is invalid."
    seen_indices = set()
    seen_positions = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return [], "The persisted Spotify source membership is invalid."
        idx, position, uri = entry.get("idx"), entry.get("position"), entry.get("uri")
        if (not isinstance(idx, int) or isinstance(idx, bool) or idx in seen_indices
                or not isinstance(position, int) or isinstance(position, bool)
                or position < 0 or position >= len(tracks) or position in seen_positions
                or not isinstance(uri, str) or tracks[position].get("uri") != uri):
            return [], "The persisted Spotify source membership is invalid."
        seen_indices.add(idx)
        seen_positions.add(position)
    return entries, None


def _acquired_indices(receipt: dict, entries: list[dict]) -> tuple[set[int], str | None]:
    acquired = receipt.get("acquired") if isinstance(receipt, dict) else None
    records = receipt.get("records") if isinstance(receipt, dict) else None
    if not isinstance(acquired, list) or not isinstance(records, list):
        return set(), "The acquisition receipt is unavailable."
    entry_indices = {entry["idx"] for entry in entries}
    if (any(not isinstance(idx, int) or isinstance(idx, bool) for idx in acquired)
            or len(acquired) != len(set(acquired)) or not set(acquired) <= entry_indices):
        return set(), "The acquisition receipt does not match persisted source membership."
    by_index = {}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("idx"), int)
                or isinstance(record.get("idx"), bool)):
            continue
        if record["idx"] in by_index:
            return set(), "The completion receipt contains duplicate record identities."
        by_index[record["idx"]] = record
    for entry in entries:
        record = by_index.get(entry["idx"])
        if (not record or record.get("uri") != entry["uri"]
                or record.get("position") != entry["position"]):
            return set(), "A persisted source occurrence has no matching completion receipt."
    return set(acquired), None


def _eligible(entries: list[dict], receipt: dict, current: list[str], config, scope: str,
              acquired: set[int]):
    source_counts = Counter(entry["uri"] for entry in entries)
    current_counts = Counter(uri for uri in current if isinstance(uri, str))
    if any(current_counts[uri] != count for uri, count in source_counts.items()):
        return [], [], "Spotify playlist occurrence counts changed since source admission."
    records = receipt.get("records") if isinstance(receipt, dict) else None
    if not isinstance(records, list):
        return [], [], "A completion receipt is unavailable."
    by_index = {}
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("idx"), int)
                or isinstance(record.get("idx"), bool)):
            continue
        if record["idx"] in by_index:
            return [], [], "The completion receipt contains duplicate record identities."
        by_index[record["idx"]] = record
    occurrences = []
    unverified = set()
    for entry in entries:
        record = by_index.get(entry["idx"])
        if (not record or record.get("uri") != entry["uri"]
                or record.get("position") != entry["position"]):
            return [], [], "A persisted source occurrence has no matching completion receipt."
        if not _delivered(record, config) or (scope == "acquired" and entry["idx"] not in acquired):
            unverified.add(entry["uri"])
        occurrences.append({"idx": entry["idx"], "position": entry["position"], "uri": entry["uri"]})
    eligible = sorted(source_counts.keys() - unverified)
    return eligible, [entry for entry in occurrences if entry["uri"] not in unverified], None



def _delivered(record: dict, config) -> bool:
    if record.get("delivered") is not True or not config.export_targets:
        return False
    deliveries = record.get("deliveries")
    if not isinstance(deliveries, dict):
        return False
    return all(isinstance(deliveries.get(target), dict)
               and deliveries[target].get("status") == "success"
               for target in config.export_targets)


def _apply(job, source, before, eligible, occurrences, report, scope):
    events = job.events()
    intent_id = f"{job.id}:{len(events) + 1}"
    count = len(occurrences)
    job.append_event("spotify_remove_intent", intent=intent_id, playlist_id=source["id"],
                     before=before, uris=eligible, occurrences=count, scope=scope)
    try:
        spotify_account.remove_tracks(source["uri"], eligible)
    except Exception as error:
        actual = spotify_account.read_playlist(source["uri"])
        remaining = [track.get("uri") for track in actual.tracks]
        if _removal_verified(before, remaining, eligible):
            job.append_event("spotify_remove_result", intent=intent_id, outcome="reconciled",
                             removed=count, error=type(error).__name__)
            report["removed"] = count
            report["counts"]["removed"] = count
        else:
            report["blocker"] = "Spotify removal result is unknown; no retry was made."
        report["remaining"] = remaining
        return report
    actual = spotify_account.read_playlist(source["uri"])
    remaining = [track.get("uri") for track in actual.tracks]
    verified = _removal_verified(before, remaining, eligible)
    job.append_event("spotify_remove_result", intent=intent_id, outcome="applied" if verified else "changed",
                     removed=count if verified else 0)
    report["removed"] = count if verified else 0
    report["counts"]["removed"] = report["removed"]
    report["remaining"] = remaining
    if not verified:
        report["blocker"] = "Spotify playlist changed during removal; the post-write state differs from expectation."
    return report


def _removal_verified(before: list, actual: list, uris: list) -> bool:
    if (not isinstance(before, list) or not isinstance(actual, list) or not isinstance(uris, list)
            or any(not isinstance(uri, str) for uri in uris)):
        return False
    removed = set(uris)
    if any(uri in removed for uri in actual):
        return False
    actual_tracks = iter(actual)
    return all(any(found == uri for found in actual_tracks)
               for uri in before if uri not in removed)


def _recover_prior(job, events: list[dict], current: list[str], report: dict) -> bool:
    intent = _unresolved_intent(events)
    if intent is None:
        return False
    if _removal_verified(intent.get("before"), current, intent.get("uris")):
        job.append_event("spotify_remove_result", intent=intent["intent"], outcome="reconciled",
                         removed=intent.get("occurrences", 0))
        report["removed"] = intent.get("occurrences", 0)
        report["counts"]["removed"] = report["removed"]
    else:
        report["blocker"] = "A previous Spotify removal has an unknown result; no retry was made."
    report["remaining"] = current
    return True


def _prior_outcome_uncertain(events: list[dict]) -> bool:
    actions = [event for event in events if event.get("event") in
               ("spotify_remove_intent", "spotify_remove_result")]
    return bool(actions and actions[-1].get("event") == "spotify_remove_result"
                and actions[-1].get("outcome") == "changed")


def _completed_uri_reappeared(events: list[dict], current: list[str]) -> bool:
    return bool(_completed_uris(events).intersection(current))


def _completed_uris(events: list[dict]) -> set[str]:
    intents = {event.get("intent"): event for event in events
               if event.get("event") == "spotify_remove_intent"}
    return {uri for event in events if event.get("event") == "spotify_remove_result"
            and event.get("outcome") in ("applied", "reconciled")
            for uri in intents.get(event.get("intent"), {}).get("uris", [])}


def _unresolved_intent(events: list[dict]) -> dict | None:
    results = {event.get("intent") for event in events if event.get("event") == "spotify_remove_result"}
    return next((event for event in reversed(events)
                 if event.get("event") == "spotify_remove_intent" and event.get("intent") not in results), None)
