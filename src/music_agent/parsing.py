"""Turning names into the strings we compare, and back.

Ticket 14 measured the library this has to survive: 298 files with diacritics, 117 credits joined with
`&`, 60 with `feat.`, 41 with a trailing label code, and brackets that mean two different things.

The fold lives here and nothing else defines one, so query building, matching, dedupe and the backfill
orphan test agree by construction — the design notes measured why that matters, having found that the database
stores NFC while `os.walk` returns NFD, so a byte comparison matched 1342 of 1627 paths.
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
    collapsed. `Claude François` and `claude francois` fold alike.

    Only a mark that follows an ASCII letter is dropped, because that is what a Latin accent
    decomposes to. Japanese voicing and Cyrillic marks are parts of letters in their own right —
    folding `かたぎり` into `かたきり` would collide two different names rather than unify one.
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


def extract_mix(name: str) -> str:
    """The trailing parenthesised mix — 'Original Mix', a remixer's name, or ''."""
    groups = _PARENTHESISED.findall(name.rsplit(".", 1)[0])
    return groups[-1].strip() if groups else ""


def strip_code_tail(name: str) -> str:
    """Drop a trailing bracket only when it is a code, never when it is part of the name.

    `Lask - Black Ibis [PRA005]` loses its code. `Ø [Phase] - Burden Of Proof` keeps its bracket,
    because stripping every bracket would rename that artist to `Ø` (the design notes).
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


def primary_artist(text: str) -> str:
    """The first artist credited — the one the credit line leads with."""
    found = credits(text)
    return found[0] if found else ""


def appears_in(wanted: str, text: str) -> bool:
    """Does the wanted string fold into the text as a whole run of words?

    Containment on word boundaries, not a substring: `of` must not match `often`.
    """
    wanted_words = fold(wanted).split()
    if not wanted_words:
        return False
    words = fold(text).split()
    span = len(wanted_words)
    return any(words[i:i + span] == wanted_words for i in range(len(words) - span + 1))


def artists_appear(wanted: str, path: str) -> bool:
    """Is every artist the request names present in the peer's path?

    Ticket 14 splits credits, so a request naming two artists needs both to be there. The whole path
    is searched rather than the basename alone, because peers routinely leave the artist in the folder
    and name the file `12 Delphium.m4a`. A folder never *rejects* anything — it only supplies what the
    basename omits.
    """
    named = credits(wanted)
    return bool(named) and all(appears_in(artist, path) for artist in named)


def search_query(artist: str, title: str) -> str:
    """`artist title`, and nothing else.

    The mix suffix and any trailing label code come off first, and no genre word is ever added —
    extra words return zero results. The query goes out folded, because peers routinely drop accents
    (the design notes).
    """
    bare = _TRAILING_PARENS.sub("", strip_code_tail(title)).strip()
    return fold(f"{artist} {bare}")


@dataclass(frozen=True)
class ParsedName:
    """What a peer's path says about itself."""

    artist: str
    title: str
    mix: str
    album_hint: str = ""


def parse_filename(path: str) -> ParsedName:
    """Read a peer's path back into its parts.

    Peers overwhelmingly organise a share as `Artist/Album/Track`, so the leading track number comes
    off the basename and the folder above it is carried as an album hint — something to show a human
    and to judge a release against, never something that decides a match (the design notes).

    The basename supplies the artist when it names one. When it does not, the artist is left empty
    rather than guessed: the caller searches the whole path for it, because that is where it usually
    is.
    """
    name = PurePosixPath(path.replace("\\", "/")).name
    parent = PurePosixPath(path.replace("\\", "/")).parent.name
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
    return ParsedName(artist.strip(), title, mix, parent)
