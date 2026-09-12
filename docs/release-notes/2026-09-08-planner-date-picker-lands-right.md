---
date: 2026-09-08
audience: Dispatchers
title: The day planner lands on the date you picked
---

# The day planner lands on the date you picked

## Send this to the team

> Hey team — when you pick a date on the day planner, it now goes to that date every
> time.
>
> 1. Pick the date the way you always have — the calendar or by typing it.
> 2. The line under the title says "Loading Saturday, September 12" while it builds.
> 3. When it stops loading, that is the day you are on.
>
> Before, typing a date could drop you on the 1st of the month instead, and after
> going back or refreshing, the date in the box could disagree with the day on
> screen. That mattered: Suggest Day Setup, Reset All and Save all act on the day in
> that box. If the box and the page ever looked out of step, they do not anymore.
>
> Nothing else on the planner changed. Same buttons, same schedule, same arrows for
> yesterday and tomorrow.

---

## Behind the scenes

**Where it lives:** The date box at the top right of the day planner.

**Why:** Three separate faults, which is why it was intermittent. Typing a date acted
on each keystroke, so 09/12 left for 09/01 the moment the "1" landed. Browsers restore
what was typed in a date box after a refresh or a Back, so the box could hold one day
while the page showed another — and the box is what ten of the planner's buttons read
to decide which day they change. And a day takes seconds to build with nothing on
screen admitting it, so a dispatcher would pick again.

**Expect to be asked:**
- *"Did it lose my picked date?"* No — the box is now pinned to the day actually on
  screen, so what you read is what the buttons will act on.
- *"Why the short pause after I pick?"* It waits for you to finish typing before it
  moves. A pick from the calendar still goes straight away.
