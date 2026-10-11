"""`music-agent` — the command line the working procedure drives.

Ticket 07: this process owns every deterministic step and every write, so a run can be replayed with no
model in the loop. The skill reads what this prints and makes the calls that need judgement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import acquire
from . import config as config_module
from . import library
from . import mcp
from . import process
from . import reference
from . import slsk
from . import state
from . import tracklist


def _build_parser() -> argparse.ArgumentParser:
    # On a parser used as a parent, a default here would overwrite a value given before the subcommand,
    # so --config is suppress-default and read with getattr.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, metavar="PATH",
                        help="the config file to read (default: config.toml at the repo root)")

    parser = argparse.ArgumentParser(
        prog="music-agent", parents=[common],
        description="Acquire, process and import tracks, following the design notes")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", parents=[common],
                                 help="what the current run has done, and what needs you")
    status.add_argument("--job", default=None, metavar="ID",
                        help="a run by id; the newest unfinished one by default")
    status.add_argument("--queue", action="store_true",
                        help="list the tracks that need a decision, with what was offered")

    acquire = commands.add_parser("acquire", parents=[common],
                                  help="search Soulseek for a tracklist, into staging")
    acquire.add_argument("--tracks", type=Path, default=None, metavar="FILE",
                         help="the tracklist to start a run from; omit it to continue a run")
    acquire.add_argument("--name", default=None, metavar="NAME",
                         help="what to call the run (default: the tracklist's own name)")
    acquire.add_argument("--job", default=None, metavar="ID",
                         help="a run by id; the newest unfinished one by default")
    acquire.add_argument("--retry", action="store_true",
                         help="a later pass over what failed, preferring peers with free slots")

    process = commands.add_parser("process", parents=[common],
                                  help="convert, place and normalize what was downloaded")
    process.add_argument("--job", default=None, metavar="ID",
                         help="a run by id; the newest unfinished one by default")
    return parser


def _job(config: config_module.Config, args: argparse.Namespace) -> state.Job | None:
    """The run a command works on: one named, or the newest that never finished."""
    if getattr(args, "job", None):
        return state.find(config, args.job)
    return state.newest_unfinished(config)


def _process(config: config_module.Config, args: argparse.Namespace) -> int:
    job = _job(config, args)
    if job is None:
        print(f"nothing to continue: no run under {config.state_root}", file=sys.stderr)
        return 1

    summary = process.run(config, job)
    print()
    print(summary.report())
    if summary.failed:
        print(f"music-agent status --job {job.id}")
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
        job = state.find(config, args.job) if args.job else state.newest_unfinished(config)
        if job is None:
            where = repr(args.job) if args.job else "any run"
            print(f"nothing to continue: no run called {where} under {config.state_root}",
                  file=sys.stderr)
            return 1

    try:
        shelf = library.open(config)
        with mcp.StdioServer(list(config.soulseek_command), env=slsk.environment(config)) as server:
            summary = acquire.run(config, job, server, reference.lookup(config),
                                  library=shelf, retry=args.retry)
    except (slsk.MissingCredential, mcp.ServerError, library.Unreadable) as error:
        print(error, file=sys.stderr)
        return 1

    print()
    print(summary.report())
    if summary.in_review or summary.nothing_usable:
        print(f"music-agent status --job {job.id} --queue")
    return 0


def _status(config: config_module.Config, args: argparse.Namespace) -> int:
    job = state.find(config, args.job) if args.job else state.newest_unfinished(config)
    if job is None:
        if args.job:
            print(f"no run called {args.job!r} under {config.state_root}", file=sys.stderr)
            return 1
        print(f"no runs yet under {config.state_root}")
        return 0

    summary = job.summary()
    tracks = summary["tracks"]
    print(f"run {job.id}")
    print(f"  {tracks} track{'s' if tracks != 1 else ''} · {summary['in_review']} in review · "
          f"{summary['settled']} settled · {summary['nothing_usable']} with nothing usable")
    print(f"  finished: {'yes' if job.finished() else 'no'}")

    if not args.queue:
        return 0

    records = job.records()
    queue = [record for record in records if record.needs_you()]
    unusable = [record for record in records if not record.usable()]

    print()
    if not queue:
        print("the queue is empty")
    else:
        print(f"{len(queue)} need{'s' if len(queue) == 1 else ''} a decision:")
        for record in queue:
            print(f"  [{record.idx}] {record.label}")
            for verdict in record.offered():
                print(f"        {verdict.label.value:<9} {verdict.candidate.user:<18}"
                      f"{verdict.candidate.ext:<6}{verdict.reason}")

    if unusable:
        print()
        print(f"{len(unusable)} with nothing usable — another search is needed:")
        for record in unusable:
            print(f"  [{record.idx}] {record.label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = config_module.load(getattr(args, "config", None))
    if args.command == "status":
        return _status(config, args)
    if args.command == "acquire":
        return _acquire(config, args)
    if args.command == "process":
        return _process(config, args)
    return 2                                        # unreachable: the subparser requires a command


if __name__ == "__main__":
    raise SystemExit(main())
