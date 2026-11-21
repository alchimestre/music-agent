"""Shared acquisition and processing pass helpers."""

from __future__ import annotations

import sys

from . import acquire, exporting, library, peers, process, reference, slsk, sweep
from . import config as config_module
from . import state

ACQUIRE_ERRORS = (slsk.MissingCredential, slsk.PortBusy, slsk.DaemonError, acquire.ServerStuck,
                  library.Unreadable, peers.Unreadable, exporting.UnknownTarget)


def stage(name: str, detail: str = "", *, stream=None) -> None:
    """Report which stage is running."""
    print(f"\n── {name}" + (f" · {detail}" if detail else ""), file=stream)


def tools(config: config_module.Config, *, shelf: library.Library | None = None,
          warn=None) -> process.Tools:
    genres = library.genres(config) if shelf is None else shelf.genres()
    return process.Tools.for_collection(config, genres, warn=warn)


def one_pass(config: config_module.Config, job: state.Job, *, retry: bool,
             attempt: int = 1, log=print, stream=None, progress=None) -> acquire.Summary:
    """Run one acquisition pass against the shared lanes."""
    daemon = slsk.Daemon(config)
    # Reading the library after opening the socket can let upload events fill it and drop the connection.
    shelf = library.open(config)
    daemon.ensure()
    _ready(daemon, config)
    stage("acquire", f"{len(job.records())} tracks · pass {attempt}{' · retry' if retry else ''}",
          stream=stream)
    return acquire.run(config, job.execution(), daemon, reference.lookup(config),
                       library=shelf, retry=retry, log=log, stream=stream, progress=progress)


def _ready(daemon: slsk.Daemon, config: config_module.Config) -> None:
    """Point the daemon at this pass: staging, the two shared lanes, and a session to search with."""
    config.staging.mkdir(parents=True, exist_ok=True)
    daemon.set_dir(config.staging)
    shared = daemon.share(slsk.share_paths(config))
    if int(shared.get("files") or 0) == 0:
        # A successful share command can still leave the account with no shared files.
        print("the daemon reports no shared files — peers read that as a leecher and may queue or ban",
              file=sys.stderr)


def clear_partial_transfers(config: config_module.Config, *, log=print) -> int:
    leftovers = sweep.find(config)
    return sweep.clear(sweep.Leftovers(leftovers.partial, ()), log=log)
