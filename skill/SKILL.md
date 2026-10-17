---
name: music-agent
description: >
  Drive the music-agent CLI: turn a tracklist — a file, or a public Spotify playlist — into tracks
  acquired from Soulseek, processed into a rekordbox collection, and imported into Traktor's collection
  as well. Covers `run`, which does all of it in one command, the stage commands behind it, what each
  stage prints, and the rare track the queue still asks about. Use when a tracklist should become files
  in both collections, and when a run's state or its queue is the question.
metadata:
  requires: "the music-agent CLI on PATH, with the credentials from .env loaded into its environment"
---

# The pipeline

One command does all of it:

```sh
set -a; source .env; set +a
music-agent run --tracks <file|URL>
```

It acquires, processes, imports into rekordbox, imports into Traktor, and clears staging — in that order,
printing a line as each stage starts. It asks nothing along the way: what the rules would not settle is
printed once at the end. **This is the whole job for an ordinary tracklist**, and the rest of this file is
what it does.

The stage commands are the same code, one stage at a time, for when a stage needs running on its own — after
a failure, or on a run that was interrupted.

`music-agent` reads `config.toml` beside the package, or `~/.config/music-agent/config.toml` for an installed
copy, so it works from any directory. The credentials and the two Soulseek ports come from the environment —
copy `.env.example` to `.env` and fill it in — and every command that reaches the network stops with a
missing key until they are loaded.

```
music-agent run --tracks <file|URL>             # all of it, in order, one command (see above)
music-agent acquire --tracks <file|URL>         # the tracklist, searched and downloaded into staging
music-agent status --queue                      # what the run has not settled, by why (see 3)
music-agent review                              # the queue as text; the flags below write a decision
music-agent process                             # identified, genred, converted, normalized, tagged
music-agent retag                               # re-derive the genre on files already tagged (see 6)
music-agent reconcile                           # rebuild the records from the files this run made (see 7)
music-agent status --files                      # what nothing points at, and what points at nothing (see 7)
music-agent import                              # rekordbox, then Traktor (see 8)
music-agent dedupe                              # one row per track, dry run; --apply merges (see 10)
music-agent backfill                            # a row for every file, dry run; --apply adds (see 10)
music-agent sweep                               # staging's own leftovers, dry run; --apply clears (see 11)
music-agent tidy                                # every leftover check at once, writing nothing (see 10, 11)
```

Every stage prints where it is: a `── stage · what it is working on` line as it starts, one line per track, and
one summary line at the end. Read those three and the run's position is never in doubt.

The vocabulary — run, track, candidate, Ready, Unchecked length, Needs you, reference, and the decision axis
(accepted / skipped / not found) — is defined in `GLOSSARY.md` beside the package. Read it once; every
command's output is written in it, and a report in those words is the difference between a status and a guess.

## 1. Get the tracklist into a file, or hand `acquire` a URL

One `Artist - Title` per line, or a CSV whose header names both columns. A file, because the CLI owns parsing
it and there is then exactly one reading of the input.

A **public Spotify playlist** goes where the file goes, and needs nothing configured — no account, no app, no
key:

```sh
music-agent acquire --tracks https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U
```

It takes the URL, a `spotify:playlist:` URI, or a file, and the run is named after the playlist instead of
after a file's stem. Spotify writes the master an entry came from into its own title (`Dreams - 2004
Remaster`); the CLI takes those tails off, because MusicBrainz holds those recordings bare and a title that
keeps the tail resolves nothing at all.

There is no separate command for a playlist and no file to write first: the URL is the tracklist, and it is
read once, by the same reader. To read back what a run holds, its own list is `tracks.jsonl` beside its
`events.jsonl`, under `<state root>/<run id>/`.

What is worth saying before a run starts: a playlist of well-covered classics will come back with a **reference
that is hard to pin** — MusicBrainz holds many edits of each, `Dreams` among them as 21 recordings from 126 s
to 301 s — and an offer whose length matches none of them is refused rather than guessed at. That is the rule
working, not the playlist failing; a list of newer or rarer tracks does better.

**Done when** the run holds every track that was asked for, in the order it was asked for.

## 2. Acquire

```
music-agent acquire --tracks <file|URL> --name <what to call the run>
```

Read what it prints. Each track resolves a **reference** from MusicBrainz, searches Soulseek, and either
settles, lands in review, or comes back with nothing usable. Tracks the collection already holds are skipped
there and then, so a run costs nothing for music that is already in the collection.

A pass shows its position while it runs: one line rewritten in place at a terminal, and one line per track as
it settles when the output is captured — which is the form a harness reads back. Each line carries the count,
what the track came to, and its label.

Searching is paced: one search every `search_interval` seconds in `config.toml`, 6.47 by default — 34
searches per 220 seconds, the rate slsk-batchdl ships, with its warning that higher values earn 30-minute bans.

The client is `soulseek-rs daemon`: one process that holds the account and answers JSON-RPC on a Unix socket
in `state_dir` (`~/.local/share/soulseek-rs/state` by default). A pass starts it when nothing answers there
and leaves it running, so the session and any unfinished transfers belong to the daemon and the next pass
picks them up; its log is `daemon.log` beside the socket. `soulseek-rs daemon status` says what it is doing.

Two failures can stop a pass, and each says so in one line. A search that comes back empty is never one of
them: that is the network answering, and once the findable tracks are taken, what is left to search is the
rare ones, whose searches come back empty one after another.

- **Nothing answering.** Four calls in a row that time out instead of returning means the daemon has stopped
  talking. The pass stops, the run stays open, and a fresh pass picks it up.
- **Session displaced.** The daemon reports `session_loss: "displaced"`: another login took the account. Every
  search after that comes back empty, and a fresh daemon loses the same session while another client holds
  the account, so the passes stop there instead of asking again. `"disconnected"` is not this — the daemon
  logs back in on its own.

Tracks a stopped pass never reached are reported as **not searched** and left exactly as they were.

The one port that must be free is `SLSK_LISTEN_PORT` (20000 by default). A Soulseek client left on it by an
earlier run is logged in on the same account, so the server hands it every search reply and the pass sees an
empty network. A pass sweeps it before starting the daemon, stopping that client and saying which; anything
else holding it is named and the pass stops rather than killing a stranger's process.

That is why a harness's own Soulseek MCP server has to be **disabled or unmounted**, not just stopped, while
a pass runs: it is on the same account, the server keeps one session per account, and it is started again the
moment it is killed. With the daemon the symptom is `session_loss: "displaced"`, and `soulseek-rs daemon
status` says so without a log to read.

One pass at a time, and never kill one mid-session. A pass that is killed abruptly — SIGKILL, a closed
terminal — leaves the server holding that session until its own timeout, and every login after it is refused
for twelve to seventeen minutes. The cure is to wait, and it is worth knowing that a brand-new account still
logs in and searches from the same machine while this is happening, which is what makes it easy to mistake
for a network fault. Two passes at once cannot work for the same reason: the later one displaces the first
one's session.

While it is up the daemon shares: `paths.lossless` and `paths.lossy` are handed to it at the start of every
pass, so peers see a client with a library rather than a leecher, and the daemon answers uploads while it
runs. Staging is not one of them, since it holds what is still being worked on.

Peers that advertise a file and then deliver nothing are counted per user, across runs: `maxretry` failures
inside `findtime_days` earn an ignore lasting `bantime_days`, and an ignored peer's files never enter a record.
Only endings that are the peer's count — a refusal, a failure, a connection that never opened — never a queue
it declared, a budget the pass chose to stop at, or bytes it has already sent. A declared queue in the five and
six figures is the exception, because that is what a paid mirror reports to hold every request forever: claiming
one counts as the failure. The pass reports the count at the end of a run:

```
music-agent peers                  # who is on the list, and how much of the ignore is left
music-agent peers --unban USER     # forget a peer, failures and all
```

**Done when** every track in the run is accounted for: settled, in review, or already held.

## 3. Work the review queue

```
music-agent status --queue
```

The queue is nearly always empty, and that is the design: the pass settles a track whenever it can, including
when a length nobody could check leaves one offer standing. **One review pass is the most anyone is asked
for.** What is left in the queue is the case nothing can read: a file whose own name names no track.

It splits what the run has not settled by **why**, and keeping those apart is the point of the split — four
different facts, which printed as one number would state something about the network as if it were something
about the tracks:

| heading | what it means | what it calls for |
|---|---|---|
| *N need a decision* | the file's name says nothing, so nothing but a person can settle it | a person picks one, or skips the track |
| *N not searched* | no pass has reached the track | the pass: `acquire --retry` |
| *N the network offered nothing* | a search answered and held no files | another pass, when the network is answering |
| *N with nothing usable* | files were offered and every one was ruled out | another pass, later |
| *N settled by hand* | the decision axis, plus the tracks a pass closed itself | nothing |

A track the rule **can** settle is in none of them, and a track whose every offer was refused is closed by the
pass that refused them: nothing is left to try, so it is dropped rather than left as a row waiting for somebody
to say so.

**The length decides between offers, never whether to take one.** A reference length that resolved is what
tells two offers apart — the ±1 s window — and it is compared only where there is something to compare it to.
With one offer and no length to check, the file is taken on its name; with several, the ladder decides: lossless
before lossy, then the higher bitrate, then the shorter queue. That is a real trade — an arbitrary tie can pick
the wrong file — and the pass logs which candidate it took and why, so the choice is visible afterwards rather
than silent.

Everything else the rule decides alone and never shows: a file below the 192 kbps floor, a different version, a
different artist, a length outside the window, a preview clip.

**An upgrade is never a question at all.** When the track is one the collection holds as a lossy copy
(`replaces`), nothing about it reaches the queue: a lossy offer cannot improve on the copy that plays. An
upgrade in a queue is a bug — say so rather than working it.

A batch is one login, not one per track:

```
music-agent review --accept 12 --candidate 2   # fetch that one and hand the record to process
music-agent review --accept 4:1,5:1,9:2        # a batch — IDX:CANDIDATE per track
music-agent review --skip 2,17,28              # passed over for now
music-agent review --not-found 12              # no usable copy exists; nothing will ask again
```

`--accept` is the only one that needs the network: it fetches that file from that peer, and takes a file
already in staging from there rather than fetching it twice. A batch goes through **one** server and as many
transfers as `workers` allows, and reports per track what came through and what was refused. `--skip` and
`--not-found` are writes and nothing else, and every later pass leaves a decided track alone.

```
music-agent acquire --retry
```

is the later pass: it re-searches what failed, preferring peers with free slots, and moves on to candidates it
has not already asked. `run` does these later passes itself — keep going while a pass is still bringing files
in, stop on the first one that is not, and stop outright if the account has another client on it — so a run
needs nobody to notice it ended early. `--retry` remains for the stage-by-stage route.

## 4. Process

```
music-agent process
```

Identifies every file from its own sound, resolves a genre, converts lossless audio to AIFF, normalizes
loudness, and writes the tags rekordbox reads. Reported by the collection itself: a file already carrying a
genre keeps it.

A lossy file keeps its own codec, and only two codecs are taken at all — **MP3 and AAC-in-m4a** — because
those are the only two a rekordbox row can point at. A peer's `.ogg`, `.opus` or `.wma` is refused at the
search, so it is never fetched in the first place.

Where the lane already holds the file's name, nothing is overwritten: the download is moved to
`paths.duplicates` and the record settles as one the collection already holds. That is the one case a
duplicate reaches this stage, and it comes from a track whose length was never resolved — the check that
skips what the collection already has cannot answer without one.

**Done when** the run reports its counts and every track it processed is tagged.

## 5. Genres

The genre vocabulary is not this repo's, and deliberately so: it is **music-tagger's**, whose taxonomy names
the collection's families (tier 1) and the names the rest of the world uses for each. `[genres] taxonomy` in
`config.toml` points at that file. With no taxonomy configured, no track gets a genre and one line says so.

A name with no line in the taxonomy is left **blank** — never invented, because a genre cannot be undone after
an import — and `status --queue` lists the tracks that came out ungenred with the names seen on them, most
frequent first. That list is a report, not a question: nothing is asked, and which family a name belongs to is
the library owner's call. The same list is what a missing taxonomy leaves behind, with nothing under it.

The second and third tiers of the typology — the My Tag columns, Smartlists, the classification pass — are
music-tagger's work and not this pipeline's. Reach for that project when the tiers need filling; this stage
only writes the family into the Genre field.

**Done when** every processed track either carries a family or is named in that list.

## 6. Re-tag when the vocabulary has moved

A run never replaces a genre: a file already carrying one keeps it, because a genre cannot be undone after
import. So a line added to music-tagger's taxonomy, or a vocabulary that changes under you, reaches only the
tracks processed *after* it.

```
music-agent retag
```

is the deliberate half of that rule, and the only way a genre changes on a file. It re-identifies each track
already placed, resolves the genre again, and writes over whatever the file holds; where nothing resolves, the
old name comes **off**, because a name the collection has not got is a row nothing will absorb. A track whose
services could not be reached at all is left as it is — one lookup failing is not the taxonomy's verdict, and
the next pass would put the name back. Say what it did — how many it re-tagged, how many it left blank, and
how many it could not ask about.

Retag after an import leaves the collection's rows holding the genre the file had *then*. Reload the tags for
those tracks in rekordbox, or accept the difference.

**Done when** the files carry what the taxonomy resolves today, and whatever came out blank is in the ungenred
list.

## 7. Rebuild the records when they disagree with the files

A status is the run's memory of what happened; the files are what is on disk. When the two part company — a
pass that reset finished tracks, a repair that went too far — nothing else here can recover it, because
`retag` only touches placed records and `process` only takes downloaded ones.

```
music-agent reconcile
```

sets each record back to what its own file says, for the records this run **downloaded**: the run's events are
the provenance, so a track already in the collection is never adopted by name, and a record with no file to
find is named rather than guessed at.

**Done when** every record agrees with its file, or the ones that cannot be found have been said out loud.

Two more ways records and files can disagree are reported rather than repaired:

```
music-agent status --files
```

names three things: the staging files no record of the run points at (a placement refused because the name was
taken, a cancelled transfer's `.part`), the lane files no row and no record points at (a re-download left
beside the copy the collection kept), and the rows whose file has gone. Nothing is repaired — the pipeline
cannot tell which copy of a stem the collection's row is meant to keep, and guessing is the mistake that
report exists to prevent — so say what is there and let the owner say what should go.

## 8. Import — both collections

```
music-agent import
```

Two writes, in order, from one command.

**Rekordbox first.** It has to be closed — pyrekordbox refuses to commit while the app runs, and the command
says so rather than writing half a run. A `rekordbox-mcp` left running by a harness is stopped first, since it
is a second writer on the same file. The collection is copied aside, the whole run commits once, and the run is
closed.

**Traktor's collection second**, through the project that owns it — `[traktor] command` in `config.toml`, or
`traktor-nml` on `PATH`. `traktor-nml` owns `collection.nml`, its own configuration and its own rules; it reads
this run's records as data, where `replaces` → `local_path` is how it learns which file replaced which. The
import runs its `plan`, applies it if it would change anything, and reports both. A machine without that tool
prints one line and nothing else happens — **`--no-traktor`** skips it deliberately.

Neither Traktor step is this pipeline's code, and nothing about Traktor is decided here.

**Done when** rekordbox reports how many it imported, `music-agent status` shows the run finished, and the
Traktor tool's report reads `0 to repoint · 0 to add · 0 to purge`.

## 9. Traktor's own last step

Traktor's collection carries no analysis of its own: the entries this import added have no grid and no
waveform until the app analyzes them. That is the one thing left to a person, and it needs the app:

1. Open Traktor.
2. Run **Check Consistency → Analyze**.

Two things its report can say that are for a person rather than the tool: a file whose name carries a
byte-order mark has to be renamed, and a track with no file was left for `traktor-nml purge`.

**Done when** the analysis pass has run for the tracks the import added.

## 10. One row per track, and a row for every file

Neither is a run's step: `import` is scoped to one run's records, and these two are what keeps the collection
equal to the lanes anyway. Rekordbox has to be closed for both.

```
music-agent dedupe             # two rows that are one track — what it would merge, writing nothing
music-agent dedupe --apply     # one backup, one merge
music-agent backfill           # lane files the collection has no row for — what it would add
music-agent backfill --apply   # one backup, one commit
```

**`dedupe`** is for after something was imported by hand, which is how a second row appears beside a track the
collection already held — usually a lossy copy the pipeline had already upgraded. The row holding the better
copy survives — **lossless beats lossy**, then the row more of the library leans on (plays, playlist lines, My
Tags, cues), then the measured one, then the older — and everything the other still held moves onto it: a
playlist line or a My Tag is dropped only where the survivor already has one in the same place. The redundant
**file** leaves the lanes for `paths.duplicates` rather than being deleted.

**`backfill`** is the other direction: a file in a lane with no row gets one, minted from what the file itself
says (its tags, falling back to its name — never invented), and a file whose track the collection already holds
under another file is refused with that row named, because that one is a duplicate for `dedupe` instead.

What `dedupe` leaves behind in Traktor is a dead entry for the file that left the lanes, so say the second half
of it: run `traktor-nml purge` (the tool this pipeline calls in section 8).

**Done when** a second `dedupe` and a second `backfill` both find nothing, and `music-agent status --files`
says nothing was left behind.

## 11. Staging's own leftovers

```
music-agent sweep              # what it would clear
music-agent sweep --apply      # and this clears it
```

A pass that is interrupted, or refused a placement, leaves a shell behind: an interrupted transfer's `.part`,
which is incomplete by definition, and a converted file no record points at any more. Neither is anything a
later pass will pick up, and both read as work in progress to whoever opens the folder.

`run` clears the interrupted transfers itself and lists the rest rather than deleting them, because a loose
file could still be wanted. Nothing a record names is in the list — that file is a track a pass still owes —
and no lane is touched. The collection is not read or written.

**Done when** a second `sweep` finds nothing.

## Longer runs

A run of dozens of tracks prints a great deal, and `run` is one command from start to finish, so a session
does not have to sit through it. Hand it to a subagent where the harness has one, and have it report back the
stage counts, the review queue with its reasons, and the ungenred list — three things, and nothing that needs
a conversation.

Stop a tool you started. A harness's Soulseek MCP server holds the ports this pipeline needs, and a
`rekordbox-mcp` holds the database the import writes; both are restarted by the harness when it wants them, so
stopping them needs nobody's permission.