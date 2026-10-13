"""The review queue as a screen.

`music-agent review` draws the run's queue and writes a decision when a key says so. Every decision goes
through `review`, so the screen and the flags cannot drift apart, and a rejected candidate is not drawn at
all. Layout: queue on the left, candidate cards on the right, colour carrying meaning rather than decoration.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import Worker

from . import mcp, review, slsk, state
from .config import Config
from .rules import Candidate, Label, Verdict, hms

# What a candidate's label would do to it. Rejected has no style: a rejection never reaches a card.
LABEL_STYLE = {Label.READY.value: "bold cyan", Label.NEEDS_YOU.value: "bold yellow"}

# A decision is what the user did about a track, and never shares a word or a glyph with a label.
DECISION_STYLE = {state.ACCEPTED: "bold green", state.SKIPPED: "dim", state.DECLINED: "red"}
DECISION_MARK = {state.ACCEPTED: "✓", state.SKIPPED: "–", state.DECLINED: "✗"}
UNDECIDED = "·"
DECISIONS = (state.ACCEPTED, state.SKIPPED, state.DECLINED)

# How near the reference a length has to be for the Δ to read as close. Not the matching window
# (`rules.DURATION_WINDOW`, one second): the two are separate, so widening the rule cannot recolour what it
# offers.
CLOSE_ENOUGH = 5

# The trailing parenthesised mix, printed on its own line so no width can truncate the one token the version
# rule turns on.
TRAILING_MIX = re.compile(r"\s*\(([^()]*)\)\s*$")


def split_mix(name: str) -> tuple[str, str]:
    """A file's name without its extension or trailing mix, and the mix itself.

    A bare name simply has no mix line, which shows the rule rather than describing it.
    """
    body = name.rsplit(".", 1)[0] if "." in name else name
    found = TRAILING_MIX.search(body)
    if found is None:
        return body, ""
    return body[: found.start()].strip(), found.group(1).strip()


def delta_text(candidate: Candidate, reference: int | None) -> Text:
    """How far the file's length is from the reference, coloured by how close that is."""
    if candidate.duration is None or reference is None:
        return Text("Δ ?", style="dim")
    offset = candidate.duration - reference
    if offset == 0:
        style = "bold green"
    elif abs(offset) <= CLOSE_ENOUGH:
        style = "bold yellow"
    else:
        style = "bold red"
    return Text(f"Δ{offset:+d}s", style=style)


class CandidateCard(Static):
    """One candidate, rendered the way the chosen variant's cards read."""

    def __init__(self, verdict: Verdict, reference: int | None, *, selected: bool) -> None:
        super().__init__()
        self.verdict = verdict
        self.reference = reference
        self.selected = selected
        if selected:
            self.add_class("selected")

    def render(self) -> Text:
        candidate = self.verdict.candidate
        stem, mix = split_mix(candidate.name)
        label = self.verdict.label.value

        line = Text()
        line.append(label, style=LABEL_STYLE[label])
        line.append(" " * max(1, 12 - len(label)))
        line.append(stem, style="bold")
        if mix:
            # The spacer stays outside the styled span: a reversed gap before the bracket reads as a
            # rendering fault rather than emphasis (the design notes, amendment 2).
            line.append("  ")
            line.append(f"({mix})", style="bold reverse")
        line.append("\n")
        line.append(f"{candidate.ext} · {hms(candidate.duration)} · ", style="dim")
        line.append_text(delta_text(candidate, self.reference))
        line.append("\n")
        line.append(f"peer {candidate.user} · queue {candidate.queue} · "
                    f"slots {'yes' if candidate.slots else 'no'}", style="dim")
        if self.selected:
            line.append("\n")
            line.append(self.verdict.reason, style="italic")
        return line


class ReviewApp(App[int]):
    """The run's review queue, full screen."""

    TITLE = "music-agent review"

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
        ("n", "next_candidate", "Next candidate"),
        ("p", "prev_candidate", "Previous candidate"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, job: state.Job,
                 server_factory: Callable[[], mcp.StdioServer]) -> None:
        super().__init__()
        self.config = config
        self.job = job
        self.server_factory = server_factory
        # The row set is taken once, from the queue this run is asking about. A row that left the table the
        # moment it was decided would move the cursor out from under the hand that just pressed the key;
        # what follows the durable state is the mark in the first column and the counts on the status line.
        self.records = state.queue(job).needs_you
        self.at = 0                              # the row the cursor is on
        self.card = 0                            # which of that track's candidates is selected
        self.decisions: dict[int, str] = {}      # what this session has settled, by track
        self.message = ""                        # one line of what just happened, or what is happening
        self.fetching: Worker[None] | None = None  # the fetch in flight, if there is one
        self.asking = ""                         # the file that fetch asked a peer for

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield DataTable(id="queue", zebra_stripes=True, cursor_type="row")
            yield VerticalScroll(id="pane")
        yield Static(id="status")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.add_column("DECISION", key="decision", width=9)
        table.add_column("TRACK", key="track")
        for record in self.records:
            table.add_row(*self._cells(record), key=str(record.idx))
        table.focus()
        await self._show()

    # ── drawing ──────────────────────────────────────────────────────────────

    def _cells(self, record: state.Record) -> tuple[Text, Text]:
        """A row's two cells: the mark the decision shows, and the track in the decision's colour."""
        decision = self.decisions.get(record.idx, "")
        style = DECISION_STYLE.get(decision, "")
        return (Text(DECISION_MARK.get(decision, UNDECIDED), style=style or "dim"),
                Text(record.label, style=style))

    async def _show(self) -> None:
        """Draw the pane for the track under the cursor, and the status line under everything."""
        pane = self.query_one("#pane", VerticalScroll)
        await pane.remove_children()
        record = self._under_cursor()
        if record is not None:
            await pane.mount(Static(self._headline(record)))
            await pane.mount(*[CandidateCard(verdict, record.duration_s, selected=number == self.card)
                               for number, verdict in enumerate(record.offered())])
        self._status()

    def _headline(self, record: state.Record) -> Text:
        head = Text()
        head.append(f"{record.artist} - {record.title}", style="bold")
        if record.mix:
            # The spacer stays outside the styled span, as on the cards.
            head.append("  ")
            head.append(f"({record.mix})", style="bold reverse")
        head.append(f"    reference {hms(record.duration_s)}", style="dim")
        return head

    def _status(self) -> None:
        self.query_one("#status", Static).update(self._status_line())

    def _status_line(self) -> Text:
        parts = [self.job.id]
        record = self._under_cursor()
        if record is not None:
            offered = record.offered()
            if offered:
                parts.append(f"candidate {self.card + 1}/{len(offered)}")
        counted = Counter(self.decisions.values())
        decided = ", ".join(f"{counted[decision]} {decision}" for decision in DECISIONS
                            if counted[decision])
        parts.append(decided or "nothing decided yet")
        if self.message:
            parts.append(self.message)
        return Text("   ·   ".join(parts))

    def _mark(self, idx: int) -> None:
        """Repaint one row, in place, so the cursor does not move under a decision."""
        record = self._record(idx)
        if record is None:
            return
        mark, track = self._cells(record)
        table = self.query_one("#queue", DataTable)
        table.update_cell(str(idx), "decision", mark)
        table.update_cell(str(idx), "track", track)

    # ── what the cursor is on ────────────────────────────────────────────────

    def _under_cursor(self) -> state.Record | None:
        return self.records[self.at] if self.records else None

    def _record(self, idx: int) -> state.Record | None:
        return next((record for record in self.records if record.idx == idx), None)

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "queue" or event.cursor_row == self.at:
            return
        self.at = event.cursor_row
        self.card = 0
        await self._show()

    # ── the decisions ────────────────────────────────────────────────────────

    def action_accept(self) -> None:
        """Fetch the candidate the cursor is on.

        The peer may take minutes to answer, so the fetch runs on a worker thread and the screen stays
        usable while it does; what it finds out arrives on the status line.
        """
        record = self._under_cursor()
        if record is None:
            return
        offered = record.offered()
        if not offered:
            self._note(f"{record.label}: nothing on offer to take")
            return
        candidate = offered[self.card].candidate
        if self.fetching is not None and self.fetching.is_running:
            # One fetch at a time. A thread cannot be called back, so a second `a` would ask the same peer
            # for the same file twice and write two settlements.
            self._note(f"still fetching {self.asking}")
            return
        self.asking = candidate.name
        self._note(f"asking {candidate.user} for {candidate.name}")
        self.fetching = self.run_worker(lambda: self._fetch(record.idx, self.card + 1), thread=True,
                                        group="accept", exclusive=True)

    async def action_skip(self) -> None:
        await self._write(review.skip)

    async def action_not_found(self) -> None:
        await self._write(review.not_found)

    async def action_next_candidate(self) -> None:
        await self._move_card(1)

    async def action_prev_candidate(self) -> None:
        await self._move_card(-1)

    async def _move_card(self, step: int) -> None:
        """`n` and `p` wrap, so the last candidate comes back round to the first."""
        record = self._under_cursor()
        offered = record.offered() if record is not None else []
        if not offered:
            return
        self.card = (self.card + step) % len(offered)
        await self._show()

    async def _write(self, settle: Callable[[state.Job, int], review.Settled]) -> None:
        """Skip and not-found are one write to the run's records, so they run where they are called."""
        record = self._under_cursor()
        if record is None:
            return
        try:
            settled = settle(self.job, record.idx)
        except (review.NotInTheQueue, review.NoSuchCandidate, review.NotAccepted) as error:
            self._note(str(error))               # the run moved on under us; the queue is still usable
            return
        await self._done(settled)

    def _fetch(self, idx: int, number: int) -> None:
        """On the worker thread: `review.accept` fetches one file and writes the decision."""
        try:
            settled = review.accept(self.job, idx, number=number,
                                    server_factory=self.server_factory, log=self._log)
        except (review.NotAccepted, review.NoSuchCandidate, review.NotInTheQueue,
                mcp.ServerError, slsk.MissingCredential) as error:
            self._say(str(error))
        except Exception as error:               # a surprise must not close the screen on the user
            self._say(f"{type(error).__name__}: {error}")
        else:
            self._from_worker(self._done, settled)

    def _log(self, message: str) -> None:
        """`review.accept` narrates as it goes, from the thread that is doing the fetching."""
        self._say(message.strip())

    def _say(self, message: str) -> None:
        self._from_worker(self._note, message)

    def _from_worker(self, callback, *args) -> None:
        """Hand a worker's result to the UI thread, which owns every widget.

        A screen the user has already quit is not a failure: the fetch is the CLI's business by then.
        """
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            pass

    def _note(self, message: str) -> None:
        self.message = message
        self._status()

    async def _done(self, settled: review.Settled) -> None:
        """Mark the track, say what it came to, and move to the next one."""
        self.decisions[settled.idx] = settled.decision
        self.message = settled.report()
        self._mark(settled.idx)
        self.at = (self.at + 1) % len(self.records)
        self.card = 0
        self.query_one("#queue", DataTable).move_cursor(row=self.at)
        await self._show()


def _server(config: Config) -> mcp.StdioServer:
    """The acquisition server, opened only when a file actually has to be fetched."""
    return mcp.StdioServer(list(config.soulseek_command), env=slsk.environment(config))


def run(config: Config, job: state.Job, *, server_factory=None) -> int:
    """Review a run's queue. Returns 0, which is the only way the screen closes."""
    # The factory is deferred on purpose: reading the credentials and starting the child is work only a
    # fetch needs, and most decisions never fetch anything at all.
    app = ReviewApp(config, job, server_factory or (lambda: _server(config)))
    if not app.records:
        print(f"{job.id}: nothing needs a decision")
        return 0
    return app.run() or 0
