# Glossary

The vocabulary this project uses. Two of these axes look alike and mean different things.

## Candidate label

What a candidate file *is*, if left alone: the system's proposal for that file. Not a state, and never
something the track has already become.

| label | what it means |
|---|---|
| Ready | agrees with the reference, and would be taken without asking. A track with any Ready candidate is settled by the system, which picks between several by a fixed ladder, so it never reaches review |
| Unchecked length | nothing could check the length — the peer reported none, or the track has no reference length at all. Also taken rather than asked about: one offer stands on its own name, and several go to the ladder like any other. Only for a track the collection does not already hold (see Upgrade) |
| Needs you | what is left for a person: the file's own name names no track, so nothing can be read off it at all |
| Rejected | ruled out: not a format this project takes, a preview, a length outside the window, quality below the floor, not the version or the track that was asked for; on an upgrade, lossy or unverifiable. A rejected candidate is never shown |

## Decision

What a person has decided about a *track*. Independent of any candidate label, and the only axis that records
something already done.

| state | glyph | what it means |
|---|---|---|
| undecided | `·` | nothing decided yet |
| accepted | `✓` | settled: the candidate that was picked has been fetched, and the record is an ordinary downloaded one from then on |
| skipped | `–` | passed over for now |
| not found | `✗` | no usable copy exists |

`music-agent review` is the only writer of this axis. Every pass reads it, and a track that has been decided is
not searched again.

## Review queue

The tracks that need a decision: those with a candidate labelled *Needs you*, which is a file whose own name
names no track. Everything else the system settles itself.

A track the system cannot offer a candidate for at all is not in the queue either. The three ways that happens
are kept apart, because they are three facts about three different things:

| report | what happened |
|---|---|
| not searched | no pass has reached the track, so nothing is known about it |
| the network offered nothing | a search answered and held no files |
| nothing usable | files were offered and every one was ruled out |

A track whose every offer was refused is closed by the pass that refused them — *settled by hand* in the queue's
own words, with a note saying so — because there is nothing left to try.

The length decides between offers, never whether to take one. Where it cannot check anything, one offer is taken
on its name and several go to the ladder (lossless, then bitrate, then queue depth). That is a real trade: an
arbitrary tie can take the wrong file, and the pass logs which it took.

Nothing about an upgrade is ever put to a person: an upgrade is optional, so a lossy or uncheckable offer buys
nothing against a copy that already plays.

## Reference

The length a candidate is checked against, taken from the release the requested track belongs to. A candidate
agrees with the reference when it is within one second of it; there is no wider band.

## Mix

The version of a track named in parentheses: Original, Extended, or a remixer's name. Two mixes of one title are
two different tracks.

The comparison folds case and punctuation, so `(original_mix)`, `(Original Mix)` and `(ORIGINAL MIX)` are one
version, and a bare title is the same version as `(Original Mix)` and `(Original Version)`. Every other suffix
is its own version, including a specific editor's `(Ajna Samm Edit)` against a request that says only `(Edit)`.

What a version is and what a request takes are two questions. A request that names a version takes only that
version; a request that names none, or the original, takes any version that is not somebody else's remix, with
the ±1 s window as the check. So `(Extended Mix)` is taken for a bare request when its length matches, and
`(Victor Ruiz Remix)` is not taken however well the length matches.

## Pass

One run of a stage over every track it still owes. A first pass searches with no peer filters at all, because a
filter can remove the only peer holding a rare track. A later pass — what `--retry` starts — may restrict
itself to peers with free slots, and asks candidates it has not asked before. A track that fails across two
passes is not retried again.

A pass stops early only when the child it searches through fails: searches that never answer, or a child that
says it has no session. A search that comes back empty never stops it — once the findable tracks are taken,
the ones still to search are the rare ones, and their searches answer nothing one after another. The tracks a
stopped pass never reached are reported as not searched and left as they were.

## Upgrade

A track the collection already holds as a lossy copy, with a run looking for a lossless one. The request is
optional — the track already plays — so an offer is only interesting if it improves on the copy held and can be
checked against it. A lossy offer is never an upgrade, and an offer whose length cannot be verified buys
nothing, because the alternative already works. An upgrade is `Record.replaces`.

## Duplicate

Two rows in the collection for one track: the same folded artist, title and mix, with lengths inside the
pipeline's one-second window (the identity `dedupe` uses). A hand import leaves them, usually beside the lossy
copy the pipeline has upgraded. `music-agent dedupe` merges them.

## Backfill

A row minted for a file in a lane that the collection has no row for — the other direction from a duplicate.
`import` is scoped to one run's records, so a file that arrives outside a run never gets one. `music-agent
backfill` fills that gap from what the file says (its tags, then its name), and refuses a file whose track the
collection holds under another file, because that one is a duplicate.

## Repoint

Pointing an entry that describes a file a run replaced at the file it made instead, rather than removing the
entry and adding a new one. The track keeps everything that belongs to the track — cues, play count, last
played, playlists, artwork — and gives up what was measured from the audio that is gone: in rekordbox the
file-derived columns (`importing.file_columns`), in Traktor the tempo, loudness, key, auto grid and fingerprint
(`traktor-nml`). Removal would hard-delete the playlist memberships that hang off the row.

## Export target

One collection a run writes to, named in `[export] targets` and registered in `exporting.REGISTRY`. A target
says whether it is installed (`available`), whether its rows can be read back (`reads`), and how one run's
tracks go into it (`import_run`); `rekordbox` and `traktor` ship. A target that is not installed is reported
and skipped, and the run is finished only when every target that ran succeeded.

## Lane

One of the two directories the collection is kept in: `paths.lossless` and `paths.lossy`. A file's extension
decides which lane it belongs to, and the import reads the file rather than the row. Staging is not a lane.

## Leftover

A file or a row that nothing points at. Three kinds, reported by `music-agent status --files` and never
repaired automatically: a file in staging no record claims (a placement the lane refused, a cancelled
transfer's `.part`), a file in a lane no row and no record claims (a re-download left beside the copy the
collection kept), and a row whose file has gone. Which copy of a stem a row means to keep is not something the
pipeline can tell.

## Peer offence and the ignore list

| term | what it means |
|---|---|
| offence | a download that ended without a byte while the ending was the peer's: it refused, it failed, or the connection never opened. A queue within reason that the peer declared, a budget the pass chose to stop at and a session the child lost are the pass's own decisions and are never counted against a peer. A queue in the five and six figures is the opposite: paid mirrors and promo pools declare those to hold every request forever, so claiming one is evidence rather than an excuse |
| ignore list | the peers this account will not ask again. `maxretry` offences inside `findtime_days`, counted across runs, earn an ignore lasting `bantime_days`; a delivery clears the record. Written at `paths.peers`, read by every pass, listed by `music-agent peers`, lifted by `music-agent peers --unban USER` |

An ignored peer's files are dropped where the search results are read, so no record, ladder or queue ever sees
them. The list belongs to the account rather than to a run: a pass reads it when it starts and writes it as
peers fail.
