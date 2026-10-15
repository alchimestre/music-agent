"""Turning names into the strings we compare, and back.

The fold lives here and nowhere else, so query building, matching and dedupe agree by construction — the
database stores NFC while `os.walk` returns NFD, so a byte comparison is not an option.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

# Artists joined into one credit. The `x` and `versus` forms need their spaces, so that an artist
# called Malcolm X is not split in two.
_CREDIT_SEP = re.compile(
    r"\s*(?:&|,|\bfeat\.|\bft\.|\bfeaturing\b|\bversus\b|\bvs\.?)\s*|\s+x\s+",
    re.IGNORECASE,
)

_PARENTHESISED = re.compile(r"\(([^()]*)\)")
_TRAILING_PARENS = re.compile(r"\s*\([^()]*\)\s*$")
_TRAILING_BRACKET = re.compile(r"\s*\[([^\]]*)\]\s*$")

# A bracket that is really a code: uppercase letters and digits, nothing else.
_CODE_LIKE = re.compile(r"[A-Z0-9][A-Z0-9 \-]*")

# The track number peers prefix a filename with: `12 Delphium`, `01 Xtal`, `A1 - Xtal`, `1-10. Title`.
_TRACK_PREFIX = re.compile(r"^\s*(?:[A-Za-z]{1,2}\s*)?\d{1,3}\s*(?:[-._)]\s*|\s+)")

# Markers peers write that are metadata rather than part of a name.
KNOWN_MARKERS = frozenset({"digi only", "vinyl only", "web"})


def fold(text: str) -> str:
    """The one comparison fold: Latin accents stripped, punctuation to spaces, case folded, whitespace
    collapsed. Only a mark following an ASCII letter is dropped, because that is what a Latin accent
    decomposes to; Japanese voicing and Cyrillic marks are parts of letters, and folding them would
    collide two different names rather than unify one.
    """
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFC", text))
    kept: list[str] = []
    for ch in decomposed:
        if unicodedata.combining(ch) and kept and kept[-1].isascii():
            continue
        kept.append(ch)

    spaced = "".join(
        ch if (ch.isalnum() or unicodedata.combining(ch)) else " " for ch in kept
    )
    return unicodedata.normalize("NFC", " ".join(spaced.casefold().split()))


# A parenthetical that credits somebody is not a version. `Galbi (feat Leïla Koumiya)` is one track, with or
# without the credit, and reading the credit as a mix made a request demand a version by that name — so the
# file that *is* the track came out a wrong version and was rejected (2026-10-13).
_CREDITS = frozenset({"feat", "ft", "featuring", "with"})


def extract_mix(name: str) -> str:
    """The trailing parenthesised mix — 'Original Mix', a remixer's name, or ''.

    A credit is not a mix: `(feat X)` and `(ft. X)` return ''.
    """
    groups = _PARENTHESISED.findall(name.rsplit(".", 1)[0])
    if not groups:
        return ""
    trailing = groups[-1].strip()
    return "" if fold(trailing).split(" ")[0].rstrip(".") in _CREDITS else trailing


def strip_code_tail(name: str) -> str:
    """Drop a trailing bracket only when it is a code, never when it is part of the name:
    `Ø [Phase] - Burden Of Proof` keeps its bracket, or the artist would become `Ø`.
    """
    match = _TRAILING_BRACKET.search(name)
    if not match:
        return name
    inner = match.group(1).strip()
    if inner.lower() not in KNOWN_MARKERS and not _CODE_LIKE.fullmatch(inner):
        return name
    return name[: match.start()].strip() or name


def credits(text: str) -> list[str]:
    """Every artist credited in a string, in the order written and without repeats."""
    found: list[str] = []
    seen: set[str] = set()
    for part in _CREDIT_SEP.split(text):
        part = part.strip()
        folded = fold(part)
        if part and folded not in seen:
            seen.add(folded)
            found.append(part)
    return found


def _credit_words(parts: list[str]) -> frozenset[str]:
    """Every word of every name in a credit, folded."""
    return frozenset(word for part in parts for word in fold(part).split() if word)


def same_credit(one: str, other: str) -> bool:
    """Whether two credits name the same people, one of them possibly naming fewer.

    Built on `credits`, so the separators are the ones the splitter already knows: `A x B`, `A & B`, `A, B`
    and `A feat. B` are one collaboration written four ways, and the files themselves disagree about which,
    which is how one track arrives twice.

    This is never asked on its own. It is asked beside a title and a length that already agree, and the
    length is what decides — two different recordings of one title do not land within a second of each
    other. Two shapes are allowed once that holds: the same people, or the lead artist alone against the
    full credit (`Rødhåd` against `Rødhåd & Vril`), and a pair's own name against its members written out
    (`Dettmann | Klock` against `Marcel Dettmann & Ben Klock`).

    A credit naming fewer people is only credible when the other names more than one, or the solo artist
    `X` would be the same person as `Malcolm X`. An empty credit matches only another empty one: a track
    that names nobody cannot be evidence that somebody else's track is held.
    """
    left, right = credits(one), credits(other)
    left_words, right_words = _credit_words(left), _credit_words(right)
    if not left_words or not right_words:
        return left_words == right_words
    if left_words == right_words:
        return True
    shorter, longer = ((left, right) if len(left_words) < len(right_words) else (right, left))
    return _credit_words(shorter) <= _credit_words(longer) and len(longer) > 1


def primary_artist(text: str) -> str:
    """The first artist credited — the one the credit line leads with."""
    found = credits(text)
    return found[0] if found else ""


def appears_in(wanted: str, text: str) -> bool:
    """Containment on word boundaries, not a substring: `of` must not match `often`."""
    wanted_words = fold(wanted).split()
    if not wanted_words:
        return False
    words = fold(text).split()
    span = len(wanted_words)
    return any(words[i:i + span] == wanted_words for i in range(len(words) - span + 1))


def artists_appear(wanted: str, path: str) -> bool:
    """Is every artist the request names present in the peer's path?

    The whole path is searched rather than the basename alone, because peers routinely leave the artist
    in the folder and name the file `12 Delphium.m4a`.
    """
    named = credits(wanted)
    return bool(named) and all(appears_in(artist, path) for artist in named)


def bare_title(text: str) -> str:
    """The title with its mix suffix and any trailing label code taken off.

    One definition, because the search query, the name matching in `rules` and the tracklist reader
    must agree about where a title ends.
    """
    return _TRAILING_PARENS.sub("", strip_code_tail(text)).strip()


# A bare title is the same version as `… (Original Mix)` / `… (Original Version)`, and every other suffix
# is its own version. Peers write one version a dozen ways — `original_mix`, `Original Mix`, `ORIGINAL MIX`
# — so the equivalence is on the folded text rather than on the spelling.
_ORIGINAL_MIXES = frozenset({"", "original", "original mix", "original version"})

# What makes a version somebody else's version rather than one of its own edits. Compared against folded
# text, so the entries are written folded — `re-edit` folds to `re edit`. A word list rather than a rule
# about who is named, because both spellings count: `(Made by Pete Remix)` and `(suray_remix)`.
_REMIX_MARKERS = ("remix", "rmx", "bootleg", "refix", "rework", "re edit", "reedit", "flip", "mashup")


def is_original(mix: str) -> bool:
    """Whether this names the original — `(Original Mix)`, `(Original Version)`, or nothing."""
    return fold(mix) in _ORIGINAL_MIXES


def is_remix(mix: str) -> bool:
    """Whether this names somebody else's version of the track."""
    text = fold(mix)
    return any(marker in text for marker in _REMIX_MARKERS)


def mix_matches(wanted: str, have: str) -> bool:
    """A bare request means the original, and a named version must be that version.

    `ENERGY` matches `ENERGY (Original Mix)` but not `ENERGY (Victor Ruiz Remix)`, and a remix satisfies
    only a request that names it. Text is compared folded, so `(Kate Stein Remix)` and
    `(kate_stein_remix)` are one remix.

    This is the *equivalence*, and it is what reference resolution filters with: a bare request has to
    resolve to the original's length, or the window it sets would reject the files that are the original.
    What a bare request is willing to *take* is wider, and lives in `rules.version_matches`.
    """
    if is_original(wanted):
        return is_original(have)
    return fold(wanted) == fold(have)


def search_query(artist: str, title: str) -> str:
    """`artist title`, and nothing else, folded.

    The mix suffix and any trailing label code come off, and no genre word is added — extra words
    return zero results, and peers routinely drop accents.
    """
    return fold(f"{artist} {bare_title(title)}")


@dataclass(frozen=True)
class ParsedName:
    """What a peer's path says about itself."""

    artist: str
    title: str
    mix: str
    album_hint: str = ""


def parse_filename(path: str) -> ParsedName:
    """Read a peer's path back into its parts.

    Peers organise a share as `Artist/Album/Track`, so the leading track number comes off the basename
    and the folder above it becomes an album hint — shown to a human, never used to decide a match. An
    unnamed artist is left empty rather than guessed: the caller searches the whole path, because that is
    where it usually is.
    """
    source = PurePosixPath(path.replace("\\", "/"))
    name = source.name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    mix = extract_mix(name)
    if mix:
        stem = _TRAILING_PARENS.sub("", stem).strip()

    artist, separator, title = stem.partition(" - ")
    if not separator:
        artist, title = "", stem
    title = strip_code_tail(title.strip())

    without_number = _TRACK_PREFIX.sub("", title).strip()
    if without_number:
        title = without_number
    return ParsedName(artist.strip(), title, mix, source.parent.name)
