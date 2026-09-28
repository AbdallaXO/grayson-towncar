---
date: 2026-09-22
audience: Dispatchers
title: Tight Turn tasks are gone; Driver Conflict pages now show the real deadline
---

# Tight Turn tasks are gone; Driver Conflict pages now show the real deadline

## Send this to the team

> Hey team — the orange "Tight turn" tasks in Ops Control are gone, for good.
>
> They fired whenever a driver was going to reach the airport a few minutes after
> the plane touched the gate, even one minute. But our standard is inside the
> terminal by gate time plus ten, so those drivers were on time. Nothing to do,
> so no task. We're pulling about seventy of them off your list every day.
>
> Driver Conflict tasks still come in exactly as before, and that is the one that
> matters: the driver genuinely can't be inside by the deadline. When you open one
> it now says it plainly — the flight's gate time, the time he has to be inside,
> when he can actually get there, and how many minutes past the deadline that is.
> If the latest drive estimate says he'll make it after all, the page says that
> too, in green.
>
> What did not change: the schedule board still shows the dashed orange gap on a
> driver's day when a turn is tight, so you can still see it at a glance. The red
> conflict flags, the Call, Text and Reassign buttons, and how you assign drivers
> are all the same. Nothing automated touches a driver.

---

## Behind the scenes

**Where it lives:** Ops Control (the task list) and the Driver Conflict task page.
The 30-minute scanner is what stopped filing the amber tier.

**Why:** the audit against the 2026-09-21 snapshot
(`docs/scheduling-redesign/analysis/29_task_queue_audit.py`, 60 days): about 73
tight-turn tasks a day; at filing a third were 1–3 minutes "late", none more than
10, because by definition the driver met the gate + 10 rule. Of the same-day ones,
31% were closed by hand with a blank note, 28% closed themselves within the next
tick, 24% were replaced by the red Driver Conflict anyway, and 0 of 321
future-board ones led to a move. It was the single biggest source of hand-closes
in Ops Control (Luis alone closed 396 in 30 days).

The mechanics: `classify_turn` still reports the amber tier, and the board, the
Recovery Advisor and move-impact still describe it as "tight but makeable"; only
task filing stopped. A one-time migration closed the tight-turn tasks that were
open at the switch, with a note saying why. The conflict page's headline used to
be "+N min after arrival" measured against the raw gate time with a "typically
10–15 min to baggage claim" hint, which contradicted the rule the scanner filed
on; it now measures against gate + 10 (the same edge), and the timeline carries an
"Inside by" marker.

**Expect to be asked:**
- *"Where did the tight-turn tasks go?"* Retired. The driver was on time by our
  own rule. If a turn actually can't be made it is a Driver Conflict, same as
  before.
- *"I still see the orange gap on the board — is that a problem?"* No. That is the
  watch signal, and it is the right place for it. Nothing to do unless it turns
  red.
- *"The conflict page says he has minutes to spare — why is the task still
  open?"* The page uses the live drive estimate; the scanner re-checks every 30
  minutes and closes the task when it agrees. Leave it unless the earlier job
  slips.
