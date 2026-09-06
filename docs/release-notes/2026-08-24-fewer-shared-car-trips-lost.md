---
date: 2026-08-24
audience: Dispatchers
title: Shared-car trips no longer get sent out unnecessarily
---

# Shared-car trips no longer get sent out unnecessarily

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
  No file names, no field names, no jargon. Say what to click, what is
  different, and what did NOT change.
-->

## Send this to the team

> Hey team — when two drivers are sharing one car, the system was being
> overly cautious about the handoff and sending some trips to outside
> drivers that would have run just fine in-house.
>
> That's fixed. The system now checks shared-car handoffs the same way it
> was already checking them for the second-shift suggestions, so fewer of
> these trips get needlessly farmed out.
>
> Nothing else about shared cars changed — the wash/fuel/handoff timing
> shown on the board is exactly the same as before, and nothing here
> touches how trips get assigned otherwise.

---

## Behind the scenes

**Where it lives:** the build engine's shared-car check — invisible to the
board, shows up as more trips staying in-house on shared-car days.

**Why:** the engine measured its buffer differently than the rest of the
system did, and the difference was strict enough that it was farming out
real handoffs the founder confirmed ran fine — one with as little as 48
minutes between one driver finishing and the next picking up.

**Expect to be asked:**
- "Did the shared-car spacing change?" — No, that number (Share Pad, in
  Tuning) is untouched. This is a separate dial (Engine Share Pad) that
  only the build engine reads, and it starts at 65 minutes — adjustable
  the same way.
