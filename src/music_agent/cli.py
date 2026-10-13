"""`music-agent` — the command line the working procedure drives.

This process owns every deterministic step and every write, so a run can be replayed with no model in the
loop. The skill reads what this prints and makes the calls that need judgement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import acquire
from . import config as config_module
from . import genre
from . import importing
from . import library
from . import mcp
from . import process
from . import reference
from .parsing import fold
from . import review
from .rules import hms
from . import slsk
from . import state
from . import tracklist


def _build_parser() -> argparse.ArgumentParser:
    # On a parser used as a parent, a default would overwrite a value given before the subcommand, so both
    # shared arguments are suppress-default and read with getattr.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, metavar="PATH",
                        help="the config file to read (default: config.toml at the repo root)")
    common.add_argument("--job", default=argparse.SUPPRESS, metavar="ID",
                        help="a run by id; by default the newest unfinished one, else the newest")

    parser = argparse.ArgumentParser(
        prog="music-agent", parents=[common],
        description="Acquire, process and import tracks, following the design notes")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", parents=[common],
                                 help="what the current run has done, and what needs you")
    status.add_argument("--queue", action="store_true",
                        help="list the tracks that need a decision, with what was offered")
    status.add_argument("--files", action="store_true",
                        help="list what is in a lane or in staging with nothing pointing at it")

    review_command = commands.add_parser(
        "review", parents=[common],
        help="settle a track by hand: take a candidate, skip it, or call it not found")
    review_command.add_argument("--accept", type=int, default=None, metavar="IDX",
                                help="take a candidate for that track: fetch it and hand it to process")
    review_command.add_argument("--candidate", type=int, default=None, metavar="N",
                                help="which of the candidates on offer to take, as status --queue lists "
                                     "them (1 by default when only one is offered)")
    review_command.add_argument("--skip", type=int, default=None, metavar="IDX",
                                help="pass that track over for now")
    review_command.add_argument("--not-found", dest="not_found", type=int, default=None, metavar="IDX",
                                help="no usable copy exists for that track")
    review_command.add_argument("--list", action="store_true",
                                help="print the queue and stop; what a run with no decision flag does")

    acquire = commands.add_parser("acquire", parents=[common],
                                  help="search Soulseek for a tracklist, into staging")
    acquire.add_argument("--tracks", type=Path, default=None, metavar="FILE",
                         help="the tracklist to start a run from; omit it to continue a run")
    acquire.add_argument("--name", default=None, metavar="NAME",
                         help="what to call the run (default: the tracklist's own name)")
    acquire.add_argument("--retry", action="store_true",
                         help="a later pass over what failed, preferring peers with free slots")

    commands.add_parser("process", parents=[common],
                        help="convert, place and normalize what was downloaded")

    importing = commands.add_parser("import", parents=[common],
                                    help="add what was tagged to the rekordbox collection")
    importing.add_argument("--no-backup", action="store_true",
                           help="skip copying the collection first (not recommended)")

    commands.add_parser("retag", parents=[common],
                        help="re-derive the genre on files already placed, replacing what they carry")
    commands.add_parser("reconcile", parents=[common],
                        help="rebuild the run's records from the files it downloaded")
    return parser


def _import(config: config_module.Config, args: argparse.Namespace) -> int:
    """The one stage that has to run with rekordbox closed."""
    job = _target(config, args, "import")
    if job is None:
        return 1

    from pyrekordbox import Rekordbox6Database

    try:
        database = Rekordbox6Database()
    except Exception as error:
        print(f"cannot open the collection ({error})", file=sys.stderr)
        return 1

    try:
        summary = importing.run(config, job, db=database, backup_first=not args.no_backup)
    except importing.NotClosed as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        database.close()

    _report(summary, job, len(summary.failed))
    return 0


def _job(config: config_module.Config, args: argparse.Namespace) -> state.Job | None:
    """The run a command works on: one named, else the newest unfinished, else the newest of all.

    The fallback matters: the import closes a run, but a finished run still holds a review queue, tracks
    waiting on a genre, and leftovers in a lane. Without the fallback only `status --job` could reach them.
    """
    if getattr(args, "job", None):
        return state.find(config, args.job)
    return state.newest_unfinished(config) or state.newest(config)


def _target(config: config_module.Config, args: argparse.Namespace, action: str) -> state.Job | None:
    """The run to work on, or None with the reason already printed."""
    job = _job(config, args)
    if job is None:
        if getattr(args, "job", None):
            print(f"nothing to {action}: no run called {args.job!r}", file=sys.stderr)
        else:
            print(f"nothing to {action}: no run under {config.state_root}", file=sys.stderr)
    return job


def _tools(config: config_module.Config) -> process.Tools:
    """The stage tools, over the collection's own genre vocabulary."""
    shelf = library.open(config)
    return process.Tools.live(config, vocabulary=genre.vocabulary(shelf.genres(), genre.approved()),
                              mappings=genre.table())


def _report(summary, job: state.Job, failures: int = 0) -> None:
    """The two lines a stage ends with: what it came to, and where to look when something needs a retry."""
    print()
    print(summary.report())
    if failures:
        print(f"music-agent status --job {job.id}")


def _process(config: config_module.Config, args: argparse.Namespace) -> int:
    job = _target(config, args, "continue")
    if job is None:
        return 1

    try:
        tools = _tools(config)
    except (slsk.MissingCredential, library.Unreadable) as error:
        print(error, file=sys.stderr)
        return 1

    summary = process.run(config, job, tools=tools)
    _report(summary, job, len(summary.failed))
    if summary.ungenred:
        print(f"{len(summary.ungenred)} without a genre from the table — the working procedure decides those")
    return 0


def _retag(config: config_module.Config, args: argparse.Namespace) -> int:
    """Re-derive the genre on tracks already placed, over whatever their files carry."""
    job = _target(config, args, "re-tag")
    if job is None:
        return 1

    try:
        tools = _tools(config)
    except (slsk.MissingCredential, library.Unreadable) as error:
        print(error, file=sys.stderr)
        return 1

    summary = process.retag(job, tools=tools)
    _report(summary, job, len(summary.failed))
    return 0


def _reconcile(config: config_module.Config, args: argparse.Namespace) -> int:
    """Rebuild the run's records from the files this run downloaded."""
    job = _target(config, args, "reconcile")
    if job is None:
        return 1

    try:
        summary = process.reconcile(config, job)
    except process.ProcessError as error:
        print(error, file=sys.stderr)
        return 1

    _report(summary, job)
    return 0


def _acquire(config: config_module.Config, args: argparse.Namespace) -> int:
    if args.tracks is not None:
        requests = tracklist.read(args.tracks)
        if not requests:
            print(f"{args.tracks} names no tracks", file=sys.stderr)
            return 1
        job = state.create(config, args.name or args.tracks.stem)
        job.write_records([
            state.Record(idx=index, artist=request.artist, title=request.title, mix=request.mix)
            for index, request in enumerate(requests, start=1)
        ])
        print(f"run {job.id} · {len(requests)} tracks · staging {config.staging}")
    else:
        job = _target(config, args, "continue")
        if job is None:
            return 1

    # Ticket 21: the child logs what each search came to, one line per track — `Search complete: 0 raw
    # results` was the only thing that knew the network had stopped answering, and it was thrown away.
    # stderr keeps the pass's own report clean on stdout, and the filter keeps the walls out of stderr.
    logs = slsk.ChildLog()
    try:
        shelf = library.open(config)
        with mcp.StdioServer(list(config.soulseek_command), env=slsk.environment(config),
                             on_log=logs) as server:
            summary = acquire.run(config, job, server, reference.lookup(config),
                                  library=shelf, retry=args.retry)
        logs.report()
    except (slsk.MissingCredential, mcp.ServerError, library.Unreadable) as error:
        print(error, file=sys.stderr)
        return 1

    _report(summary, job, len(summary.failed))
    if summary.in_review or summary.nothing_usable:
        print(f"music-agent status --job {job.id} --queue")
    if summary.in_review:
        print("the ones needing a decision are settled by hand: "
              "music-agent review --accept IDX [--candidate N]")
    if summary.unsearched:
        print("the network stopped answering part-way through — run this again when it is back")
    return 0


def _queue(job: state.Job) -> state.Queue:
    """The queue, split by why each track is in it. A candidate's number is what `review --accept` takes.

    Returns the queue as well as printing it, so a caller that wants to add to the report does not read the
    run twice.
    """
    waiting = state.queue(job)
    print()
    if waiting.needs_you:
        print(f"{len(waiting.needs_you)} need{'s' if len(waiting.needs_you) == 1 else ''} a decision:")
        for record in waiting.needs_you:
            offered = record.offered()
            reference = f"reference {hms(record.duration_s)}" if record.duration_s else "no reference length"
            print(f"\n  [{record.idx}] {record.label} — {reference}")
            for reason in dict.fromkeys(verdict.reason for verdict in offered):
                print(f"      {reason}")
            for number, verdict in enumerate(offered, start=1):
                candidate = verdict.candidate
                length = f"{hms(candidate.duration)}".strip()
                if candidate.duration is not None and record.duration_s:
                    length += f" {candidate.duration - record.duration_s:+d}s"
                print(f"    {number:>3}. {candidate.ext:<6}{length:<9}{candidate.user:<18}{candidate.name}")
    else:
        print("the queue is empty")

    for heading, tracks in (
        ("not searched — no pass has reached them", waiting.not_searched),
        ("the network offered nothing — a search answered and held no files", waiting.nothing_offered),
        ("with nothing usable — files were offered and every one was refused", waiting.refused),
    ):
        if not tracks:
            continue
        print()
        print(f"{len(tracks)} {heading}:")
        for record in tracks:
            print(f"  [{record.idx}] {record.label}")

    if waiting.decided:
        print()
        print(f"{len(waiting.decided)} settled by hand — no pass will ask about them again:")
        for record in waiting.decided:
            print(f"  [{record.idx}] {record.label}: {record.decision}")

    return waiting


def _files(config: config_module.Config, job: state.Job) -> int:
    """What is in a lane or in staging with nothing pointing at it, and what points at nothing."""
    print(f"run {job.id}")
    try:
        leftovers = library.unpointed(
            config, claimed=[record.local_path for record in job.records() if record.local_path])
    except library.Unreadable as error:
        print(error, file=sys.stderr)
        return 1

    if leftovers.empty():
        print("  nothing left behind: every file in a lane is pointed at, and every row's file is there")
        return 0
    for heading, paths in (
        (f"in staging that this run does not point at", leftovers.staged),
        (f"in a lane that no row and no record points at", leftovers.lane),
        (f"rows whose file has gone", leftovers.gone),
    ):
        if not paths:
            continue
        print(f"  {len(paths)} {heading}:")
        for path in paths:
            print(f"      {path}")
    return 0


def _review(config: config_module.Config, args: argparse.Namespace) -> int:
    """Settle by hand what the rule would not.

    There is no screen: the queue prints as text and the caller shows it as a menu, so a decision is asked
    for where the person already is (the design notes). The flags below stay the one writer of the decision axis,
    which is what keeps a menu, a script and the working procedure from drifting apart.
    """
    job = _target(config, args, "review")
    if job is None:
        return 1

    asked = [flag for flag, given in (("--accept", args.accept), ("--skip", args.skip),
                                      ("--not-found", args.not_found)) if given is not None]
    if len(asked) > 1:
        print(f"one decision at a time, not {' and '.join(asked)}", file=sys.stderr)
        return 1

    try:
        if args.accept is not None:
            logs = slsk.ChildLog()
            settled = review.accept(
                job, args.accept, number=args.candidate,
                server_factory=lambda: mcp.StdioServer(list(config.soulseek_command),
                                                       env=slsk.environment(config), on_log=logs))
            logs.report()
            print(settled.report())
            print(f"music-agent process --job {job.id}   # convert, place and normalize it")
            return 0
        if args.skip is not None:
            print(review.skip(job, args.skip).report())
            return 0
        if args.not_found is not None:
            print(review.not_found(job, args.not_found).report())
            return 0
    except (review.NotInTheQueue, review.NoSuchCandidate, review.NotAccepted) as error:
        print(error, file=sys.stderr)
        return 1
    except (slsk.MissingCredential, mcp.ServerError) as error:
        print(error, file=sys.stderr)
        return 1

    waiting = _queue(job)
    if waiting.needs_you:
        print("\nsettle one by hand: music-agent review --accept IDX --candidate N"
              "   (or --skip IDX, or --not-found IDX)")
    return 0


def _genres(config: config_module.Config, job: state.Job) -> None:
    """Tracks the genre table has nothing for, and names proposed as a genre of their own."""
    ungenred = [record for record in job.records()
                if record.status in {state.TAGGED, state.NORMALIZED} and not record.genre]

    if ungenred:
        print()
        print(f"{len(ungenred)} waiting on a genre — the table has nothing and the working procedure decides:")
        for record in ungenred:
            print(f"  [{record.idx}] {record.label}")
        # The choice has to be among the collection's own genres, so print them — and the database is opened
        # only when there is such a choice to make.
        try:
            for name in sorted(library.open(config).genres()):
                print(f"      {name}")
        except library.Unreadable as error:
            print(f"      (the collection could not be read, so its genres are not listed: {error})")

    proposals = genre.proposals(state.ungenred_names(state.all_jobs(config)), mappings=genre.table())
    if not proposals:
        return
    print()
    print(f"{len(proposals)} name{'s' if len(proposals) != 1 else ''} that fitted nothing, on enough "
          "tracks to become a genre:")
    for proposal in proposals:
        print(f"  {proposal.name!r}, on {proposal.tracks} tracks — to accept it, add both lines to "
              "genres.toml:")
        print(f'      "{fold(proposal.name)}" = "{proposal.name}"')
        print(f'      approved = [..., "{proposal.name}"]')


def _status(config: config_module.Config, args: argparse.Namespace) -> int:
    job = _job(config, args)
    if job is None:
        if getattr(args, "job", None):
            print(f"no run called {args.job!r} under {config.state_root}", file=sys.stderr)
            return 1
        print(f"no runs yet under {config.state_root}")
        return 0

    if args.files:
        return _files(config, job)

    summary = job.summary()
    tracks = summary["tracks"]
    print(f"run {job.id}")
    print(f"  {tracks} track{'s' if tracks != 1 else ''} · {summary['in_review']} in review · "
          f"{summary['settled']} settled · {summary['nothing_usable']} with nothing usable")
    if summary["nothing_usable"]:
        print(f"    of those: {summary['not_searched']} not searched · "
              f"{summary['nothing_offered']} the network offered nothing · "
              f"{summary['refused']} every offer refused")
    if summary["decided"]:
        print(f"  {summary['decided']} settled by hand")
    print(f"  finished: {'yes' if job.finished() else 'no'}")

    if not args.queue:
        return 0

    _queue(job)
    _genres(config, job)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = config_module.load(getattr(args, "config", None))
    if args.command == "status":
        return _status(config, args)
    if args.command == "acquire":
        return _acquire(config, args)
    if args.command == "review":
        return _review(config, args)
    if args.command == "process":
        return _process(config, args)
    if args.command == "retag":
        return _retag(config, args)
    if args.command == "reconcile":
        return _reconcile(config, args)
    if args.command == "import":
        return _import(config, args)
    return 2                                        # unreachable: the subparser requires a command


if __name__ == "__main__":
    raise SystemExit(main())
