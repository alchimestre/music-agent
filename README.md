# music-agent

Turns a tracklist into tracks in a rekordbox collection: finds the files on Soulseek, keeps lossless ones as
AIFF, normalizes loudness to −14 LUFS / −1 dBTP, writes tags from MusicBrainz and AcoustID, and imports the
result. Every step is written under `.state/jobs/<run>/`, so a run can be read back, resumed, or repeated.

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

In the order a run uses them. Each works on the same run.

```
music-agent acquire --tracks <file|URL>         # the tracklist, searched and downloaded into staging
music-agent status --queue                      # what the run has not settled, and why
music-agent review                              # the queue as text
music-agent process                             # identify, genre, convert, normalize, tag
music-agent retag                               # re-derive the genre on files already tagged
music-agent reconcile                           # rebuild the records from the files this run made
music-agent status --files                      # files nothing points at, and rows whose file is gone
music-agent import                              # into rekordbox, which must be closed first
music-agent dedupe                              # one row per track; dry run, --apply merges
music-agent backfill                            # a row for every file that has none; dry run, --apply adds
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

Traktor's collection is a separate project: [`traktor-nml`](https://github.com/alchimestre/traktor-nml) reads
this project's job state as data to learn which file replaced which. Run `traktor-nml apply` after an
acquisition, with Traktor closed, and `traktor-nml purge` for the entries `dedupe` leaves behind.

## The review queue

```sh
music-agent review
```

Prints the tracks the rule will not settle: each track, its candidates numbered, and why each was left alone.
The queue is text, so a caller can put it to a person as a menu and write the answer back:

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

`skill/SKILL.md` ships with the project: the commands in the order a run uses them, the review queue and how to
put it to a person, and the one judgement the pipeline leaves to a caller. Point a harness at it.

## Tests

```sh
uv run pytest -q
```

MIT licensed.
