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
> 1. Drivers → Regular Shifts lists who still needs one, suggested from their last 8 weeks.
> 2. A manager presses Review & confirm, picks the usual shift and working days, changes any day that's different, and confirms.
> 3. The Morning, Midday and Evening shapes are on Shift Templates, each capped at 12 hours.
>
> Only managers can change these; everyone can see them.
>
> Auto-assign, the schedule board and Day Setup stay on today's hours until a manager switches over on Regular Shifts, once everyone has one. Day Setup still offers each driver his regular car first. Nothing is sent to drivers, and the driver app is the same.

---

## Behind the scenes

**Where it lives:** Every driver's profile, left column, just above Weekly Schedule. Managers change the earliest start, latest finish, days a week, extra-shift days and regular car under Edit Driver → Shift facts. The regular week itself is set on Drivers → Regular Shifts (managers also get a Set / Edit regular shift button on the card). The switch for auto-assign sits at the top of Regular Shifts.

**Why:** Each driver's hours lived in a fixed table nobody could see or change, so auto-assign was working from a guess. This puts every driver's real week and limits in one place, to the minute, and holds every regular day to 12 hours from leaving base to getting back — ready for auto-assign once everyone has one.

**Expect to be asked:**
- *"The card says No regular shift yet."* — Nobody has confirmed one for that driver. A manager does it on Regular Shifts.
- *"It says No regular shift yet for an affiliate (or an operator), and Regular Shifts doesn't list them."* — Right: regular shifts are for our in-house chauffeurs only. Affiliates and operators never get one.
- *"Shift facts says Friday Evening, but Weekly Schedule right below says Full Day."* — Weekly Schedule is the old schedule. Auto-assign, the schedule board and Day Setup keep using it, and the old hours, until a manager switches over to regular shifts, so the two can disagree until then. Nothing on Weekly Schedule changed.
- *"The regular car says (inactive)."* — That car is out of service, so Day Setup won't offer it. Pick his new car under Edit Driver → Shift facts.
- *"I changed a driver's earliest start and it won't save."* — One of their confirmed days starts before it. The message names the day: change that day on Regular Shifts first, or pick a limit that fits.
- *"Does this change who gets trips today?"* — No. Nothing reads regular shifts until a manager switches over on Regular Shifts, and that can't happen until every driver has one.
- *"What changes when a manager switches over?"* — Every driver with a regular shift is read by it: auto-assign plans his day around it, held to 12 hours from leaving base to getting back; the schedule board shows those hours and checks moves against them; and Day Setup follows his regular days on and off, so a regular day off shows as off. Switching back puts everything on today's hours again.
- *"The Use regular shifts for auto-assign button is greyed out."* — Someone still needs a regular shift. The line beside it says how many, and they're listed under Needs a regular shift.
- *"A new driver shows under Needs a regular shift, but auto-assign is already on regular shifts."* — That's expected. The switch stays on, and dispatch keeps using his old hours until a manager confirms his regular shift.
- *"It says no trips in the last 8 weeks."* — There's nothing to suggest from. A manager picks his usual shift and days by hand — or no days at all, for a driver who only takes extra shifts.
- *"What does Same as usual mean on a day?"* — That day follows the driver's usual shift. Leaving its times blank means the shift's usual times, shown under the boxes.
- *"What's Float?"* — A driver who goes wherever the day needs them, any shift, still within 12 hours and their limits.
