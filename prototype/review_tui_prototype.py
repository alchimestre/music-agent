#!/usr/bin/env python3
"""PROTOTYPE — throwaway, fixture data only, writes nothing.

FROZEN ARCHIVE. This file is the record of the four layouts that were compared, and it is no longer
kept in step with the rule: its own fixture predates the one-second window and still shows rejected
candidates. The live prototype is `review_tui_textual.py`, which renders the chosen layout over
`review_model.py`.

Question: what should the `music-agent review` screen look like and behave like?

Three structurally different variants of the same screen, cycled with  <left> / <right>
(or [ and ]). Everything is in-memory; there is no persistence, no database, no network.

    python prototype/review_tui_prototype.py          # interactive
    python prototype/review_tui_prototype.py --demo    # print every variant once, no input

Field names mirror the real Soulseek search payload
(filename, username, filesize, extension, bitrate, duration_sec, has_free_slots,
queue_size) so the fixture can later be swapped for a real search dump.
"""

from __future__ import annotations

import os
import re
import sys

# --------------------------------------------------------------------------
# Fixture: four requested tracks, deliberately covering the interesting cases
# from tickets 04 (match/confidence) and 05 (versions).
# --------------------------------------------------------------------------

def _c(user, path, ext, dur, size_mb, bitrate=None, queue=0, slots=True, verdict="", reason=""):
    return {
        "user": user, "file": path, "ext": ext, "duration": dur, "size_mb": size_mb,
        "bitrate": bitrate, "queue": queue, "slots": slots,
        "lossless": ext.lower() in ("flac", "wav", "aiff", "aif", "alac"),
        "verdict": verdict, "reason": reason,
    }


FIXTURE = [
    {
        "request": {"artist": "Skee Mask", "title": "Slow Music", "version": None, "ref_duration": 302},
        "flag": "two lossless copies, both 5:02 — the system will not pick between them",
        "candidates": [
            _c("dusty", r"Music\Electronic\Skee Mask - Slow Music (Original Mix).flac", "flac", 302, 41.2,
               verdict="Ready", reason="original mix, same length as the reference"),
            _c("bpm_dude", r"Music\FLAC\Skee Mask - Slow Music.flac", "flac", 302, 48.9,
               verdict="Ready", reason="no mix name in the file — the original"),
            _c("junkpile", r"Music\misc\skee_mask_slow_music_128.mp3", "mp3", 303, 4.7, 128,
               verdict="Not a match", reason="128 kbps — below the quality floor"),
        ],
    },
    {
        "request": {"artist": "Kiasmos", "title": "Blurred", "version": "Extended Mix", "ref_duration": 449},
        "flag": "the extended mix only exists lossy; the lossless copy is the original",
        "candidates": [
            _c("vinyl_only", r"Music\Kiasmos - Blurred (Extended Mix).mp3", "mp3", 448, 10.4, 320,
               verdict="Needs you", reason="the right mix, but 320 kbps"),
            _c("lofi", r"Music\Kiasmos - Blurred (Original Mix).flac", "flac", 311, 38.1,
               verdict="Needs you", reason="lossless, but the 5:11 original"),
            _c("bootleg", r"Music\edits\Kiasmos - Blurred (Victor Ruiz Remix).flac", "flac", 452, 44.0,
               verdict="Not a match", reason="Victor Ruiz remix — a different track"),
        ],
    },
    {
        "request": {"artist": "Altinbas", "title": "Trail Of", "version": None, "ref_duration": 371},
        "flag": "nothing here can be taken without a look",
        "candidates": [
            _c("peershare", r"Music\Altinbas - Trail Of (Original Mix).aiff", "aiff", 374, 78.5,
               verdict="Needs you", reason="runs 3 s longer — a different pressing?"),
            _c("silent", r"Music\Altinbas - Trail Of.wav", "wav", None, 71.0,
               verdict="Needs you", reason="the peer reports no length"),
            _c("preview", r"Music\samples\Altinbas - Trail Of (preview).mp3", "mp3", 30, 0.7, 192,
               verdict="Not a match", reason="0:30 — a preview clip"),
        ],
    },
    {
        "request": {"artist": "Ntogn", "title": "Through the Mines of Khazad-dum", "version": None,
                    "ref_duration": 528},
        "flag": "the only copy found, and it reports no length",
        "candidates": [
            _c("mole", r"Music\Ntogn - Through the Mines of Khazad-dum.aiff", "aiff", None, 110.5,
               verdict="Needs you", reason="the peer reports no length"),
        ],
    },
]

BLANK_LINE = ""

# Colour only when stdout is a terminal, so --demo | less (or a pipe) stays readable and aligned.
COLOR = sys.stdout.isatty()

# Palette borrowed from the JobRadar TUI: colour carries meaning, never decoration.
#   bold cyan  = a confident match     bold yellow = needs a human eye
#   dim red    = out                   bold green  = accepted
#   dim        = secondary             reverse     = header / selection
STYLES = {
    "auto": "1;36", "review": "1;33", "reject": "2;31", "accept": "1;32",
    "skip": "2", "head": "1;7", "dim": "2", "bold": "1",
}
# A candidate's label says what would happen to it, so it can never be misread as a state.
VERDICT_STYLE = {"Ready": "auto", "Needs you": "review", "Not a match": "reject"}
# A track's state is a decision the human made. The two axes never share a word or a glyph.
DECISION_STYLE = {"accepted": "accept", "skipped": "skip", "not found": "reject"}
DECISION_MARK = {"accepted": "✓", "skipped": "–", "not found": "✗"}


def _mark(dec):
    return DECISION_MARK.get(dec, "·")


def _s(text, name):
    if not COLOR or not name or name not in STYLES:
        return text
    return f"\033[{STYLES[name]}m{text}\033[0m"


def _verdict(v):
    return _s(v, VERDICT_STYLE.get(v, ""))


def _dstyle(dur, ref):
    """The Δ column is the deciding number, so it carries the colour."""
    if dur is None:
        return "dim"
    d = abs(dur - ref)
    return "accept" if d == 0 else ("review" if d <= 5 else "reject")


def _state_line(state):
    done = state["decisions"]
    counts = {}
    for v in done.values():
        counts[v] = counts.get(v, 0) + 1
    parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing decided yet"
    return f"STATE  track {state['track'] + 1}/{len(FIXTURE)}  candidate {state['cand'] + 1}  |  {parts}"


# --------------------------------------------------------------------------
# Terminal helpers
# --------------------------------------------------------------------------

def _width(default=100):
    try:
        return max(60, os.get_terminal_size().columns)
    except OSError:
        return default


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _bar(n, total, width=16):
    filled = int(round(width * n / max(1, total)))
    return "█" * filled + "·" * (width - filled)


def _dur(d):
    return "  ?  " if d is None else f"{d:>3}s"


def _delta(dur, ref):
    if dur is None:
        return "  ?  "
    return f"{dur - ref:+3d}s"


def _inv(s):
    """Selected row. Inverse video on a tty; a marker when piped, same width."""
    if COLOR:
        return "\033[7m" + s + "\033[0m"
    return ("▶" + s[1:]) if s else s


def _dim(s):
    return "\033[2m" + s + "\033[0m" if COLOR else s


def _bold(s):
    return "\033[1m" + s + "\033[0m" if COLOR else s


def _version_of(name):
    """The trailing parenthesised mix, e.g. '(Original Mix)' — the token that decides which version."""
    groups = re.findall(r"\(([^()]*)\)", name.rsplit(".", 1)[0])
    return f"({groups[-1]})" if groups else ""


def _clear():
    sys.stdout.write("\033[2J\033[H")


# --------------------------------------------------------------------------
# Variant A — queue on the left, candidates on the right (split view)
# --------------------------------------------------------------------------

def render_a(state):
    W = _width()
    left = 30
    right = W - left - 4
    out = [_bold("music-agent review   [variant A — queue / detail split]")]
    out.append(f"PROTOTYPE · fixture data · {len(FIXTURE)} tracks awaiting review")
    out.append("─" * min(W, 110))
    out.append(_dim("QUEUE".ljust(left - 2) + "│ DETAIL OF THE SELECTED TRACK"))
    out.append("─" * min(W, 110))

    rows = max(len(FIXTURE), len(FIXTURE[state["track"]]["candidates"]) + 3)
    for i in range(rows):
        lcol = ""
        if i < len(FIXTURE):
            t = FIXTURE[i]
            mark = _mark(state["decisions"].get(i, ""))
            label = f"{mark} {t['request']['artist']} - {t['request']['title']}"
            lcol = _clip(label, left - 2)
            if i == state["track"]:
                lcol = _inv(lcol.ljust(left - 2))
            else:
                lcol = lcol.ljust(left - 2)

        rcol = ""
        t = FIXTURE[state["track"]]
        if i == 0:
            rcol = _bold(f"{t['request']['artist']} - {t['request']['title']}"
                         + (f" ({t['request']['version']})" if t["request"]["version"] else ""))
            rcol += f"   ref {t['request']['ref_duration']}s"
        elif i == 1:
            rcol = _dim("flagged: " + t["flag"])
        elif i >= 3 and i - 3 < len(t["candidates"]):
            c = t["candidates"][i - 3]
            sel = (i - 3) == state["cand"]
            line = (f"{'▸' if sel else ' '} [{c['ext']:>4}] {_dur(c['duration'])} "
                    f"{_delta(c['duration'], t['request']['ref_duration']):>4}  "
                    f"{(str(c['bitrate']) + 'k') if c['bitrate'] else 'lossless':>8}  "
                    f"{_clip(os.path.basename(c['file'].replace(chr(92), '/')), max(10, right - 46))}")
            rcol = (_inv(line) if sel else line)

        out.append(lcol + " │ " + rcol)

    out.append("─" * min(W, 110))
    c = FIXTURE[state["track"]]["candidates"][state["cand"]] if FIXTURE[state["track"]]["candidates"] else None
    if c:
        out.append(f"{_bold(c['verdict']):<20} {c['reason']}")
    out.append(_state_line(state))
    out.append(_dim("[a]ccept  [s]kip  [n]ext candidate  [f] not found   <left>/<right> switch variant   [q]uit"))
    return "\n".join(out)


# --------------------------------------------------------------------------
# Variant B — one track per screen, candidates as stacked cards
# --------------------------------------------------------------------------

def render_b(state):
    W = min(_width(), 96)
    t = FIXTURE[state["track"]]
    out = [_bold("music-agent review   [variant B — one track, stacked candidates]"), ""]
    out.append("  " + "─" * (W - 4))
    head = f"  REQUEST   {t['request']['artist']} - {t['request']['title']}"
    if t["request"]["version"]:
        head += f"  ({t['request']['version']})"
    out.append(_bold(head))
    out.append(f"  ref {t['request']['ref_duration']}s   ·   {t['flag']}")
    out.append("  " + "─" * (W - 4))
    out.append("")

    for i, c in enumerate(t["candidates"]):
        sel = i == state["cand"]
        border = "╔" if sel else "┌"
        out.append(f"  {border}─ {_bold(c['verdict'])} " + "─" * max(4, W - 20 - len(c["verdict"])))
        name = os.path.basename(c["file"].replace("\\", "/"))
        out.append(f"  │ {_clip(name, W - 8)}")
        bits = [f"{c['ext']}", _dur(c["duration"]), f"Δ{_delta(c['duration'], t['request']['ref_duration'])}",
                f"{c['bitrate']}k" if c["bitrate"] else "lossless", f"{c['size_mb']} MB"]
        out.append("  │ " + " · ".join(bits))
        out.append(f"  │ peer {c['user']}   queue {c['queue']}   free slots {'yes' if c['slots'] else 'no'}")
        if sel:
            out.append(f"  │ {_dim(c['reason'])}")
        out.append("  " + ("╚" if sel else "└") + "─" * (W - 6))
        out.append("")
    out.append(_state_line(state))
    out.append(_dim("[a]ccept   [n]ext candidate   [s]kip this one   [f] not found   <left>/<right> variant"))
    return "\n".join(out)


# --------------------------------------------------------------------------
# Variant C — the whole queue as one table, bulk decisions
# --------------------------------------------------------------------------

def render_c(state):
    W = _width()
    out = [_bold("music-agent review   [variant C — whole queue, bulk decisions]")]
    auto = sum(1 for t in FIXTURE if any(c["verdict"] == "Ready" for c in t["candidates"]))
    rej = sum(1 for t in FIXTURE for c in t["candidates"] if c["verdict"] == "Not a match")
    out.append(f"PROTOTYPE · {len(FIXTURE)} tracks · {auto} have a Ready candidate · "
               f"{rej} candidates rejected by rule")
    out.append("─" * min(W, 112))
    out.append(f"{'':2} {'TRACK':<34} {'BEST CANDIDATE':<26} {'Δ':>4} {'VERDICT':<8} REASON")
    out.append("─" * min(W, 112))
    for i, t in enumerate(FIXTURE):
        cands = t["candidates"]
        best = cands[state["cand"] % len(cands)] if i == state["track"] and cands else (cands[0] if cands else None)
        mark = _mark(state["decisions"].get(i, ""))
        trk = _clip(f"{t['request']['artist']} - {t['request']['title']}", 33)
        if best:
            cname = _clip(os.path.basename(best["file"].replace("\\", "/")), 25)
            row = (f"{mark:2} {trk:<34} {cname:<26} {_delta(best['duration'], t['request']['ref_duration']):>4} "
                   f"{best['verdict']:<8} {_clip(best['reason'], max(10, W - 96))}")
        else:
            row = f"{mark:2} {trk:<34} {'—':<26} {'':>4} {'—':<8}"
        out.append(_inv(row) if i == state["track"] else row)
    out.append("─" * min(W, 112))
    t = FIXTURE[state["track"]]
    out.append(f"SELECTED  {_bold(t['request']['artist'] + ' - ' + t['request']['title'])}  ·  {t['flag']}")
    out.append(f"          {len(t['candidates'])} candidates · "
               f"{sum(1 for c in t['candidates'] if c['verdict'] == 'Ready')} Ready")
    out.append(_state_line(state))
    out.append(_dim("[a]ccept    [A]ccept every Ready in the queue    [s]kip  "
                    "[↑][↓] move track  [n]ext candidate  <left>/<right> variant"))
    return "\n".join(out)


def _cards(cands, ref, sel_idx, width):
    """B's stacked candidate cards, fitted to `width` columns (used inside A's detail pane)."""
    lines = []
    for i, c in enumerate(cands):
        top = "╔" if i == sel_idx else "┌"
        bot = "╚" if i == sel_idx else "└"
        name = os.path.basename(c["file"].replace("\\", "/"))
        ver = _version_of(name)
        stem = name.rsplit(".", 1)[0]
        title = (stem[: len(stem) - len(ver)].strip() if ver else stem) or stem
        lines.append(f" {top}─ {_verdict(c['verdict'])} " + "─" * max(2, width - len(c["verdict"]) - 6))
        lines.append(f" │ {_clip(title, width - 4)}")
        if ver:
            lines.append(f" │ {_dim('version')} {_bold(ver)}")
        bits = [c["ext"], _dur(c["duration"]).strip(),
                _s(f"Δ{_delta(c['duration'], ref)}", _dstyle(c["duration"], ref)),
                f"{c['bitrate']}k" if c["bitrate"] else "lossless", f"{c['size_mb']} MB"]
        lines.append(" │ " + " · ".join(bits))
        lines.append(f" │ peer {c['user']} · queue {c['queue']} · slots {'yes' if c['slots'] else 'no'}")
        if i == sel_idx:
            lines.append(f" │ {_dim(_clip(c['reason'], width - 6))}")
        lines.append(f" {bot}" + "─" * max(2, width - 3))
        lines.append("")
    return lines


def render_d(state):
    """The chosen design: A's queue/detail split, with B's cards inside the detail pane."""
    W = _width()
    left = 30
    right = max(30, W - left - 4)
    t = FIXTURE[state["track"]]
    out = [_bold("music-agent review   [variant D — CHOSEN: A's split, B's cards]")]
    out.append(f"PROTOTYPE · fixture data · {len(FIXTURE)} tracks awaiting review")
    out.append("─" * min(W, 110))
    out.append(_dim("QUEUE".ljust(left - 2) + "│ DETAIL OF THE SELECTED TRACK"))
    out.append("─" * min(W, 110))

    left_lines = []
    for i, fx in enumerate(FIXTURE):
        mark = _mark(state["decisions"].get(i, ""))
        label = _clip(f"{mark} {fx['request']['artist']} - {fx['request']['title']}", left - 2).ljust(left - 2)
        left_lines.append(_inv(label) if i == state["track"] else label)

    head = f"{t['request']['artist']} - {t['request']['title']}"
    if t["request"]["version"]:
        head += f"  ({t['request']['version']})"
    right_lines = [_bold(head) + f"   ref {t['request']['ref_duration']}s",
                   _dim("flagged: " + t["flag"]), ""]
    right_lines += _cards(t["candidates"], t["request"]["ref_duration"], state["cand"], right)

    for i in range(max(len(left_lines), len(right_lines))):
        l = left_lines[i] if i < len(left_lines) else " " * (left - 2)
        r = right_lines[i] if i < len(right_lines) else ""
        out.append(l + " │ " + r)

    out.append("─" * min(W, 110))
    c = t["candidates"][state["cand"]] if t["candidates"] else None
    if c:
        out.append(f"{_bold(c['verdict'])}   {c['reason']}")
    out.append(_state_line(state))
    out.append(_dim("[a]ccept  [s]kip  [n]ext candidate  [f] not found   <left>/<right> switch variant   [q]uit"))
    return "\n".join(out)


VARIANTS = [("D", "chosen — A's split with B's cards", render_d),
            ("A", "queue / detail split", render_a),
            ("B", "one track, stacked candidates", render_b),
            ("C", "whole queue, bulk decisions", render_c)]


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

def _read_key():
    """Return 'left', 'right', 'up', 'down', or a single character. Requires a tty."""
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1).decode(errors="replace")
        if ch == "\x1b":
            seq = ""
            while select.select([fd], [], [], 0.02)[0] and len(seq) < 2:
                seq += os.read(fd, 1).decode(errors="replace")
            return {"[C": "right", "[D": "left", "[A": "up", "[B": "down"}.get(seq, "esc")
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv):
    state = {"track": 0, "cand": 0, "variant": 0, "decisions": {}}

    if "--demo" in argv:
        for key, name, render in VARIANTS:
            print(f"\n===== VARIANT {key} — {name} =====")
            print(render(state))
        return 0

    if not sys.stdin.isatty():
        print("not a tty — run with --demo to print all variants", file=sys.stderr)
        return 2

    while True:
        _clear()
        key, name, render = VARIANTS[state["variant"]]
        print(render(state))
        print()
        print(_dim(f"variant {key} ({name}) · ← / → to switch · prototype, nothing is saved"))

        k = _read_key()
        t = FIXTURE[state["track"]]
        cands = t["candidates"]

        if k in ("q", "\x03", "esc"):
            break
        elif k == "right":
            state["variant"] = (state["variant"] + 1) % len(VARIANTS)
        elif k == "left":
            state["variant"] = (state["variant"] - 1) % len(VARIANTS)
        elif k == "n":
            if cands:
                state["cand"] = (state["cand"] + 1) % len(cands)
        elif k == "a":
            state["decisions"][state["track"]] = "accepted"
            state["track"] = (state["track"] + 1) % len(FIXTURE)
            state["cand"] = 0
        elif k == "A":
            for i, fx in enumerate(FIXTURE):
                if any(c["verdict"] == "Ready" for c in fx["candidates"]) and i not in state["decisions"]:
                    state["decisions"][i] = "accepted"
        elif k == "s":
            state["decisions"][state["track"]] = "skipped"
            state["track"] = (state["track"] + 1) % len(FIXTURE)
            state["cand"] = 0
        elif k == "f":
            state["decisions"][state["track"]] = "not found"
            state["track"] = (state["track"] + 1) % len(FIXTURE)
            state["cand"] = 0
        elif k == "up":
            state["track"] = (state["track"] - 1) % len(FIXTURE)
            state["cand"] = 0
        elif k == "down":
            state["track"] = (state["track"] + 1) % len(FIXTURE)
            state["cand"] = 0

    _clear()
    print("PROTOTYPE ended. Nothing was written anywhere.")
    print(_state_line(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
