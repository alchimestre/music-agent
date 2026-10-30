# music-agent

A tracklist in, tracks in every collection you export to: files from Soulseek, lossless kept as AIFF at
−14 LUFS / −1 dBTP, tags from MusicBrainz and AcoustID. Each step is recorded under `.state/jobs/<run>/`,
so a run can be resumed or repeated.

## Start

1. `git submodule update --init` — the loudness normalizer.
2. `uv sync` — Python 3.13+.
3. `cp .env.example .env`, then fill in Soulseek, MusicBrainz and AcoustID credentials.
4. `set -a; source .env; set +a`.

```sh
music-agent run --tracks <file|dir|URL>
```

That is the whole pipeline. It prints one line per stage and asks nothing.

`uv tool install --editable .` puts `music-agent` on PATH from this checkout. The converter and normalizer
ship here; `[tools]` in `config.toml` can point at other copies.

## Commands

| command | what it does | time |
|---|---|---|
| `run --tracks FILE` | acquire, process, export, sweep | hours |
| `run --tracks DIR` | the same, over a lane: every file in it is a track to upgrade | hours |
| `acquire --tracks FILE\|DIR` | search and download into staging | hours |
| `process` | identify, genre, convert, normalize, tag | hours |
| `import` | export to every target; rekordbox must be closed | seconds |
| `status` | what the run did, and what needs you | seconds |
| `status --queue` | tracks not settled, and why | seconds |
| `status --files` | files nothing points at; rows whose file has gone | seconds |
| `review --list` | the queue, numbered, with the reason each was left | seconds |
| `retag` | re-derive genres on files already tagged | minutes |
| `reconcile` | rebuild the run's records from the files it downloaded | seconds |
| `dedupe` | one row per track; dry run, `--apply` merges | seconds |
| `backfill` | a row for every file that has none; dry run, `--apply` adds | seconds |
| `sweep` | staging leftovers; dry run, `--apply` clears | seconds |
| `tidy` | every leftover check: staging, unpointed files, duplicate rows, gone rows | seconds |
| `peers` | peers that never deliver; `--unban USER` asks again | seconds |

`FILE` is a path, or the URL or share link of a public Spotify, Deezer or YouTube playlist. `--job ID`
picks a run; the default is the newest unfinished one, else the newest. A YouTube playlist needs
`yt-dlp` on `PATH` (`brew install yt-dlp`); the other two need nothing configured.

Use the stage commands only for an interrupted run or a failed stage; `run` is the normal path.

## Export targets

`[export] targets` lists the collections, in order; `rekordbox` and `traktor` ship. A target that is not
installed is skipped, one that refuses leaves the run open, and naming one target is a complete config.

```sh
music-agent import --target rekordbox     # one target; repeat for several
music-agent import --without traktor      # leave one out
```

`status --files`, `dedupe`, `backfill` and `tidy` read rekordbox's collection, so they need rekordbox
configured.

Traktor's collection belongs to [traktor-nml](https://github.com/alchimestre/traktor-nml): it owns
`collection.nml` and its rules, and `[traktor] command` says how to run it. Entries `dedupe` leaves behind are
`traktor-nml purge`'s to remove.

Genres come from music-tagger's taxonomy through `[genres] taxonomy`; a genre no line in it covers stays
blank.

## When a track is left over

Almost always empty. A file whose length cannot be checked is taken as it is; only a file whose name names no
track is left for you.

```sh
music-agent review --list                     # the queue, numbered, and why each was left
music-agent review --accept 12 --candidate 2  # fetch that one, then process
music-agent review --skip 12                  # passed over for now
music-agent review --not-found 12             # no usable copy; nothing asks again
```

`--accept`, `--skip` and `--not-found` are the only answers a queued track takes.

## Peers

A peer that advertises, accepts and delivers nothing is a paid mirror. A pass counts those per user:
`maxretry` inside `findtime_days` earns an ignore for `bantime_days`, and a peer that delivers is forgiven.
Stored at `paths.peers`.

## Configuration

`config.toml` beside this file, or `~/.config/music-agent/config.toml` when installed; `--config` reads
another one. It holds directories, loudness targets, Soulseek pace, the ignore policy, `[tools]` paths,
`[export]` targets, and the names of the credential variables. No secrets.

Soulseek takes one client per account. A pass stops another client on this machine before it starts, then
holds the session in a daemon that outlives it.

`SLSK_LISTEN_PORT` lives in `.env`, not `config.toml`: which port this machine can accept peers on is a fact
about its network. Forward it on the router.

## Agents

`skill/SKILL.md` ships with the project and is written for a harness. `run` first, then the stage commands,
and the rare track the queue asks about.

## Tests

```sh
uv run pytest -q
```

No `.env` is needed: the suite supplies a generic MusicBrainz contact.

MIT licensed.
