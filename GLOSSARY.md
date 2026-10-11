# Glossary

The vocabulary this project uses. Two of these axes look alike and mean different things, which is
why they are written down.

## Candidate label

What a candidate file *is*, if left alone. It describes the system's proposal for that file. It is not
a state, and a label is never something the track has already become.

- **Ready** — agrees with the reference and would be taken without asking. A track with any Ready
  candidate is settled by the system, choosing between several by a fixed ladder, so it never reaches
  review.
- **Needs you** — plausible, but something prevents taking it unattended: the length cannot be
  verified, or the mix is not the one asked for.
- **Rejected** — ruled out: the length is outside the window, the quality is below the floor, or the
  file is a preview. **A rejected candidate is never shown** — the screen has no place for it.

## Decision

What the user has decided about a *track*. Independent of any candidate label, and the only axis that
records something already done.

- **undecided** (`·`) — nothing decided yet.
- **accepted** (`✓`) — settled.
- **skipped** (`–`) — passed over for now.
- **not found** (`✗`) — no usable copy exists.

## Review queue

The list of tracks that need a decision. A track belongs here only when the system could not settle it
alone — that is, when **no candidate is Ready**. When several are Ready the system picks one by its own
ladder and logs the choice rather than asking, so a tie never appears here. A track whose every
candidate is rejected does not appear either; it is reported as having nothing usable.

## Reference

The length a candidate is checked against, taken from the release the requested track belongs to. A
candidate agrees with the reference when it is within one second of it; there is no wider band, and a
file outside that window is a different file rather than a near miss.

## Mix

The version of a track named in parentheses — Original, Extended, or a remixer's name. Two mixes of one
title are two different tracks, not two copies of one track.

## Pass

One run of a stage over every track it still owes. The **first pass** searches with no peer filters at
all, because a filter can remove the only peer holding a rare track. A **later pass** — what `--retry`
starts — is the one allowed to restrict itself to peers with free slots, and it moves on to candidates it
has not asked before rather than repeating a question that was already answered. A track that fails
across two passes is not retried again.
