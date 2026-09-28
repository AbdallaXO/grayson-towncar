---
date: 2026-09-21
audience: Dispatchers
title: Ops Control only files a task when there is something to do about it
---

# Ops Control only files a task when there is something to do about it

## Send this to the team

> Hey team — Ops Control is going to be a lot quieter, on purpose. We went
> through six months of task history: most tight-turn, conflict and flight tasks
> were closing on their own, and the ones you closed by hand were coming back
> two hours later. That stops today.
>
> What's different:
>
> 1. A tight turn or conflict has to still be there on the next check before it
>    becomes a task. A real "won't make it" with the pickup inside two hours still
>    shows up right away.
> 2. When you close a turn or flight task, it stays closed until a time, a driver
>    or the flight actually changes. Your call stands.
> 3. Tight turns for tomorrow and later are no longer filed. Real conflicts on
>    future days still are.
> 4. An after-hours fee task now asks the money question when you complete it:
>    charge it, already collected, or waive it. Pick one and it is settled for good.
> 5. Unpaid-booking tasks only appear three days before the trip. The guest
>    still gets the automatic reminders before that.
>
> What did not change: the schedule board, the red conflict flags on trips, and
> how you assign drivers. Nothing automated touches a driver.

---

## Behind the scenes

**Where it lives:** the 30-minute scanner behind Ops Control, and the Complete
button on the task queue.

**Why:** the audit (see `docs/scheduling-redesign/analysis/29_task_queue_audit.py`
against the 2026-09-21 snapshot) found roughly 200 tasks filed a day and about
120 dispatcher clicks on them, with these facts behind the five rules above:
half of all same-day tight turns were gone within one 30-minute tick whether
anyone touched them or not; 38% of hand-closed turn tasks, 49% of flight tasks
and 94% of fee tasks were re-filed within a day; 0 of 321 future-board tight
turns ended in a driver move; 88% of fee tasks ended with no fee on the leg;
untouched unpaid bookings paid themselves in a median 10 hours.

The mechanics: every scanner task now carries a fingerprint of the facts it
describes (the two legs, their times, the driver, the tier, the lateness to
ten minutes). A task a person closed suppresses re-filing for 72 hours while
that fingerprint is unchanged. Auto-closes and signal closes do not count. The
two-sighting rule uses the shared cache with a 90-minute memory, so it works
across workers. A new "Came back" number in the Ops Control header counts
hand-closes the scanner re-filed within a day over the last seven days. It
should sit near zero; if it climbs, the scanner is arguing with the team again.

**Expect to be asked:**
- *"A conflict I saw on the board isn't in Ops Control."* If the pickup is more
  than two hours out, it appears on the next 30-minute check, if it is still
  there. If it is inside two hours and red, it is there now.
- *"I closed it and it didn't come back — is it still a problem?"* Closing it
  says "the board is right, leave it." If a time or driver changes, it comes
  back as a new task. If you closed it by mistake, change nothing and re-check
  the trip on the board.
- *"Why can't I just complete the fee task?"* Because a blank close never did
  anything, and the same task came back on almost all of them. Pick charge,
  collected or waive. The old fee tasks that were open when this shipped
  behave the same way.
- *"Where did the unpaid tasks for next week go?"* Closed with a note saying
  when they come back: three days before the trip.
