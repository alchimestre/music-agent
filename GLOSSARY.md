# Glossary

The vocabulary this project uses. Two of these axes look alike and mean different things, which is
why they are written down.

## Candidate label

What a candidate file *is*, if left alone. It describes the system's proposal for that file. It is not
a state, and a label is never something the track has already become.

- **Ready** — agrees with the reference and would be taken without asking. A track with any Ready
  candidate is settled by the system, choosing between several by a fixed ladder, so it never reaches
  review.
- **Needs you** — the length cannot be verified against the reference: either the peer reported none, or
  the track has no reference length at all. Nothing else sends a candidate here, and only for a track the
  collection does not already hold: see **Upgrade**.
- **Rejected** — ruled out: the file is not a format we take, it is a preview, its length is outside the
  window, its quality is below the floor, it is not the version or the track that was asked for, or — on an
  upgrade — it is lossy or unverifiable. **A rejected candidate is never shown** — it has no place in a list
  a person has to read.

## Decision

What the user has decided about a *track*. Independent of any candidate label, and the only axis that
records something already done.

- **undecided** (`·`) — nothing decided yet.
- **accepted** (`✓`) — settled: the candidate the user picked was fetched, and the record is an ordinary
  downloaded one from then on.
- **skipped** (`–`) — passed over for now.
- **not found** (`✗`) — no usable copy exists.

`music-agent review` is the only thing that writes this axis — its flags are the whole interface, a menu in the
harness being nothing but a way to arrive at them — and every
pass reads it: a track the user has decided is left alone rather than searched again, because that is what
deciding it means.

## Review queue

The list of tracks that need a decision. A track belongs here only when the system could not settle it
alone — that is, when **no candidate is Ready**. When several are Ready the system picks one by its own
ladder and logs the choice rather than asking, so a tie never appears here.

A track the system cannot offer a candidate for at all is not in the queue either, and the three ways that
happens are kept apart rather than counted together, because they are three different facts about three
different things:

- **not searched** — no pass has reached the track, so nothing is known about it.
- **the network offered nothing** — a search answered and held no files.
- **nothing usable** — files were offered and every one was ruled out.

So the queue asks exactly one question: **is a file acceptable when its length cannot be checked?** Everything
else the rule settles on its own — a file below the floor, a different version, a different artist, a length
outside the window are all ruled out and never shown. A length is the only evidence that cannot be read off
the candidate, which is why it is the only thing a person is asked about.

And it asks it only about a track the collection does not already hold. An **upgrade** is optional, so nothing
about one is ever put to the user: see that entry.

## Reference

The length a candidate is checked against, taken from the release the requested track belongs to. A
candidate agrees with the reference when it is within one second of it; there is no wider band, and a
file outside that window is a different file rather than a near miss.

## Mix

The version of a track named in parentheses — Original, Extended, or a remixer's name. Two mixes of one
title are two different tracks, not two copies of one track.

One version written two ways is still one version. The comparison folds case and punctuation, so
`(original_mix)`, `(Original Mix)` and `(ORIGINAL MIX)` are the same, and a bare title is the same version
as `(Original Mix)` and `(Original Version)` — but only those: every other suffix, including a specific
editor's `(Ajna Samm Edit)` against a request that says only `(Edit)`, is its own version and goes to review.

What a version *is* and what a request *takes* are two questions, and the second is the wider one. A request
that names a version takes only that version. A request that names none — or names the original — takes any
version that is not somebody else's remix, and the ±1 s length window is what keeps that honest. So a file
called `(Extended Mix)` whose length matches the reference is taken for a bare request, while `(Victor Ruiz
Remix)` is not taken for it however well the length matches.

## Pass

One run of a stage over every track it still owes. The **first pass** searches with no peer filters at
all, because a filter can remove the only peer holding a rare track. A **later pass** — what `--retry`
starts — is the one allowed to restrict itself to peers with free slots, and it moves on to candidates it
has not asked before rather than repeating a question that was already answered. A track that fails
across two passes is not retried again.

A pass also stops early when the **network goes quiet**, and the distinction it rests on is the useful part: a
search that comes back with nothing means a rare track, but fifty in a row means the network has stopped
answering. The pass reports the tracks it never reached as **not searched** and leaves them as they were,
rather than searching them into the same silence and calling the result a fact about the tracks.

## Upgrade

A track the collection already holds **as a lossy copy**, with a run looking for a lossless one. It makes the
request *optional*: the track already plays, so an offer is only interesting if it improves on the copy held
and can be checked against it. A lossy offer is never an upgrade — a second lossy copy is no better than the
first — and an offer whose length cannot be verified buys nothing worth a question, because the alternative is
a copy that already works. Both are ruled out rather than shown. An upgrade is `Record.replaces`.

## Duplicate

Two rows in the collection for one track: the same folded artist, title and mix, with lengths inside the
pipeline's one-second window (the identity, so two mixes of one title are never one track). A hand
import leaves them, usually beside the lossy copy the pipeline had already upgraded. `music-agent dedupe`
merges them — the row holding the better copy survives, what the other held moves onto it, and the redundant
file leaves the lanes rather than being deleted.

## Backfill

A row minted for a file in a lane that the collection has no row for — the other direction from a duplicate.
`import` is scoped to one run's records, so a file that arrives outside a run never gets one; `music-agent
backfill` fills that gap from what the file itself says (its tags, then its name) and refuses a file whose
track the collection already holds under another file, because that one is a duplicate and `dedupe` settles it.

## Repoint

Pointing an entry that describes a file a run replaced at the file it made instead, rather than removing the
entry and adding a new one. The track keeps everything that belongs to the *track* — cues, play count, last
played, playlists, artwork — and gives up whatever was measured from the audio that is gone: in rekordbox the
file-derived columns (`importing.file_columns`), in Traktor the tempo, loudness, key, auto grid and fingerprint
(`~/projects/traktor-nml`). Removal would have cost the playlist memberships (the design notes), and a stale number
left behind is what the user caught in the app (the design notes).

## Leftover

A file or a row that nothing points at. Three kinds, all reported by `music-agent status --files` and none of
them repaired automatically: a file in staging no record of the run claims (a placement the lane refused, a
cancelled transfer's `.part`), a file in a lane no row and no record claims (a re-download left beside the
copy the collection kept), and a row whose file has gone. Which copy of a stem a row means to keep is not
something the pipeline can tell, so it says what is there and leaves the choice.
