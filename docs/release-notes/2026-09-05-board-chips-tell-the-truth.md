---
date: 2026-09-05
audience: Dispatchers
title: The board's turn colours now match what Auto-Assign already knows
---

# The board's turn colours now match what Auto-Assign already knows

## Send this to the team

> Hey team — a few turns a day that showed green or amber on the board are going
> to show red from now on. That's not new trouble, it's trouble you were already
> having and couldn't see.
>
> When Auto-Assign stopped booking pickups drivers couldn't reach, the board
> didn't get the same update. So it kept using the earlier, optimistic clearing
> time. Across the last four weeks that meant about **five turns a day** where the
> chip said the driver was fine on a return he was physically short for — the
> worst one was green on a turn he was **47 minutes** short.
>
> Expect roughly five more red turns a day, and more red when you assign someone
> by hand. Those are the ones worth a second look.
>
> Airport pickups are unchanged — a few minutes past a landing time was never a
> problem.
>
> Nothing moved on its own. Same drivers, same trips, same driver app.

---

## Behind the scenes

**Where it lives:** the turn colours between two trips on the schedule board, the
warning when you assign a driver by hand, and the Recovery Advisor.

**Why:** on 2026-09-02 Auto-Assign started taking the later of the two clearing
times whenever the next job is fixed-time — a departure, a return, a cruise. The
board's own turn formula never got that rule, so for three days the two disagreed
and the board was the optimistic one.

**Measured across 28 real boards before the change:** 36.7 fixed-time turns a day
where the rule applies, 9.3 turns a day changing colour, and **5.1 a day shown as
clean or tight that the engine calls impossible**. All 142 of them are on file
with driver and times — the worst were sereen on 25 July (green chip, 47 minutes
short) and Michael Olmo on 31 July (amber chip, 51 minutes short).

**What the hand-assign warning does now:** it fires red about twice as often over
the same four weeks (156 → 298), and it is right 90% of the time when it does,
against 94% before. It catches 77 more genuine conflicts than it used to.

**Expect to be asked:**
- "Why is the board suddenly redder?" It isn't. The trips were always this tight;
  the board was measuring from a clearing time the driver never actually hit.
- "Do I have to fix all of them?" No. It's a colour, not an instruction. Nothing
  reassigns itself and nothing is blocked.
- "Why are airport pickups still green?" Their booked time is the landing time,
  and the guest is still collecting bags. That exemption is deliberate and
  unchanged.
