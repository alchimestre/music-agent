# music-agent

Turns a tracklist into tracks in a rekordbox collection and Traktor's: finds the files on Soulseek, keeps
lossless ones as AIFF, normalizes loudness to −14 LUFS / −1 dBTP, writes tags from MusicBrainz and AcoustID, and
imports the result into both. Every step is written under `.state/jobs/<run>/`, so a run can be read back,
resumed, or repeated.

## Install

```sh
git submodule update --init    # the normalizer `process` drives
uv sync                        # a virtualenv, from a checkout
uv tool install --editable .   # the command on PATH, from this checkout
uv tool install git+https://github.com/alchimestre/music-agent   # or with no checkout
```

The converter to AIFF and the loudness normalizer both ship with the project — `tools/convert_to_aiff.sh` and
the `vendor/loudness_normalizer` submodule — and `config.toml` can point `[tools]` at other copies.

Soulseek, MusicBrainz and AcoustID need credentials, and the two Soulseek listening ports depend on the router
this runs behind. `config.toml` holds the environment variable names; the values live in `.env`, which
`.env.example` is the template for:

```sh
cp .env.example .env    # then fill it in
set -a; source .env; set +a
```

## Commands

One command does the whole job:

```
music-agent run --tracks <file|URL>             # acquire, process, import both collections, sweep
```

It prints a line as each stage starts and asks nothing along the way. The stages are also commands of their own,
for a run that was interrupted or a stage that failed.

```
music-agent acquire --tracks <file|URL>         # the tracklist, searched and downloaded into staging
music-agent status --queue                      # what the run has not settled, and why
music-agent review                              # the queue as text
music-agent process                             # identify, genre, convert, normalize, tag
music-agent retag                               # re-derive the genre on files already tagged
music-agent reconcile                           # rebuild the records from the files this run made
music-agent status --files                      # files nothing points at, and rows whose file is gone
music-agent import                              # rekordbox, then Traktor; rekordbox must be closed
music-agent dedupe                              # one row per track; dry run, --apply merges
music-agent backfill                            # a row for every file that has none; dry run, --apply adds
music-agent sweep                               # staging's leftover .part files; --apply clears
```

`acquire` and `process` take hours on a long tracklist. `review`, `status`, `import`, `dedupe` and `backfill`
take seconds.

`import` is scoped to one run's records. Two verbs keep the collection and the lanes in step, both dry runs
until `--apply`:

| verb | when | what it does |
|---|---|---|
| `dedupe` | two rows for one track: the same folded artist, title and mix, lengths within one second | merges them. The row holding the better copy survives (lossless before lossy, then the row the library has used, then the measured one, then the older), what the other held moves onto it (playlist lines, plays, My Tags, cues), and the redundant file leaves the lanes for `paths.duplicates` |
| `backfill` | a file in a lane that no row points at | mints a row from what the file says (its tags, then its name) and the length off its header; refuses a file whose track the collection already holds under another file, naming that row |

Neither verb identifies or re-tags anything.

Traktor's collection belongs to a separate project: [`traktor-nml`](https://github.com/alchimestre/traktor-nml)
owns `collection.nml`, its own configuration and its own rules, and reads this project's job state as data to
learn which file replaced which. `import` runs its `plan` and `apply` when it is installed, so a run ends in
both collections; `[traktor] command` in `config.toml` says how to run it, and `--no-traktor` skips it. The
entries `dedupe` leaves behind are still `traktor-nml purge`'s to remove.

The genre vocabulary is not this project's either: it is music-tagger's taxonomy, and `[genres] taxonomy`
points at it. Genres outside it are left blank rather than invented.

## The review queue

```sh
music-agent review
```

Prints the tracks the rule will not settle: each track, its candidates numbered, and why each was left alone.
It is nearly always empty — the pass takes a file whose length cannot be checked rather than asking — and what
is left is a file whose own name names no track. The queue is text, so a caller can put it to a person as a menu
and write the answer back:

```sh
music-agent review --accept 12 --candidate 2   # fetch that one and hand the record to process
music-agent review --skip 12                   # passed over for now
music-agent review --not-found 12              # no usable copy exists; nothing will ask again
music-agent review --list                      # print the queue and stop
```

Those three flags are the only writers of the decision axis (`GLOSSARY.md`).

## Configuration

`config.toml` beside this file — copy `config.example.toml` to start — or
`~/.config/music-agent/config.toml` for an installed copy; `--config` reads another one. It holds the staging
and library directories, the loudness targets and tolerances, the Soulseek pace, the `[tools]` paths, and the
names of the credential environment variables. No secrets.

The two Soulseek listening ports are not in it: which port this machine can accept peers on is a fact about its
network, so `SLSK_LISTEN_PORT` and `SLSK_OBFUSCATED_PORT` live in `.env` with the credentials. Forward the
first on the router — a peer that cannot reach it cannot deliver search results, and every search comes back
empty.

## Driving it from an agent

`skill/SKILL.md` ships with the project: `run` first, then the stage commands behind it, what each stage prints,
and the rare track the queue still asks about. Point a harness at it. A pass frees the two Soulseek ports itself,
stopping a client left on them, so a harness's own Soulseek server does not have to be arranged around.

## Tests

```sh
uv run pytest -q
```

MIT licensed.
