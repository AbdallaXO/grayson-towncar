---
date: 2026-09-17
audience: Dispatchers
title: Every car's service intervals on one screen
---

# Every car's service intervals on one screen

## Send this to the team

> Hey team — new **Service** tab under Fleet. Every car's oil, tires, brakes
> and inspection in one grid, instead of opening cars one at a time.
>
> 1. Fleet → **Service**
> 2. Click any square to set how often it's due and when it was last done
> 3. Car just back from the shop? Same square, **Just done** — the date and
>    mileage are already filled in, one click logs it and restarts the clock
>
> A dashed square means nothing is set for that car. A grey one means we don't
> know when it was last done, so it can't tell you anything — the windshield
> sticker is usually enough to fix it. Works on your phone.
>
> The old "add the standard intervals" button is gone; it guessed. The shop's
> name, the cost and days off the road still live on the car's own page.

---

## Behind the scenes

**Where it lives:** Fleet → Service (new tab, between Vehicles and Inspections),
It's on the fleet manager's top bar too. The Forecast view on the same tab
has its own note.

**Why:** Setting an interval used to mean opening one car at a time and scrolling
to find the panel, so in practice they never got set — and an odometer we read
every three minutes had nothing to feed. A grid makes a missing interval visible
as an empty square instead of something you'd have to go looking for.

**What went away:** the "add the standard intervals" button on the desk and the
"use the standard set" link on the car page. It stamped every car with a generic
table of numbers, which is not how a real maintenance schedule gets set, and
once stamped those numbers read as fact.

**Expect to be asked:**
- *"Why does this car say no baseline?"* — we know how often it's due but not
  when it was last done, so there's nothing to count from. Put in the last
  service date and it starts working.
- *"Can I mark an oil change done from the grid?"* — yes, "Just done" on the
  square. It writes a real service record, so the Report's cost and
  on-time-service figures pick it up; only the vendor, the downtime dates and
  the written description need the car's own page.
- *"I logged one by mistake."* — delete the record on the car's page, then fix
  the last-done date on the square. Deleting a record deliberately does not
  wind the interval back on its own.
- *"Where did the button that filled everything in go?"* — removed on purpose.
  It was inventing intervals nobody had chosen.
