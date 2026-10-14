---
name: music-agent
description: >
  Drive the music-agent CLI: turn a tracklist — a file, a folder of downloads, or a public Spotify
  playlist — into tracks acquired from Soulseek, processed into a rekordbox collection, and imported.
  Covers the commands in the order a run uses them, the review queue and how to put it to a person, and
  the one judgement the pipeline leaves to a caller: a genre its table has no mapping for. Use when a
  tracklist should become files in the collection, when something should end up in rekordbox, and when a
  run's state, its review queue, or a genre proposal is the question.
metadata:
  requires: "the music-agent CLI on PATH, with the credentials from .env loaded into its environment"
---

# The pipeline

These are the commands, in the order a run uses them. Each works on the same **run** without being told
which — only the import closes a run — so they are typed in sequence rather than orchestrated.

`music-agent` reads `config.toml` beside the package, or `~/.config/music-agent/config.toml` for an installed
copy, so it works from any directory. The credentials and the two Soulseek ports come from the environment —
copy `.env.example` to `.env` and fill it in — and every command that reaches the network stops with a
missing key until they are loaded:

```sh
set -a; source .env; set +a
```

```
music-agent acquire --tracks <file|URL>         # the tracklist, searched and downloaded into staging
music-agent status --queue                      # what the run has not settled, by why (see 3)
music-agent review                              # the queue as text; the flags below write a decision
music-agent process                             # identified, genred, converted, normalized, tagged
music-agent retag                               # re-derive the genre on files already tagged (see 6)
music-agent reconcile                           # rebuild the records from the files this run made (see 7)
music-agent status --files                      # what nothing points at, and what points at nothing (see 7)
music-agent import                              # into rekordbox, which has to be closed first
music-agent dedupe                              # one row per track, dry run; --apply merges (see 10)
music-agent backfill                            # a row for every file, dry run; --apply adds (see 10)
music-agent sweep                               # staging's own leftovers, dry run; --apply clears (see 11)
```

The vocabulary — run, track, candidate, Ready, Needs you, reference, proposal, and the decision axis
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

What is worth saying before a run starts: a playlist of well-covered classics will land **in review**, not in
staging. MusicBrainz holds many edits of each — `Dreams` comes back as 21 recordings from 126 s to 301 s — and
the ±1 s rule then refuses to pick a reference. That is the rule working, not the playlist failing; a list of
newer or rarer tracks does better.

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

Searching is paced: one search every `search_interval` seconds in `config.toml`, two by default. The network
answers searches at a rate and says nothing when it stops answering at all, so the pace is kept on this side,
and the child's own log comes out on stderr with an `slsk: ` prefix — including `Search complete: 0 raw
results` for a search that found nothing, which is the difference between a rare track and a network that has
gone quiet.

Fifty searches in a row with nothing back is that second case, and the pass stops rather than spending the
rest of its allowance on silence: it reports those tracks as **not searched** and leaves them exactly as they
were, because nothing should be written about a search that never happened. Run it again when the network is
back.

An empty search has more than one cause, and this measures the one on the network's side before a pass is
spent:

```sh
python skill/probe-search.py      # with .env loaded
```

It asks for one track the network cannot be short of and prints the child's own account of the search. A query
that broad finding nothing is the network, never the tracklist — but a listening port a peer cannot reach also
comes back empty, so check `SLSK_LISTEN_PORT` in `.env` names a port forwarded on the router before blaming
the network. The probe needs the ports to itself, so run it when no pass is running.

**Done when** every track in the run is accounted for: settled, in review, or already held.

## 3. Work the review queue

```
music-agent status --queue
```

It splits what the run has not settled by **why**, and keeping those apart is the point of the split — four
different facts, which printed as one number would state something about the network as if it were something
about the tracks:

| heading | what it means | what it calls for |
|---|---|---|
| *N need a decision* | the length cannot be checked, so nothing but a person can settle it | a person picks one, or skips the track |
| *N not searched* | no pass has reached the track | the pass: `acquire --retry` |
| *N the network offered nothing* | a search answered and held no files | another pass, when the network is answering |
| *N with nothing usable* | files were offered and every one was ruled out | another pass, later |
| *N settled by hand* | the decision axis — nothing will ask about them again | nothing |

A track the rule **can** settle is in none of them: the pass owes that one, not a person.

**The queue asks one question**, and it is worth knowing what it does *not* ask: is a file acceptable when its
length cannot be checked against the reference? Everything else the rule decides alone and never shows — a file
below the 192 kbps floor, a different version, a different artist, a length outside the one-second window. So
the only two reasons a candidate carries are *the peer reports no length* and *no reference length for this
track*. If a track reads *nothing usable* it means every file offered was ruled out, not that nobody answered.

**An upgrade is never a question at all.** When the track is one the collection holds as a lossy copy
(`replaces`), nothing about it reaches the queue: a lossy offer cannot improve on the copy that plays, and an
offer nobody can check buys nothing when the alternative already works. An upgrade in a queue is a bug — say
so rather than working it.

The queue is text, so a caller can put it to a person as a menu: one item per track, its candidates in the
printed order with peer, format and reason, and *skip* / *not found* beside them — and ask about the tracks in
one batch rather than one message each. A peer that reported a bitrate but no length is not in the list: the
length is computed from the file's size and that bitrate, so the rule decides it either way.

The answer is written by the CLI, never by hand, and always against a candidate's number:

```
music-agent review --accept 12 --candidate 2   # fetch that one and hand the record to process
music-agent review --accept 4:1,5:1,9:2        # a batch — IDX:CANDIDATE per track
music-agent review --skip 2,17,28              # passed over for now
music-agent review --not-found 12              # no usable copy exists; nothing will ask again
```

`--accept` is the only one that needs the network: it fetches that file from that peer, and takes a file
already in staging from there rather than fetching it twice. A batch goes through **one** server and as many
transfers as `workers` allows, and reports per track what came through and what was refused — one login, not
one per track, which is the difference between minutes and an hour on a queue of thirty. `--skip` and
`--not-found` are writes and nothing else, and every later pass leaves a decided track alone. Close with a
line per decision: the track, the file, what was written.

Taking a candidate whose length cannot be verified is exactly the call the rule refuses to make unattended, so
it belongs to a person; from then on the record is an ordinary downloaded one and `process` carries on.

```
music-agent acquire --retry
```

is the later pass: it re-searches what failed, preferring peers with free slots, and moves on to candidates it
has not already asked. Reach for it when the queue holds tracks whose candidates all failed.

**Done when** every track is settled, held, or in the queue with a reason a person has seen, and every track
that was answered has been answered through `review`.

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

## 5. Decide the genres the table missed

This is the judgement the pipeline leaves to a caller, and the only one. Where `genres.toml` has no mapping, a
track is tagged with no genre and named in `status --queue` under *waiting on a genre*, with the genres the
collection already uses printed beside it.

For each such track, choose one of those genres, or leave it blank deliberately — a genre outside the
collection's vocabulary is the library owner's decision, never the caller's. Add the mapping to
`genres.toml`, and make the target one of the library's **own top-level families**: the collection's genres are
a typology (the first tier in the Genre field, the lower tiers in My Tag columns), so a target that is not a
family names something the collection no longer holds, and the resolver ignores it rather than trusting it.

```toml
"krautrock" = "Rock & Psych"
```

A name that *five* tracks have carried with nothing to map it to is a **proposal** rather than a decision:
`status --queue` prints each one with the two lines that would accept it. Put the proposal to the library owner
and let them answer; accepting is theirs, and it is the one thing that makes a new genre possible at all.

**Done when** every track named under *waiting on a genre* has a mapping now, or was left blank on purpose.

## 6. Re-tag when the table has moved

A run never replaces a genre: a file already carrying one keeps it, because a genre cannot be undone after
import. So a mapping added above, or a vocabulary that changes under you, reaches only the tracks processed
*after* it — the ones already tagged keep what they carry, and the import turns that into rows.

```
music-agent retag
```

is the deliberate half of that rule, and the only way a genre changes on a file. It re-identifies each track
already placed, resolves the genre again, and writes over whatever the file holds; where nothing resolves, the
old name comes **off**, because a name the collection has not got is a row nothing will absorb. Say what it
did — how many it re-tagged and how many it left blank.

**Done when** the files carry what the table resolves today, and whatever came out blank is named under
*waiting on a genre*.

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

## 8. Import

Rekordbox has to be closed — pyrekordbox refuses to commit while it runs, and the command says so rather than
writing half a run. It copies the collection aside first, commits the whole run once, and closes the run.

```
music-agent import
```

**Done when** the run reports how many it imported, and `music-agent status` shows the run finished.

## 9. Traktor's collection

Not this pipeline's job, and deliberately not its code: Traktor has no database and no API, its collection is
one XML file, and the tool that reconciles it is a separate project. It reads this pipeline's job state as
data — `replaces` → `local_path` is how it learns which file replaced which — so after a run that upgraded
anything, say the one thing there is to say and run it with Traktor closed.

It repoints the entries that describe a file the pipeline replaced, adds what the collection has never seen,
and reports the rest without removing anything. Two things it reports are for a person rather than the tool: a
file whose name carries a byte-order mark has to be renamed, and what the run leaves unanalyzed is what
**Check Consistency → Analyze** in Traktor is for.

**Done when** its report reads `0 repointed · 0 added` for what is on disk, and the analyze pass has been run.

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
of it: run the Traktor tool's purge (see 9).

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

Nothing a record names is in the list — that file is a track a pass still owes — and no lane is touched. The
collection is not read or written.

**Done when** a second `sweep` finds nothing.

## Longer runs

A run of dozens of tracks takes a while and prints a great deal. Hand it to a subagent where the harness has
one, so the pipeline's own output stays out of the conversation, and have it report the counts, the review
queue with its reasons, and any genre proposal back.