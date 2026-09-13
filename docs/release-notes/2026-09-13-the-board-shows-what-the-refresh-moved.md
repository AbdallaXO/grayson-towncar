---
date: 2026-09-13
audience: Dispatchers
title: The schedule board shows what a flight refresh moved, and what it broke
---

# The schedule board shows what a flight refresh moved, and what it broke

## Send this to the team

> Hey team — after you refresh and match flights, the schedule board now
> shows what moved and which turns it broke, so you don't have to read every
> row.
>
> 1. A red strip at the top says how many turns broke and how many pickups
>    moved.
> 2. Any driver whose day changed gets a mark beside their name. A moved job
>    has a gold ring, a dashed outline where it used to sit, and a line to
>    where it is now. A turn that no longer works gets a red bracket
>    underneath with the minutes short.
> 3. Use the arrows on the strip to jump between broken turns, or click
>    **Highlight changes** to fade everything that didn't move.
>
> Hover a moved job to see its original time. Moves under ten minutes aren't
> drawn unless they broke a turn, and it all clears itself about twelve hours
> after the refresh.
>
> Nothing else changed. Same trips, same drag-and-drop, same red badges for
> conflicts that were already there. It never moves a job for you.

---

## Behind the scenes

**Where it lives:** The schedule board, in-house view only. The affiliate board
doesn't get it — operators accept their own jobs.

**Why:** Matching a flight moves the pickup and stops there. Nothing re-checked
the driver's turnarounds until the next half-hourly sweep, so between clicking
Match and that sweep the only thing that knew a 20-minute move had wrecked a
turn was a dispatcher scanning the board line by line. Now the board runs each
driver's day as it stands and as it stood before the move, and marks the turns
that got worse.

**Expect to be asked:**
- *"I fixed the turn but the bracket is still there."* — Reload the board. The
  bracket stays until the turn actually works again; acknowledging the time
  change on its own doesn't clear it, because that only says "I've seen it moved".
- *"I closed the strip and it came back."* — A later refresh that moved something
  brings it back. Closing it only hides that one refresh.
- *"Why isn't every conflict marked?"* — Only the ones the new times created.
  Conflicts that were already there keep their usual red badge and nothing else.
- *"It says a job moved by three minutes — why?"* — Small moves are hidden unless
  they broke a turn. If a three-minute move is drawn, it's because it did.
- *"Highlight changes hid a driver."* — It fades, it never hides. Every row is
  still there; click **All rows** to bring them back to full strength.
