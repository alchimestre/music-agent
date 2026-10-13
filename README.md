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
music-agent acquire --tracks <file>             # search and download into staging
music-agent status --queue                      # what the run has not settled, and why
music-agent review                              # the queue on screen, to settle tracks by hand
music-agent process                             # identify, genre, convert, normalize, tag
music-agent retag                               # re-derive the genre on files already tagged
music-agent reconcile                           # rebuild the records from the files this run made
music-agent status --files                      # files nothing points at, and rows whose file is gone
music-agent import                              # into rekordbox, which must be closed first
```

`acquire` and `process` take hours on a long tracklist. `review`, `status` and `import` take seconds.

## The screen

```sh
music-agent review
```

On a terminal this opens the review queue: the tracks that need a decision on the left, the candidates for
the selected track as cards on the right. The card holds the file, its peer, its length against the
reference, and the reason the system will not take it alone.

```
a  accept      s  skip      f  not found      n / p  next / previous candidate      q  quit
```

The same decisions are available as flags, which is how an agent drives them:

```sh
music-agent review --accept 12 --candidate 2
music-agent review --skip 12
music-agent review --not-found 12
```

With no terminal, `music-agent review` prints the queue instead of drawing it.

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
