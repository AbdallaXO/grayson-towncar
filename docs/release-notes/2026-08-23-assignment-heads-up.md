---
date: 2026-08-23
audience: Dispatchers
title: The board flags risky assignments the moment you make them
---

# The board flags risky assignments the moment you make them

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
-->

## Send this to the team

> Hey team — when you assign a trip by hand on the schedule board or the
> planner (drag it onto a driver, or hit Assign on a suggestion), the system
> now double-checks it. If that driver genuinely can't make his next pickup,
> a small note appears in the corner.
>
> It only speaks up for a real clash. A turn that's merely tight won't
> interrupt you — you'll still see it amber on the board, and if a note comes
> up for a real clash it'll be mentioned in there too.
>
> Nothing is blocked — every assignment goes through exactly as before. The
> note is a heads-up; if the day is planned that way on purpose, dismiss it and
> move on.
>
> One more thing: when two drivers share a car, the scheduler now leaves about
> two hours between their jobs (it used to allow one) — time for the wash,
> fuel, and the swap at base.
>
> Nothing changed for drivers, and nothing assigns itself.

---

## Behind the scenes

**Where it lives:** the schedule board and the planner — the note pops up in the
corner right after an assignment lands. The two new dials (the shared-car
spacing, and the warnings on/off switch) are in Planner → Tuning, under
Timing & Buffer.

**Why:** hand assignments had no check at all — an impossible back-to-back went
in silently. And the old one-hour shared-car spacing was tighter than nine out
of ten real car swaps actually run.

**Expect to be asked:**
- "It warned me but I meant it." — Fine. It never blocks; dismiss it.
- "Can we turn it off, or change the spacing?" — Yes, both, in Planner →
  Tuning: Assign Warnings 0 turns the notes off, Share Pad sets the spacing in
  minutes.
- "Is it accurate?" — 9 in 10. That is why only the real clash interrupts:
  measured over 28 days of real boards, "he can't make it" was right 269 times
  out of 298. The tight-turn note was right 235 out of 341 — about 2 in 3 — so
  it no longer raises a note on its own. One interruption in three being wrong
  is how a team learns to ignore the other two.
- "So I'll miss tight turns now?" — No. They still show amber on the board, and
  they are still listed inside a note that a real clash brought up. They just
  stop being a reason to interrupt you on their own.
