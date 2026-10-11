#!/usr/bin/env python3
"""PROTOTYPE (textual) — throwaway, fixture data only, writes nothing.

The review screen as a full-screen textual app, so it fills the terminal instead of being pinned to
a fixed width. Queue on the left, candidate cards on the right — the layout chosen from the four
variants — styled the way the JobRadar TUI is: colour carries meaning, never decoration.

    .venv/bin/python prototype/review_tui_textual.py

Everything shown is computed by `review_model`; this file only renders. A candidate that has been
rejected is never shown at all, and a track the system could settle alone never appears in the queue.
"""

from __future__ import annotations

import os
import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import review_model as model  # noqa: E402

# Same semantic map as JobRadar's tui.py: a candidate's label says what would happen to it, so it can
# never be misread as a state. Rejected candidates are absent, so they need no style.
VERDICT_STYLE = {"Ready": "bold cyan", "Needs you": "bold yellow"}
# A track's state is a decision the human made. The two axes never share a word or a glyph.
DECISION_STYLE = {"accepted": "bold green", "skipped": "dim", "not found": "red"}
DECISION_MARK = {"accepted": "✓", "skipped": "–", "not found": "✗"}


def _mark(dec):
    return DECISION_MARK.get(dec, "·")


class CandidateCard(Static):
    """One candidate, rendered the way the chosen variant's cards read."""

    def __init__(self, cand, label, reason, ref, selected):
        super().__init__()
        self.cand, self.label, self.reason, self.ref, self.selected = cand, label, reason, ref, selected
        if selected:
            self.add_class("selected")

    def render(self) -> Text:
        c = self.cand
        ver = _ver(c)
        t = Text()
        t.append(self.label, style=VERDICT_STYLE.get(self.label, ""))
        t.append(" " * max(1, 12 - len(self.label)))
        t.append(model.stem_of(c), style="bold")
        if ver:
            # The spacer stays outside the styled span, or the chip's background paints over it.
            t.append("  ")
            t.append(ver, style="bold reverse")
        t.append("\n")
        t.append("%s · %s · " % (c["ext"], model.hms(c["duration"])), style="dim")
        d = model.delta(c, {"ref_duration": self.ref})
        if d is None:
            t.append("Δ ?", style="dim")
        else:
            t.append("Δ%+ds" % d, style="bold green" if abs(d) <= 1 else "bold yellow")
        t.append(" · %s · %.1f MB" % (("%dk" % c["bitrate"]) if c["bitrate"] else "lossless",
                                      c["size_mb"]))
        t.append("\n")
        t.append("peer %s · queue %s · slots %s" % (c["user"], c["queue"],
                                                    "yes" if c["slots"] else "no"), style="dim")
        if self.selected:
            t.append("\n")
            t.append(self.reason, style="italic")
        return t


def _ver(cand):
    return model.version_of(os.path.basename(cand["file"].replace("\\", "/")))


class ReviewApp(App):
    """Full-screen review queue over the fixture."""

    TITLE = "music-agent review"
    SUB_TITLE = "PROTOTYPE · fixture data · nothing is saved"

    CSS = """
    #body { height: 1fr; }
    #queue { width: 52%; }
    #pane { width: 1fr; padding: 0 1; }
    #status { height: 1; background: $boost; padding: 0 1; }
    CandidateCard { border: round $primary; padding: 0 1; margin-bottom: 1; }
    CandidateCard.selected { border: round $accent; }
    """

    BINDINGS = [
        ("a", "accept", "Accept"),
        ("s", "skip", "Skip"),
        ("f", "not_found", "Not found"),
        ("n", "next_candidate", "Next cand"),
        ("p", "prev_candidate", "Prev cand"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.tracks = model.queue()
        self.track, self.cand, self.decisions = 0, 0, {}

    # -- composition -------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield DataTable(id="queue", zebra_stripes=True, cursor_type="row")
            yield VerticalScroll(id="pane")
        yield Static(id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = "PROTOTYPE · %s" % model.summary()
        t = self.query_one("#queue", DataTable)
        t.add_column("DECISION", width=9)
        t.add_column("TRACK")
        self._fill_queue()
        t.focus()
        await self.show_detail()

    # -- rendering ---------------------------------------------------------
    def _fill_queue(self) -> None:
        t = self.query_one("#queue", DataTable)
        t.clear()
        for i, fx in enumerate(self.tracks):
            dec = self.decisions.get(i, "")
            style = DECISION_STYLE.get(dec, "")
            req = fx["request"]
            t.add_row(Text(_mark(dec), style=style or "dim"),
                      Text("%s - %s" % (req["artist"], req["title"]), style=style))
        t.move_cursor(row=self.track)

    async def show_detail(self) -> None:
        pane = self.query_one("#pane", VerticalScroll)
        await pane.remove_children()
        fx = self.tracks[self.track]
        req = fx["request"]

        head = Text()
        head.append("%s - %s" % (req["artist"], req["title"]), style="bold")
        if req["version"]:
            head.append("  ")
            head.append("(%s)" % req["version"], style="bold reverse")
        head.append("    reference %s" % model.hms(req["ref_duration"]), style="dim")
        head.append("\n")
        head.append(fx["flag"], style="italic dim")
        pane.mount(Static(head))

        for i, (c, label, reason) in enumerate(model.visible(fx)):
            pane.mount(CandidateCard(c, label, reason, req["ref_duration"], i == self.cand))
        self.show_status()

    def show_status(self) -> None:
        fx = self.tracks[self.track]
        counts: dict[str, int] = {}
        for v in self.decisions.values():
            counts[v] = counts.get(v, 0) + 1
        parts = ", ".join("%s=%d" % kv for kv in sorted(counts.items())) or "nothing decided yet"
        self.query_one("#status", Static).update(
            "%s - %s   ·   candidate %d/%d   ·   %s"
            % (fx["request"]["artist"], fx["request"]["title"],
               self.cand + 1, len(model.visible(fx)), parts))

    # -- events ------------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "queue" and event.cursor_row != self.track:
            self.track, self.cand = event.cursor_row, 0
            self.call_later(self.show_detail)

    # -- actions -----------------------------------------------------------
    async def _advance(self, decision: str) -> None:
        self.decisions[self.track] = decision
        self.track = (self.track + 1) % len(self.tracks)
        self.cand = 0
        self._fill_queue()
        await self.show_detail()

    async def action_accept(self) -> None:
        await self._advance("accepted")

    async def action_skip(self) -> None:
        await self._advance("skipped")

    async def action_not_found(self) -> None:
        await self._advance("not found")

    async def action_next_candidate(self) -> None:
        n = len(model.visible(self.tracks[self.track]))
        self.cand = (self.cand + 1) % n
        await self.show_detail()

    async def action_prev_candidate(self) -> None:
        n = len(model.visible(self.tracks[self.track]))
        self.cand = (self.cand - 1) % n
        await self.show_detail()


if __name__ == "__main__":
    ReviewApp().run()
