---
date: 2026-10-04
audience: Dispatchers
title: Saving a driver's week only changes what you changed
---

# Saving a driver's week only changes what you changed

## Send this to the team

> Hey team — saving a driver's week now only changes what you actually changed.
>
> Saving from the Driver Schedules button on the capacity planner no longer clears a driver's max hours, preferred time of day, schedule notes or set shift. Saving on the In-House Driver Schedules page no longer resets their usual hours to 6 AM–11 PM.
>
> If one of those went missing for a driver lately, it needs putting back once. Max hours and preferred time you can set on the In-House Driver Schedules page; for a schedule note or a set shift, tell Abdalla. From now on they stay.
>
> Nothing else changed: same screens, same buttons. Days off, exceptions and the Flex box work exactly as before.

---

## Behind the scenes

**Where it lives:** the Driver Schedules button (top of the capacity planner) and the edit panel that opens when you click a driver on the In-House Driver Schedules page. Both save through the same place.

**Why:** each screen only shows some of a driver's schedule, and saving filled everything it didn't show with a blank or a stock value. A planner save wiped max hours, preferred time of day, schedule notes and morning/evening-type shifts on all seven days, and turned off the driver's own default max hours and preferred time. Every save from the In-House page reset the driver's defaults to 6 AM–11 PM flexible, which is what a day without its own row falls back to.

**Also now true:**
- An hour outside midnight–11 PM, a day that isn't Monday to Sunday, or max hours over 24 is refused with a message, and nothing in that save is kept, rather than half-saving. Neither screen lets you pick those, so nobody should see it.
- Ticking Flex on a fixed day in the planner still makes it an open day.
- A day that had no row of its own starts from the driver's usual settings when first saved, instead of stock ones.

**Expect to be asked:**
- "Does this bring back what was wiped?" No. Nothing kept a copy. Put back what's wrong once (admin → Drivers → the driver → weekly schedule rows for notes and named shifts) and it now survives saves.
- "Which drivers were hit?" Anyone saved from either screen before today. Morning/evening-type shifts were turned into plain full day, and defaults went to 6 AM–11 PM flexible with no max hours.
