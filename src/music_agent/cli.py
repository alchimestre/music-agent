"""`music-agent` — the command line for the pipeline.

This process owns every deterministic step and every write, so a run can be replayed exactly. The caller
reads what this prints and makes the calls that need judgement.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import acquire
from . import config as config_module
from . import exporting
from . import genre
from . import importing
from . import library
from . import process
from . import reference
from . import review
from .rules import hms
from . import slsk
from . import spotify
from . import state
from . import sweep
from . import tracklist
from . import traktor


def _run(config: config_module.Config, args: argparse.Namespace) -> int:
    """A tracklist in, files in every configured collection out: every stage, in order, in one command.

    This is the same code the stage commands run — nothing is decided differently here — so a run needs
    nobody driving between the steps. Nothing is asked along the way: what the rules would not settle is
    printed once at the end, and it can be answered in one batch or left alone.
    """
    from types import SimpleNamespace

    _stage("run", "one tracklist, all the way to the configured collections")

    job = _start(config, args) if args.tracks is not None else _job(config, args)
    if job is None:
        if args.tracks is None:
            print("no run to work on — give --tracks, or a --job to continue", file=sys.stderr)
        return 1

    # Pinned by id from here on: "the newest unfinished run" is not this run once another one starts.
    pinned = SimpleNamespace(job=job.id, tracks=None, name=None, retry=True, no_backup=False,
                             target=getattr(args, "target", None), without=getattr(args, "without", None))

    # One pass is the run. A later pass meets the same peers again, and the ones that answered with nothing
    # an hour ago are the ones that will answer with nothing now; asking is what `--passes` is for, and a
    # pass that brings nothing in ends the loop early whatever the count.
    failed = 0
    for number in range(1, max(1, args.passes) + 1):
        try:
            summary = _acquire_pass(config, job, retry=number > 1)
        except ACQUIRE_ERRORS as error:
            # The passes are abandoned, not the run. Every file they brought in is already written to the run
            # as it went, so what did arrive is still worth placing — ending here is how one stage's bad
            # minute left eighteen downloaded files in staging with nothing pointing at them.
            print(error, file=sys.stderr)
            failed = 1
            break
        _report(summary, job, len(summary.failed))
        if not summary.downloaded:
            break

    for stage, handler in (("process", _process), ("import", _import)):
        code = handler(config, pinned)
        if code:
            print(f"stopped at {stage}", file=sys.stderr)
            return code

    _stage("sweep", "staging")
    leftovers = sweep.find(config)
    removed = sweep.clear(sweep.Leftovers(leftovers.partial, ()), log=print)
    print(f"{removed} interrupted transfer(s) cleared" if removed else "nothing to clear")

    # The code is dropped on purpose: work left for a person is not a failed run, and the pass ends normally
    # either way. Only `music-agent tidy` on its own turns it into an exit status.
    _tidy(config, pinned)

    _queue(job)
    exported = ", ".join(target.name for target in exporting.configured(
        config, only=getattr(args, "target", None), without=getattr(args, "without", None))
        if target.available(config))
    print(f"\nrun {job.id} is exported to {exported or 'no configured target'}.")
    if failed:
        print("the passes stopped early — everything they did bring in has still been placed", file=sys.stderr)
    return failed


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
        description="Acquire, process and import tracks")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser(
        "run", parents=[common],
        help="the whole pipeline in one command: acquire, process, import, sweep")
    run.add_argument("--tracks", default=None, metavar="FILE|URL",
                     help="the tracklist to start from — a file, or a public Spotify playlist's URL")
    run.add_argument("--name", default=None, metavar="NAME",
                     help="what to call the run (default: the tracklist's own name)")
    run.add_argument("--passes", type=int, default=1, metavar="N",
                     help="how many search passes over the tracklist (default: 1; a pass that brings nothing "
                          "in ends the loop early)")
    run.add_argument("--target", action="append", default=None, metavar="NAME",
                     help="export to this target only; repeat for several (default: every [export] target)")
    run.add_argument("--without", action="append", default=None, metavar="NAME",
                     help="leave this target alone; repeat for several")

    status = commands.add_parser("status", parents=[common],
                                 help="what the current run has done, and what needs you")
    status.add_argument("--queue", action="store_true",
                        help="list the tracks that need a decision, with what was offered")
    status.add_argument("--files", action="store_true",
                        help="list what is in a lane or in staging with nothing pointing at it")

    review_command = commands.add_parser(
        "review", parents=[common],
        help="settle a track by hand: take a candidate, skip it, or call it not found")
    review_command.add_argument("--accept", type=_picks, default=None, metavar="IDX[:N],…",
                                help="take candidates: fetch them and hand them to process. One track, or "
                                     "several as IDX:CANDIDATE, comma-separated — the batch fetches "
                                     "concurrently through one server")
    review_command.add_argument("--candidate", type=int, default=None, metavar="N",
                                help="which of the candidates on offer to take, as status --queue lists "
                                     "them (1 by default when only one is offered). Goes with a single "
                                     "--accept")
    review_command.add_argument("--skip", type=_indexes, default=None, metavar="IDX,…",
                                help="pass those tracks over for now; comma-separated for several")
    review_command.add_argument("--not-found", dest="not_found", type=_indexes, default=None,
                                metavar="IDX,…",
                                help="no usable copy exists for those tracks; comma-separated for several")
    review_command.add_argument("--list", action="store_true",
                                help="print the queue and stop; what a run with no decision flag does")

    acquire = commands.add_parser("acquire", parents=[common],
                                  help="search Soulseek for a tracklist, into staging")
    acquire.add_argument("--tracks", default=None, metavar="FILE|URL",
                         help="the tracklist to start a run from — a file, or the URL of a public Spotify "
                              "playlist; omit it to continue a run")
    acquire.add_argument("--name", default=None, metavar="NAME",
                         help="what to call the run (default: the tracklist's own name)")
    acquire.add_argument("--retry", action="store_true",
                         help="a later pass over what failed, preferring peers with free slots")

    commands.add_parser("process", parents=[common],
                        help="convert, place and normalize what was downloaded")

    importing = commands.add_parser("import", parents=[common],
                                    help="add what was tagged to every configured collection")
    importing.add_argument("--no-backup", action="store_true",
                           help="skip copying a collection first (not recommended)")
    importing.add_argument("--target", action="append", default=None, metavar="NAME",
                           help="export to this target only; repeat for several")
    importing.add_argument("--without", action="append", default=None, metavar="NAME",
                           help="leave this target alone; repeat for several")

    commands.add_parser("retag", parents=[common],
                        help="re-derive the genre on files already placed, replacing what they carry")
    commands.add_parser("reconcile", parents=[common],
                        help="rebuild the run's records from the files it downloaded")
    dedupe = commands.add_parser(
        "dedupe", parents=[common],
        help="merge the rows that are one track, keeping the better copy (dry run unless --apply)")
    dedupe.add_argument("--apply", action="store_true",
                        help="write it; without this nothing is merged")
    dedupe.add_argument("--leave-files", action="store_true",
                        help="merge the rows but leave the redundant files where they are")
    backfill = commands.add_parser(
        "backfill", parents=[common],
        help="give a row to every file in a lane the collection has none for (dry run unless --apply)")
    backfill.add_argument("--apply", action="store_true",
                          help="write it; without this nothing is added")
    sweep = commands.add_parser(
        "sweep", parents=[common],
        help="clear staging of what no pass will pick up (dry run unless --apply)")
    sweep.add_argument("--apply", action="store_true",
                       help="remove them; without this nothing is deleted")
    commands.add_parser(
        "tidy", parents=[common],
        help="what a pass left for a person: duplicates, unrowed files, staging and lane drift")
    ignored_peers = commands.add_parser(
        "peers", parents=[common],
        help="the peers that advertise files and deliver nothing, and the ignore they earned")
    ignored_peers.add_argument("--unban", default=None, metavar="USER",
                               help="forget a peer and its failures, so the next pass asks it again")
    return parser


def _import(config: config_module.Config, args: argparse.Namespace) -> int:
    """Every configured collection, in the order the config names them.

    Every stage before this one has to be run in order, so this is also the stage that ends a run. A target
    that is not installed is reported and skipped; the others still receive the tracks.
    """
    job = _target(config, args, "import")
    if job is None:
        return 1

    try:
        targets = exporting.configured(config, only=getattr(args, "target", None),
                                        without=getattr(args, "without", None))
    except exporting.UnknownTarget as error:
        print(error, file=sys.stderr)
        return 1
    if not targets:
        print("no export target selected — check [export] targets, --target and --without",
              file=sys.stderr)
        return 1

    _stage("import", f"{' → '.join(target.name for target in targets)} · run {job.id}")
    try:
        results = exporting.run(config, job, targets=targets, backup_first=not args.no_backup)
    except importing.NotClosed as error:
        print(error, file=sys.stderr)
        return 1
    except Exception as error:              # a collection's own failure, at the CLI boundary
        print(f"the import stopped: {error}", file=sys.stderr)
        return 1

    failed = 0
    for target, result in zip(targets, results):
        line = (result.report() or "nothing to do").replace("\n", "\n" + " " * 13)
        print(f"  {target.name:<11}{line}")
        failed += result.failed
    print()
    if not any(result.ran for result in results):
        print(f"nothing received the run — it stays open: music-agent status --job {job.id}",
              file=sys.stderr)
        return 1
    if failed:
        print(f"music-agent status --job {job.id}")
    return 1 if failed else 0


def _rekordbox(config: config_module.Config):
    """The rekordbox target when this config exports to it and this machine has it."""
    for target in exporting.configured(config):
        if target.name == "rekordbox" and target.available(config):
            return target
    return None


def _rekordbox_or_say(config: config_module.Config, why: str):
    """The rekordbox target, or None with the reason already printed."""
    try:
        target = _rekordbox(config)
    except exporting.UnknownTarget as error:
        print(error, file=sys.stderr)
        return None
    if target is None:
        print(why, file=sys.stderr)
    return target


def _dedupe(config: config_module.Config, args: argparse.Namespace) -> int:
    """The rows that are one track, merged into the better copy — a dry run until `--apply` is given.

    The collection is the whole scope, not a run: a duplicate arrives when something imports a track the
    collection already holds, which is not a run's business to notice. What it leaves behind in Traktor is a
    dead entry, so the report says where to go next.
    """
    from pyrekordbox import Rekordbox6Database

    from . import duplicates

    if _rekordbox_or_say(config, "dedupe reads rekordbox's collection: name it in [export] targets "
                                 "first") is None:
        return 1
    try:
        database = Rekordbox6Database()
    except Exception as error:                      # pyrekordbox raises its own types and its own messages
        print(f"cannot read the rekordbox collection ({error}) — is rekordbox installed?", file=sys.stderr)
        return 1

    try:
        summary = duplicates.run(config, db=database, apply=args.apply,
                                 remove_files=not args.leave_files)
    except duplicates.NotClosed as error:
        print(error, file=sys.stderr)
        return 1

    if not summary.merges:
        print("no duplicates: every track in the collection is held once")
        return 0
    for line in summary.lines():
        print(f"  {line}")
    print()
    if not args.apply:
        print(f"{summary.report()} — nothing merged yet: music-agent dedupe --apply merges them")
        return 0
    print(summary.report())
    if summary.files:
        print("traktor-nml purge takes out the Traktor entries those files left behind")
    return 0


def _backfill(config: config_module.Config, args: argparse.Namespace) -> int:
    """A row for every file in a lane the collection has none for — a dry run until `--apply` is given.

    The other direction from `dedupe`: `import` is scoped to a run's records, so a file placed in a lane outside
    a run never gets a row, and the collection quietly holds less than the library does.
    """
    from pyrekordbox import Rekordbox6Database

    from . import backfill

    if _rekordbox_or_say(config, "backfill writes rekordbox rows: name rekordbox in [export] targets "
                                 "first") is None:
        return 1

    try:
        database = Rekordbox6Database()
    except Exception as error:                      # pyrekordbox raises its own types and its own messages
        print(f"cannot read the rekordbox collection ({error}) — is rekordbox installed?", file=sys.stderr)
        return 1

    try:
        summary = backfill.run(config, db=database, apply=args.apply)
    except backfill.NotClosed as error:
        print(error, file=sys.stderr)
        return 1

    if not summary.candidates:
        print("no gap: every file in the lanes is in the collection")
        return 0
    for line in summary.lines():
        print(f"  {line}")
    print()
    if not args.apply:
        print(f"{summary.report()} — nothing added yet: music-agent backfill --apply adds them")
        return 0
    print(f"{len(summary.minted)} added")
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
    """The stage tools, over the collection's own genre vocabulary.

    The vocabulary is the collection's own names plus the families music-tagger defines, and the mapping onto
    them is that project's taxonomy — read here rather than kept a second time, so the two cannot drift.
    """
    shelf = library.open(config)
    families = genre.families(config.taxonomy)
    if not families:
        print(f"no genre taxonomy at {config.taxonomy or '(unset)'} — genres stay blank; point "
              f"[genres] taxonomy in config.toml at music-tagger's taxonomy.json", file=sys.stderr)
    return process.Tools.live(config, vocabulary=genre.vocabulary(shelf.genres(), families),
                              mappings=genre.table(config.taxonomy))


# Everything a pass can fail on that is worth one line and a stop: credentials, a port someone else holds,
# a daemon that will not answer, a daemon that stopped answering, an unreadable collection.
ACQUIRE_ERRORS = (slsk.MissingCredential, slsk.PortBusy, slsk.DaemonError, acquire.ServerStuck,
                  library.Unreadable, exporting.UnknownTarget)

# A pass is resumed this many times when the daemon dies under it. Everything it did is already in the run,
# so a restart costs the price of a login; four daemons in a row dying is a problem a restart will not fix.
RESTARTS = 3

# What is waited before a fresh daemon is worth trying, once the account's session has been taken and the
# daemon that held it died without logging out. One wait, and it is the top of the measured window: a login starts
# working again twelve to seventeen minutes after the daemon died, and anything below the top spends a login
# inside it — and every attempt is another connection to an account that is refusing them. When the second
# daemon is refused too the pass stops, because a third login buys the same answer.
SESSION_WAITS = (1020.0,)


def _stage(name: str, detail: str = "") -> None:
    """One line saying which stage is running, and on what.

    A run is several stages long and each one used to start silently, so a long run read as if the previous
    stage were still going. This is the line that says where it is.
    """
    print(f"\n── {name}" + (f" · {detail}" if detail else ""))


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
    except (slsk.MissingCredential, library.Unreadable, exporting.UnknownTarget) as error:
        print(error, file=sys.stderr)
        return 1

    _stage("process", f"run {job.id} · convert, place, normalize, tag")
    summary = process.run(config, job, tools=tools)
    _report(summary, job, len(summary.failed))
    if summary.ungenred:
        print(f"{len(summary.ungenred)} without a genre — nothing resolved for them, or the taxonomy "
              f"has no line for what it found")
    return 0


def _retag(config: config_module.Config, args: argparse.Namespace) -> int:
    """Re-derive the genre on tracks already placed, over whatever their files carry."""
    job = _target(config, args, "re-tag")
    if job is None:
        return 1

    try:
        tools = _tools(config)
    except (slsk.MissingCredential, library.Unreadable, exporting.UnknownTarget) as error:
        print(error, file=sys.stderr)
        return 1

    _stage("retag", f"run {job.id} · the genre, re-derived from the files")
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


def _start(config: config_module.Config, args: argparse.Namespace) -> state.Job | None:
    """The run `--tracks` starts: a file, or a public Spotify playlist read the same way one is.

    A playlist is read here rather than written out first, because handing over a URL should cost no more
    than handing over a file — and the text it is parsed from is the text a tracklist file would have held.
    """
    name = None
    if spotify.names_a_playlist(args.tracks):
        try:
            found = spotify.playlist(args.tracks)
        except spotify.PlaylistError as error:
            print(error, file=sys.stderr)
            return None
        requests, name = tracklist.parse(found.text()), found.name
    else:
        try:
            requests = tracklist.read(Path(args.tracks).expanduser())
        except (OSError, UnicodeDecodeError) as error:
            reason = getattr(error, "strerror", None) or error
            print(f"cannot read {args.tracks} ({reason})", file=sys.stderr)
            return None

    if not requests:
        print(f"{args.tracks} names no tracks", file=sys.stderr)
        return None

    job = state.create(config, args.name or name or Path(args.tracks).stem)
    job.write_records([
        state.Record(idx=index, artist=request.artist, title=request.title, mix=request.mix)
        for index, request in enumerate(requests, start=1)
    ])
    print(f"run {job.id} · {len(requests)} tracks · staging {config.staging}")
    return job


def _silence(error: Exception) -> str:
    """What a pass that stopped on its session usually means.

    A second Soulseek client on the same account is the usual cause, and it is not this program's process:
    the server keeps one session per account and closes the other one's connection. A harness's own Soulseek
    MCP server is the one that does it, and killing it does not settle the question — a harness restarts it.
    """
    said = str(error).lower()
    if "session" not in said and "login" not in said:
        return ""
    return ("\n  the account's session is held elsewhere: stop the other client, or let the daemon wait it out")


def _acquire_pass(config: config_module.Config, job: state.Job, *, retry: bool) -> acquire.Summary:
    """Resume a pass through a fresh daemon whenever the daemon dies under it.

    The daemon is somebody else's program and it can go down mid-pass — a crash, a killed process, calls that
    stop returning. Everything it did is already written to the run as it went, so the pass is resumed rather
    than lost: a new daemon, the same records, and a line saying so.

    A session the server has taken away is not that. The daemon that lost it could not log out either — its
    networking is what died — so the server keeps the account until its own timeout passes, and a fresh
    daemon is refused for as long as that takes. Measured here, a login starts working again twelve to
    seventeen minutes after the daemon died, so there is one wait and it is the top of that window. A second
    refusal ends the pass: 2026-10-16 a pass spent five logins finding out what two would have said, and each
    one was another connection to an account that was refusing them.

    A daemon that merely stopped answering is the other kind: it is restarted at once, and the next pass
    carries on from where the run got to. `ServerStuck.lost_session` is what tells them apart.
    """
    lost = 0
    for attempt in range(1, RESTARTS + 2):
        try:
            return _one_pass(config, job, retry=retry, attempt=attempt)
        except (slsk.DaemonError, acquire.ServerStuck) as error:
            if isinstance(error, slsk.DaemonError) and error.method in {"auth", "start"}:
                # A protocol mismatch, another account on the daemon, or a binary that will not run is not
                # mended by trying again: every attempt spends a startup timeout to reach the same answer.
                raise
            if attempt > RESTARTS:
                raise
            if isinstance(error, acquire.ServerStuck) and error.lost_session:
                if lost >= len(SESSION_WAITS):
                    # The ladder is spent. Another attempt is another connection to an account that is
                    # refusing them, which is what the server's own note says extends the refusal, so the
                    # pass ends here and the run reports what it kept.
                    raise
                # The daemon that lost the session cannot get it back; a fresh process is the only thing
                # worth trying, and not before the server has had time to let go.
                slsk.Daemon(config).stop()
                wait = SESSION_WAITS[lost]
                lost += 1
                retry_at = time.strftime("%H:%M", time.localtime(time.time() + wait))
                print(f"the account is still held by the daemon that died — waiting {wait / 60:.0f}m, "
                      f"next login at {retry_at}", file=sys.stderr)
                time.sleep(wait)
            else:
                slsk.Daemon(config).stop()
            print(f"the daemon stopped mid-pass ({error}){_silence(error)}; "
                  f"{RESTARTS + 1 - attempt} more attempt(s)", file=sys.stderr)
    raise AssertionError("unreachable: the loop either returns or raises")


def _one_pass(config: config_module.Config, job: state.Job, *, retry: bool,
              attempt: int = 1) -> acquire.Summary:
    """One pass, everything it needs and nothing else: the daemon, the shares, the records."""
    daemon = slsk.Daemon(config)
    # The daemon is started only when nothing answers on the socket, and the port sweep is part of starting
    # it: a client left on the port by an earlier run answers the same account, and every search then comes
    # back empty — the pass would spend its whole tracklist learning nothing.
    daemon.ensure()
    _ready(daemon, config)
    shelf = library.open(config)
    _stage("acquire", f"{len(job.records())} tracks · pass {attempt}"
                      f"{' · retry' if retry else ''}")
    return acquire.run(config, job, daemon, reference.lookup(config),
                       library=shelf, retry=retry)


def _ready(daemon: slsk.Daemon, config: config_module.Config) -> None:
    """Point the daemon at this pass: staging, the two shared lanes, and a session to search with."""
    config.staging.mkdir(parents=True, exist_ok=True)
    daemon.set_dir(config.staging)
    shared = daemon.share([config.lossless, config.lossy])
    if int(shared.get("files") or 0) == 0:
        # Without shared files peers read the account as a leecher and queue or ban it. The two lanes are
        # handed over above; this says when the daemon did not take them.
        print("the daemon reports no shared files — peers read that as a leecher and may queue or ban",
              file=sys.stderr)


def _acquire(config: config_module.Config, args: argparse.Namespace) -> int:
    if args.tracks is not None:
        job = _start(config, args)
        if job is None:
            return 1
    else:
        job = _target(config, args, "continue")
        if job is None:
            return 1

    try:
        summary = _acquire_pass(config, job, retry=args.retry)
    except ACQUIRE_ERRORS as error:
        print(error, file=sys.stderr)
        return 1

    _report(summary, job, len(summary.failed))
    if summary.dropped:
        print(f"{len(summary.dropped)} closed — every offer was refused, so nothing will ask again")
    if summary.in_review:
        print(f"{len(summary.in_review)} need a decision: music-agent review --job {job.id}")
    if summary.nothing_usable:
        print(f"music-agent status --job {job.id} --queue")
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
    except (library.Unreadable, exporting.UnknownTarget) as error:
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


def _picks(text: str) -> list[tuple[int, int | None]]:
    """`IDX` or `IDX:CANDIDATE`, comma-separated — the numbering `status --queue` prints.

    One track is the common case and stays a bare index; a batch is the same flag with more of them, and
    naming the candidate per track is what makes a batch of them unambiguous.
    """
    picks: list[tuple[int, int | None]] = []
    for piece in text.replace(" ", "").split(","):
        if not piece:
            continue
        idx, colon, number = piece.partition(":")
        try:
            picks.append((int(idx), int(number) if colon else None))
        except ValueError:
            raise argparse.ArgumentTypeError(f"{piece!r} is not IDX or IDX:CANDIDATE")
    if not picks:
        raise argparse.ArgumentTypeError("no track index in that")
    return picks


def _indexes(text: str) -> list[int]:
    """Track indexes, comma-separated. Nothing is fetched, so a batch of these is just several writes."""
    try:
        return [int(piece) for piece in text.replace(" ", "").split(",") if piece]
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a list of track indexes")


def _review(config: config_module.Config, args: argparse.Namespace) -> int:
    """Settle by hand what the rule would not.

    There is no screen: the queue prints as text and the caller renders it as a menu, so a decision is asked
    for where the person already is. The flags below stay the one writer of the decision axis, which is what
    keeps a menu, a script and a hand-typed command from drifting apart.
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
            picks = args.accept
            if args.candidate is not None:
                if len(picks) > 1:
                    print("--candidate names one track's candidate: write IDX:CANDIDATE for each of them",
                          file=sys.stderr)
                    return 1
                picks = [(picks[0][0], args.candidate)]
            def connect():                    # noqa: E731
                daemon = slsk.Daemon(config)
                daemon.ensure()
                daemon.set_dir(config.staging)
                daemon.share([config.lossless, config.lossy])
                return daemon

            if len(picks) == 1:
                settled = review.accept(job, picks[0][0], number=picks[0][1], daemon_factory=connect)
                print(settled.report())
                print(f"music-agent process --job {job.id}   # convert, place and normalize it")
                return 0
            batch = review.accept_many(job, picks, workers=config.workers, daemon_factory=connect)
            for settled in batch.settled:
                print(settled.report())
            for line in batch.refused:
                print(line, file=sys.stderr)
            print(f"{len(batch.settled)} taken, {len(batch.refused)} refused")
            if batch.settled:
                print(f"music-agent process --job {job.id}   # convert, place and normalize these")
            return 1 if batch.refused else 0
        if args.skip is not None:
            for idx in args.skip:
                print(review.skip(job, idx).report())
            return 0
        if args.not_found is not None:
            for idx in args.not_found:
                print(review.not_found(job, idx).report())
            return 0
    except (review.NotInTheQueue, review.NoSuchCandidate, review.NotAccepted) as error:
        print(error, file=sys.stderr)
        return 1
    except (slsk.MissingCredential, slsk.DaemonError) as error:
        print(error, file=sys.stderr)
        return 1

    waiting = _queue(job)
    if waiting.needs_you:
        print("\nsettle one by hand: music-agent review --accept IDX --candidate N"
              "   (or --skip IDX, or --not-found IDX)")
    return 0


def _genres(config: config_module.Config, job: state.Job) -> None:
    """Tracks the taxonomy had nothing for. Reported and counted, never asked about: the vocabulary is
    music-tagger's, and a name it does not know is a line missing there, not a question for a person."""
    # A run ends at `imported`: a set of only the half-way statuses reports nothing on a finished one.
    ungenred = [record for record in job.records()
                if record.status in {state.NORMALIZED, state.TAGGED, state.IMPORTED} and not record.genre]
    if not ungenred:
        return
    print()
    print(f"{len(ungenred)} without a genre — nothing resolved for them, or no line in the taxonomy:")
    for record in ungenred:
        print(f"  [{record.idx}] {record.label}")
    counted = state.ungenred_names(state.all_jobs(config))
    if counted:
        top = sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        print("  names seen on them, most first (add a line under a family in music-tagger's taxonomy):")
        for name, tracks in top:
            print(f"      {name!r} · {tracks}")


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


def _say(label: str, detail: str, action: str = "") -> int:
    """One check's line: what it found, or that it found nothing, and the verb that would act on it."""
    if not detail:
        print(f"  {label:<11} nothing")
        return 0
    print(f"  {label:<11} {detail}")
    if action:
        print(f"  {'':<11} {action}")
    return 1


def _tidy(config: config_module.Config, args: argparse.Namespace) -> int:
    """What a pass left behind, in one look. Nothing is written and every line names the verb that would.

    Four verbs decide these and each one has to be remembered: `dedupe` for two rows that are one track,
    `backfill` for a file in a lane the collection has no row for, `sweep` for staging nothing will pick up,
    and `status --files` for the drift the other way, between a run's records and what is actually on disk.

    This is the last thing a run prints, because the failure it exists for is silence. A duplicate reached
    the collection and nothing said so: the pass that imported it was reading its own records, every other
    check is a verb a person has to know to run, and the track was heard twice before anything looked.
    """
    from pyrekordbox import Rekordbox6Database

    from . import backfill, duplicates

    job = _job(config, args)
    print(f"\ntidy · run {job.id}" if job is not None else "\ntidy")
    trouble = 0

    database = None
    try:
        rekordbox = _rekordbox(config)
    except exporting.UnknownTarget as error:
        print(f"  {'rekordbox':<11} {error}", file=sys.stderr)
        trouble += 1
    else:
        if rekordbox is None:
            print(f"  {'rekordbox':<11} not a configured target — its rows are not read")
        else:
            try:
                database = Rekordbox6Database()
            except Exception as error:              # pyrekordbox raises its own types and its own messages
                print(f"  {'rekordbox':<11} cannot read the collection ({error})", file=sys.stderr)
                trouble += 1

    if database is not None:
        try:
            rows = duplicates.run(config, db=database, apply=False, remove_files=False)
            trouble += _say("duplicates", rows.report() if rows.merges else "",
                            "music-agent dedupe --apply")
        except duplicates.NotClosed as error:
            print(f"  {'duplicates':<11} {error}", file=sys.stderr)
            trouble += 1
        try:
            gaps = backfill.run(config, db=database, apply=False)
            trouble += _say("backfill", gaps.report() if gaps.candidates else "",
                            "music-agent backfill --apply")
        except backfill.NotClosed as error:
            print(f"  {'backfill':<11} {error}", file=sys.stderr)
            trouble += 1
        finally:
            database.close()

    left = sweep.find(config)
    trouble += _say("staging", f"{len(left.all)} to clear" if left.all else "",
                    "music-agent sweep --apply")

    if job is not None:
        try:
            drift = library.unpointed(
                config, claimed=[record.local_path for record in job.records() if record.local_path])
            # `staging` above already owns what is in staging, so this line is the part `sweep` cannot see:
            # a file in a lane nothing points at, and a row whose file has gone.
            counted = (f"{len(drift.lane)} in a lane, {len(drift.gone)} rows whose file has gone")
            trouble += _say("lanes", "" if not (drift.lane or drift.gone) else counted,
                            "music-agent status --files")
        except (library.Unreadable, exporting.UnknownTarget) as error:
            print(f"  {'lanes':<11} {error}", file=sys.stderr)
            trouble += 1

    print("nothing to do" if not trouble else f"{trouble} thing(s) for you")
    return 1 if trouble else 0


def _sweep(config: config_module.Config, args: argparse.Namespace) -> int:
    """Staging's own leftovers: an interrupted transfer, and a file no record points at.

    Not a run's step and not scoped to one — whatever a pass leaves behind, this is what says so and clears
    it. Nothing it removes is a file a record names, and the collection is not touched.
    """
    leftovers = sweep.find(config)
    print(f"staging: {config.staging}")
    print(leftovers.report())
    if not leftovers.all:
        return 0
    if not args.apply:
        print(f"\nmusic-agent sweep --apply   # remove those {len(leftovers.all)}")
        return 0
    print()
    print(f"{sweep.clear(leftovers)} removed")
    return 0


def _peers(config: config_module.Config, args: argparse.Namespace) -> int:
    """The ignore list: who is on it, what earned the place, and how long is left.

    A peer that advertises a file and then delivers nothing is what a paid mirror looks like. Failures are
    counted per user inside a window, and enough of them earn an ignore that expires by itself.
    `--unban` forgets a peer — failures and all — for the one that had a bad week rather than a business
    model.
    """
    from .peers import Ledger, Unreadable

    try:
        ledger = Ledger.at(config)
    except Unreadable as error:
        print(error, file=sys.stderr)
        return 1

    if args.unban:
        if ledger.unban(args.unban):
            print(f"{args.unban} forgotten — the next pass asks them again")
        else:
            print(f"{args.unban} is not on the ledger")
        return 0

    print(f"ledger: {config.peers}")
    rows = ledger.peers()
    if not rows:
        print("no peer has failed a download yet")
        return 0

    width = max(len(peer.user) for peer in rows)
    ignored = 0
    for peer in rows:
        if peer.ignored:
            ignored += 1
            standing = f"ignored, {peer.days_left} days left"
        else:
            standing = "tolerated"
        print(f"  {peer.user:<{width}}  {peer.failures}/{ledger.maxretry}  "
              f"{peer.why or '—':<16}  {standing}")
    print()
    print(f"{len(rows)} peer(s), {ignored} ignored"
          + ("  — music-agent peers --unban USER asks one again" if ignored else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = config_module.load(getattr(args, "config", None))
    if args.command == "run":
        return _run(config, args)
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
    if args.command == "dedupe":
        return _dedupe(config, args)
    if args.command == "backfill":
        return _backfill(config, args)
    if args.command == "sweep":
        return _sweep(config, args)
    if args.command == "tidy":
        return _tidy(config, args)
    if args.command == "peers":
        return _peers(config, args)
    return 2                                        # unreachable: the subparser requires a command


if __name__ == "__main__":
    raise SystemExit(main())
