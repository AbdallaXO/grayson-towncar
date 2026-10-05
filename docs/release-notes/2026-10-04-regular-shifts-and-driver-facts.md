---
date: 2026-10-04
audience: Dispatchers
title: Every driver's profile shows their regular shift, their limits and their regular car
---

# Every driver's profile shows their regular shift, their limits and their regular car

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
-->

## Send this to the team

> Hey team — every driver's profile now has a Shift facts card: Morning, Midday, Evening or Float driver (Float means any shift), their regular week (including "Morning or Evening" and "done by 3 PM" days), the earliest they start and the latest they finish, days a week, extra-shift days, and their regular car.
>
> 1. Drivers → Regular Shifts lists who still needs one, with a suggestion from their last 8 weeks.
> 2. A manager picks the usual shift and working days, changes any day that's different, and presses Confirm.
> 3. The Morning, Midday and Evening shapes are on Shift Templates, each capped at 12 hours.
>
> Only managers can change these; everyone can see them.
>
> Auto-assign keeps using today's hours until a manager switches it over. Day Setup still offers each driver his regular car first. Nothing is sent to drivers, and the driver app is the same.

---

## Behind the scenes

**Where it lives:** Every driver's profile, left column, just above Weekly Schedule. Managers change the earliest start, latest finish, days a week, extra-shift days and regular car under Edit Driver → Shift facts. The regular week itself is set on Drivers → Regular Shifts.

**Why:** Each driver's hours lived in a fixed table nobody could see or change, so auto-assign was working from a guess. This puts every driver's real week and limits in one place, to the minute, and holds every regular day to 12 hours from leaving base to getting back — ready for auto-assign once everyone has one.

**Expect to be asked:**
- *"The card says No regular shift yet."* — Nobody has confirmed one for that driver. A manager does it on Regular Shifts.
- *"I changed a driver's earliest start and it won't save."* — One of their confirmed days starts before it. The message names the day: change that day on Regular Shifts first, or pick a limit that fits.
- *"Does this change who gets trips today?"* — No. Auto-assign only reads regular shifts after a manager switches it over on Regular Shifts, and that can't happen until every driver has one.
- *"What's Float?"* — A driver who goes wherever the day needs them, any shift, still within 12 hours and their limits.
