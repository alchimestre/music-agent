"""The rule the review queue is a view over: fixture data, plus the classification.

Kept apart from the renderers so the rule has exactly one home. The prototype therefore shows what
the rule actually does rather than a hand-written impression of it, and this module is the piece the
CLI ports. Verdicts are computed, never authored.

The renderers that sat beside it are gone — the screen shipped as `src/music_agent/review_tui.py`, and
that was retired on 2026-10-13 (the design notes) — so what is left here is the fixture, and its reader is
`tests/test_rules.py`: it checks the ported rule in
`rules.py` against this one, track by track, so a drift between the two shows up as a failure.

Terminology follows GLOSSARY.md: a candidate carries a *label* (what would happen to it), a track
carries a *decision* (what the human did).
"""

from __future__ import annotations

import os
import re

# The window a candidate's length may differ from the reference by. Anything wider is not a near-miss,
# it is a different file, and it is rejected outright rather than offered for review.
DURATION_WINDOW = 1
# A lossy file below this is not worth taking, whatever its length says.
QUALITY_FLOOR = 192
# Anything shorter than this is a preview, not a track.
MIN_SECONDS = 60


def _c(user, path, ext, dur, size_mb, bitrate=None, queue=0, slots=True):
    return {
        "user": user, "file": path, "ext": ext, "duration": dur, "size_mb": size_mb,
        "bitrate": bitrate, "queue": queue, "slots": slots,
        "lossless": ext.lower() in ("flac", "wav", "aiff", "aif", "alac"),
    }


FIXTURE = [
    {
        "request": {"artist": "Skee Mask", "title": "Slow Music", "version": None, "ref_duration": 302},
        "flag": "two lossless copies, both 5:02 — settled by the ladder, not by asking",
        "candidates": [
            _c("dusty", r"Music\Electronic\Skee Mask - Slow Music (Original Mix).flac", "flac", 302, 41.2),
            _c("bpm_dude", r"Music\FLAC\Skee Mask - Slow Music.flac", "flac", 302, 48.9),
            _c("junkpile", r"Music\misc\skee_mask_slow_music_128.mp3", "mp3", 303, 4.7, 128),
        ],
    },
    {
        "request": {"artist": "Kiasmos", "title": "Blurred", "version": "Extended Mix",
                    "ref_duration": 449},
        "flag": "the right mix, but lossy",
        "candidates": [
            _c("vinyl_only", r"Music\Kiasmos - Blurred (Extended Mix).mp3", "mp3", 448, 10.4, 320),
            _c("lofi", r"Music\Kiasmos - Blurred (Original Mix).flac", "flac", 311, 38.1),
            _c("bootleg", r"Music\edits\Kiasmos - Blurred (Victor Ruiz Remix).flac", "flac", 452, 44.0),
        ],
    },
    {
        "request": {"artist": "Altinbas", "title": "Trail Of", "version": None, "ref_duration": 371},
        "flag": "nothing here matches the reference length",
        "candidates": [
            _c("peershare", r"Music\Altinbas - Trail Of (Original Mix).aiff", "aiff", 374, 78.5),
            _c("silent", r"Music\Altinbas - Trail Of.wav", "wav", None, 71.0),
            _c("preview", r"Music\samples\Altinbas - Trail Of (preview).mp3", "mp3", 30, 0.7, 192),
            _c("othermix", r"Music\Altinbas - Trail Of (Kayla Painter Remix).flac", "flac", 371, 40.2),
        ],
    },
    {
        "request": {"artist": "Ntogn", "title": "Through the Mines of Khazad-dum", "version": None,
                    "ref_duration": 528},
        "flag": "the only copy found, and it reports no length",
        "candidates": [
            _c("mole", r"Music\Ntogn - Through the Mines of Khazad-dum.aiff", "aiff", None, 110.5),
        ],
    },
    {
        "request": {"artist": "Sven Wunder", "title": "Lotus", "version": None, "ref_duration": 260},
        "flag": "the only copy runs 4 s long",
        "candidates": [
            _c("crateguy", r"Music\Sven Wunder - Lotus (Original Mix).aiff", "aiff", 264, 61.0),
        ],
    },
]


# -- text helpers ----------------------------------------------------------

def version_of(name):
    """The trailing parenthesised mix, e.g. '(Original Mix)'."""
    groups = re.findall(r"\(([^()]*)\)", name.rsplit(".", 1)[0])
    return f"({groups[-1]})" if groups else ""


def mix_of(cand):
    return version_of(os.path.basename(cand["file"].replace("\\", "/"))).strip("()")


def stem_of(cand):
    name = os.path.basename(cand["file"].replace("\\", "/"))
    ver = version_of(name)
    stem = name.rsplit(".", 1)[0]
    return (stem[: len(stem) - len(ver)].strip() if ver else stem) or stem


def hms(sec):
    return "  ?  " if sec is None else "%d:%02d" % (sec // 60, sec % 60)


def delta(cand, req):
    return None if cand["duration"] is None else cand["duration"] - req["ref_duration"]


# -- the rule --------------------------------------------------------------

def version_matches(cand, req):
    """A bare request means the original; a named version must match its own name."""
    want = (req.get("version") or "").strip().lower()
    have = mix_of(cand).lower()
    if not want:
        return have == "" or have.startswith("original")
    return want in have


def classify(cand, req):
    """(label, reason). Label is 'Ready', 'Needs you' or 'Rejected'.

    Order matters: a preview would otherwise be judged as a wrong mix, and a wildly wrong length
    should not be reported as a mix problem.
    """
    if cand["duration"] is not None and cand["duration"] < MIN_SECONDS:
        return "Rejected", "%s — a preview clip" % hms(cand["duration"])
    d = delta(cand, req)
    if d is None:
        return "Needs you", "the peer reports no length"
    if abs(d) > DURATION_WINDOW:
        return "Rejected", "runs %+d s against the reference" % d
    if not cand["lossless"] and cand["bitrate"] is not None and cand["bitrate"] < QUALITY_FLOOR:
        return "Rejected", "%d kbps — below the quality floor" % cand["bitrate"]
    if not version_matches(cand, req):
        return "Needs you", "a different mix — %s" % (mix_of(cand) or "unnamed")
    return "Ready", "same mix, length matches"


def candidates(track):
    """[(candidate, label, reason)] for every candidate, rejected ones included."""
    return [(c,) + classify(c, track["request"]) for c in track["candidates"]]


def visible(track):
    """Only the candidates worth a human's attention. Rejections are never shown."""
    return [x for x in candidates(track) if x[1] != "Rejected"]


def ready(track):
    """[(candidate, label, reason)] for candidates that could be taken unattended."""
    return [x for x in candidates(track) if x[1] == "Ready"]


def rank(cand):
    """The ladder used when more than one candidate is Ready: lossless first, then the higher
    bitrate, then the shorter queue. A tie is settled by this, never by asking."""
    return (0 if cand["lossless"] else 1, -(cand["bitrate"] or 0), cand["queue"] or 0)


def pick(track):
    """The candidate the system takes on its own, or None if nothing is Ready."""
    r = ready(track)
    return sorted(r, key=lambda x: rank(x[0]))[0] if r else None


def taken(track):
    """Any Ready candidate means the system settles the track itself, picking between several by
    `rank` and logging which it took."""
    return bool(ready(track))


def in_review(track):
    """A track belongs in review only when the system could not settle it at all — that is, when no
    candidate is Ready. Every card on this screen therefore reads 'Needs you'; a Ready candidate is
    settled before it can appear here."""
    vis = visible(track)
    return bool(vis) and not taken(track)


def queue():
    return [t for t in FIXTURE if in_review(t)]


def auto_taken():
    return [t for t in FIXTURE if taken(t)]


def nothing_usable():
    return [t for t in FIXTURE if not visible(t)]


def summary():
    return "%d in review · %d settled automatically · %d with nothing usable" % (
        len(queue()), len(auto_taken()), len(nothing_usable()))
