---
date: 2026-09-02
audience: Dispatchers
title: Auto-Assign stops booking pickups the driver can't reach in time
---

# Auto-Assign stops booking pickups the driver can't reach in time

<!--
  NOTE: the Build-a-plan half of this note only matters once the Day-Builder
  switch is on. The Auto-Assign change is live for everyone the moment this
  deploys. Send the message the day it goes out.
-->

## Send this to the team

> Hey team — Auto-Assign used to put drivers on departures and cruise runs it
> couldn't actually get them to on time. It doesn't any more.
>
> The clearing time on the board and the clearing time Auto-Assign used to
> decide were two different numbers, and the board's was the later one. So you'd
> see a driver clearing 12:03 with a 12:00 pickup next, and the schedule
> thought that was fine. It now uses the same time you see.
>
> What changes for you: a few more trips a day come back unassigned instead of
> being put on someone who'd turn up late. Those are ones to farm out or shuffle
> yourself. In exchange, when the board says a driver makes his next pickup, he
> makes it.
>
> Airport arrivals are unchanged — being a few minutes past a landing time was
> never a problem, the guest is still getting their bags.
>
> Also: the Day Setup panels got tidied up. Same information, less reading.
>
> Assigning trips by hand, the schedule board and the driver app all work
> exactly the same as yesterday.

---

## Behind the scenes

**Where it lives:** Auto-Assign All on the capacity planner, and the Day Setup
window's second-shift and Build-a-plan panels.

**Why:** the engine had two clocks. Chain feasibility used a static planning
model (booked time + a flat 45-minute airport dwell + a category-average
drive). The clearing time rendered on the board used the measured one — real
drive and dwell figures for that hour and day type, the flight time when known,
plus the Publix stop. They disagreed by up to 35 minutes, and the board's was
the later. Measured across 11 replayed days, that put roughly **10 fixed-time
pickups a day** on drivers who could not physically arrive. On the founder's
own 2026-09-12 board it was 22 in one day, the worst 27 minutes late.

Chain feasibility now takes the later of the two whenever the next pickup is a
fixed-time job. Airport arrivals keep the old behaviour deliberately — their
booked time is the landing slot, and the deplaning grace already covers it.
Applying it there too cost 4.6 trips a day for no punctuality gain.

Repositioning between jobs is untouched and still uses the category table.

**The trade, measured:** 107 late fixed-time pickups over 11 days became 1.
Trips kept in-house fell by 40 over the same 11 days, about 3.6 a day, roughly
$7,200 per 28 days in affiliate premium. Those trips are farmed, not lost.

**Expect to be asked:**
- "Why is Auto-Assign leaving me more trips?" Because it stopped promising
  pickups it couldn't make. The ones it leaves are the ones that needed a
  person to decide.
- "Can I make it tighter again?" Yes — the Turn buffer control in the
  Auto-Assign and Schedule Builder windows, and per-driver buffers on the
  driver record. Aggressive means no cushion beyond the drive. It's safer to
  use now than it was, because the base time it measures from is honest.
- "Did the second-shift cards change what they suggest?" No. Same proposals,
  same rules. The card just reads as a name, a car and a shift instead of a
  paragraph, and the dollar figure moved to the tick-box tooltip.
