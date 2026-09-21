---
date: 2026-09-21
audience: Dispatchers
title: The schedule board shows cancelled jobs as amber ghosts, and keeps itself up to date
---

# The schedule board shows cancelled jobs as amber ghosts, and keeps itself up to date

## Send this to the team

> Hey team — two changes on the schedule board.
>
> 1. A cancelled job no longer disappears, and no longer sits there looking
>    live. It shows as a thin amber chip with the time crossed out and an ✕,
>    in a little lane under the driver it was on (or under Unassigned). Hover
>    it to see when it was cancelled and by whom. You can't drag it, and it
>    doesn't count toward the driver's jobs or the totals at the top. The row
>    header says "· 1 cancelled" so you can spot it without scrolling.
> 2. The board now checks every 45 seconds whether anything on that day
>    changed — a cancellation, a reassignment, a moved pickup — and refreshes
>    itself when something did. It waits if you are mid-drag, have a dialog
>    open or are typing, and your scroll position stays where it was.
>
> What did not change: how you assign, drag, hold or release a day, and what
> drivers see.

---

## Behind the scenes

**Where it lives:** the schedule board, both the in-house and affiliate views.

**Why:** the 4:42 PM job on 21 September was cancelled and still showed as a
normal chip. Two causes. The board dropped cancelled legs from its query, so a
reload made the job vanish with nothing to say it had been cancelled rather
than moved; and the page only ever refreshed its clock and the advisor banner,
never the chips, so until a reload the dead job sat there as if real.

**Mechanics:** cancelled legs for the day (leg cancelled, or the whole
reservation cancelled) are fetched separately and rendered in a 20px ghost
lane under the row's real lanes, so they never collide with live chips and the
lane-packing is untouched. The refresh polls `schedule-board/version/`, a
fingerprint of the day's leg count plus the latest change on those legs and on
their reservations; when it differs from the one the page was rendered with,
the page reloads, unless a drag is in progress, a modal is open, or an input
has focus.

**Expect to be asked:**
- *"Can I remove the ghost?"* No. It clears from the board at the end of the
  day with everything else. It is there so nobody wonders where the job went.
- *"The board reloaded while I was looking at a popup."* Hover popups are not
  held; drags, dialogs and typing are. Say so if that gets in the way and the
  interval or the guards can be tuned.
