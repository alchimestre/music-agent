# music-agent

A command line tool that turns a tracklist into tracks in a rekordbox collection. It finds the files on
Soulseek, keeps lossless ones as AIFF, sets the loudness to −14 LUFS / −1 dBTP, writes the tags from
MusicBrainz and AcoustID, and imports the collection. Every step is written down in `.state/jobs/<run>/`,
so a run can be read back, resumed, or repeated with no model in the loop.

## Run it

The command is installed on this machine:

```sh
music-agent --help
```

If that is not found, use one of these:

```sh
uv run music-agent --help          # from this directory
.venv/bin/music-agent --help       # the project's own environment
uv tool install --editable .       # install the command again
```

Most commands talk to Soulseek, MusicBrainz or AcoustID, so load the credentials first. `config.toml`
holds only the environment variable *names*; the values live in `.env`:

```sh
set -a; source .env; set +a
```

## The commands

Commands in the order a run uses them. Each one works on the same run, so they are typed in sequence.

```
music-agent acquire --tracks <file|URL>         # search and download into staging
music-agent status --queue                      # what the run has not settled, and why
music-agent review                              # the queue as text, for the caller to put to the user
music-agent process                             # identify, genre, convert, normalize, tag
music-agent retag                               # re-derive the genre on files already tagged
music-agent reconcile                           # rebuild the records from the files this run made
music-agent status --files                      # files nothing points at, and rows whose file is gone
music-agent import                              # into rekordbox, which must be closed first
music-agent dedupe                              # one row per track; dry run, --apply merges
music-agent backfill                            # a row for every file that has none; dry run, --apply adds
```

`acquire` and `process` take hours on a long tracklist. `review`, `status` and `import` take seconds.

**Traktor's collection is not this project's business.** It has no database and no API — its collection is one
XML file — and it is reconciled by `~/projects/traktor-nml`, which reads this project's job state as data to
learn which file replaced which. `traktor-nml apply` after an acquisition, with Traktor closed.

**One row per track, and a row for every file: `dedupe` and `backfill`.** The collection should hold exactly
what the lanes do, and `import` — scoped to one run's records — is not what keeps it that way; these two are.

`music-agent dedupe` merges two rows that are one track. A hand import leaves them, usually beside the lossy
copy the pipeline later upgraded. The row holding the better copy survives (lossless beats lossy; then the one
the library has used; then the measured one; then the older), everything the other still held moves onto it
(playlist lines, plays, My Tags, cues), and the redundant file leaves the lanes for `paths.duplicates` rather
than being deleted — what it leaves behind in Traktor is a dead entry, and `traktor-nml purge` takes that out.

`music-agent backfill` is the other direction: a file in a lane the collection has no row for gets one, minted
from what the file itself says (its tags, falling back to its name), and a file whose *track* the collection
already holds under another file is refused with the row it is held by named — that is a duplicate, so `dedupe`
settles it instead. Neither verb identifies or re-tags anything: a file whose tags are unreadable keeps
them. Both are dry runs until `--apply`.

## The queue

```sh
music-agent review
```

The tracks the rule will not settle, as text: each track, its candidates numbered, and the reason each was
left alone. There is no screen — the caller puts the queue to a person as a menu, one question per track,
the candidates as the options with *skip* and *not found* beside them — and the answer comes back here:

```sh
music-agent review --accept 12 --candidate 2   # fetch that one and hand the record to process
music-agent review --skip 12                   # passed over for now
music-agent review --not-found 12              # no usable copy exists; nothing will ask again
```

Those three flags are the only writers of the decision axis (`GLOSSARY.md`), which is what keeps a menu, a
script and the working procedure from drifting apart. `--list` prints the queue and stops.

## The agent glue

Two files let an omp session drive the pipeline instead of you:

- `skill/SKILL.md` — the working procedure: the commands in order, the queue as a table, the genre call, and
  what each output means. Symlinked to `the skill directory`.
- `agent/music-agent.md` — the subagent for a run of dozens of tracks. Symlinked to
  `the subagent file`.

The vocabulary those two share is in `GLOSSARY.md`. The decisions behind the code, one file per question,
are in `the design notes/` — open `map.md` first.

Status-file generation reads the records and events once per call, then derives counts and queue buckets
from those snapshots. Nothing is cached between calls; later commands read the current files.

## Tests

```sh
.venv/bin/python -m pytest -q
```
