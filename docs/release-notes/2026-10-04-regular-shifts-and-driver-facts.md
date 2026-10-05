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

> Hey team — driver profiles now have a Shift facts card: Morning, Midday, Evening or Float (any shift) driver, regular week (even "Morning or Evening" or "done by 3 PM" days), earliest start, latest finish, days a week, extra-shift days and regular car.
>
> 1. Drivers → Regular Shifts lists who still needs one, suggested from their last 8 weeks.
> 2. A manager presses Review & confirm, picks the usual shift and days, changes any day that differs, and confirms.
> 3. Shift Templates, linked there, holds each shift's usual times and longest length (never over 12 hours).
>
> Only managers change these; everyone can see them.
>
> Auto-assign, the schedule board and Day Setup stay on today's hours until a manager switches over, once everyone has one. Day Setup still offers each driver's regular car first. Nothing goes to drivers; the driver app is unchanged.

---

## Behind the scenes

**Where it lives:** Every driver's profile, left column, above Strengths & habits. Managers change the earliest start, latest finish, days a week, extra-shift days and regular car under Edit Driver → Shift facts. The regular week itself is set on Drivers → Regular Shifts (managers also get a Set / Edit regular shift button on the card). The switch for auto-assign sits at the top of Regular Shifts. The usual times of Morning, Midday, Evening and Float, and the longest each may run, are on Shift Templates (the link at the top of Regular Shifts): everyone can look, managers change them.

**Why:** Each driver's hours lived in a fixed table nobody could see or change, so auto-assign was working from a guess. This puts every driver's real week and limits in one place, to the minute, and holds every regular day to 12 hours from leaving base to getting back — ready for auto-assign once everyone has one.

**Expect to be asked:**
- *"The card says No regular shift yet."* — Nobody has confirmed one for that driver. A manager does it on Regular Shifts.
- *"Regular Shifts doesn't list an affiliate (or an operator)."* — Right: regular shifts are for our in-house chauffeurs only, as their profile says. Affiliates and operators never get one.
- *"Shift facts says Friday Evening, but Weekly Schedule further down says Full Day."* — Weekly Schedule is the old schedule. Auto-assign, the schedule board and Day Setup keep using it, and the old hours, until a manager switches over to regular shifts. After that it isn't used for a driver with a regular shift, and his Weekly Schedule card says so: change his week on Regular Shifts, because changing his days or hours in Edit Schedules does nothing for him. Time off still works. Nothing on Weekly Schedule changed.
- *"The regular car says (inactive)."* — That car is out of service, so Day Setup won't offer it. Pick his new car under Edit Driver → Shift facts.
- *"I changed a driver's earliest start and it won't save."* — One of their confirmed days starts before it. The message names the day: change that day on Regular Shifts first, or pick a limit that fits.
- *"I set his latest finish to 2 AM and it won't save."* — Without Next day ticked, 2 AM comes before his earliest start, so it leaves no time for any shift. Tick Next day (after midnight) and save again. The same goes for 12 AM meant as the end of the day.
- *"In the admin, deleting a day from a driver's weekly schedule (or changing its day) won't save."* — That day is part of his regular shift, and the old row holds it, so deleting or moving the row would erase or move his shift. Change the day on Regular Shifts instead. Those rows now show each day's regular shift, read-only; rows without one delete as before.
- *"Does this change who gets trips today?"* — No. Nothing reads regular shifts until a manager switches over on Regular Shifts, and that can't happen until every driver has one.
- *"What changes when a manager switches over?"* — Every driver with a regular shift is read by it: auto-assign plans his day around it, held to 12 hours from leaving base to getting back; the schedule board shows his regular shift and checks moves against it; and Day Setup follows his regular days on and off, so a regular day off shows as off, and uses his regular start time when two drivers share a car. A Morning day holds only the time he leaves base, and an Evening day only the time he's back: the other end may stretch to that shift's longest length, within his limits and the day's own Done by or Not before. Switching back puts everything on today's hours again.
- *"His Morning day says back at base at 2 PM, but auto-assign planned him until 5."* — Once a manager switches over, a Morning day's back-at-base time is what the board shows, not a stop: auto-assign may keep him out for up to Morning's longest length (on Shift Templates) after he leaves base. For a hard stop, open that day on Regular Shifts and set Done by. On an Evening day it's the other way round: set Not before to hold his start.
- *"I moved a 1 AM trip onto an Evening driver and it warned that his shift hadn't started."* — Once a manager switches over, the board reads each date on its own. That date's Evening shift starts in the afternoon and runs past midnight into the next day, so a 1 AM trip belongs to the shift that started the day before, and the warning says so. If he's covering it, keep it there: it warns, it never blocks, and a swap or farm-out can still give him more trips that day.
- *"The Use regular shifts for auto-assign button is greyed out."* — Someone still needs a regular shift. The line beside it says how many, and they're listed under Needs a regular shift.
- *"A new driver shows under Needs a regular shift, but auto-assign is already on regular shifts."* — That's expected. The switch stays on, and dispatch keeps using his old hours until a manager confirms his regular shift.
- *"It says no trips in the last 8 weeks."* — There's nothing to suggest from. A manager picks his usual shift and days by hand — or no days at all, for a driver who only takes extra shifts.
- *"What does Same as usual mean on a day?"* — That day follows the driver's usual shift. Leaving its times blank means the shift's usual times, shown under the boxes.
- *"What's Float?"* — A driver who goes wherever the day needs them, any shift, still within 12 hours and their limits.
- *"Can a regular shift sit outside a shift's usual times?"* — Yes. The usual times on Shift Templates are targets: the editor warns, and still confirms. Only the longest length is a hard limit, and it can never go over 12 hours.
- *"I lowered a shift's longest length on Shift Templates and it won't save."* — Some confirmed regular days on that shift are longer. The message names the drivers and days: shorten those on Regular Shifts first.
- *"I changed a shift's usual times, or made it longer, and it won't save."* — A confirmed regular week wouldn't fit any more: a day left with blank times would fall outside that driver's start and finish limits, say, or a longer day would leave too little rest before the next one. The message names the driver and the day: change that day on Regular Shifts first.
- *"I changed Morning's usual times. Did anyone's shift move?"* — Only Morning days left with blank times, and days that may be Morning or another shift. Days with their own times stay as they are, and auto-assign reads none of it until a manager switches over.
