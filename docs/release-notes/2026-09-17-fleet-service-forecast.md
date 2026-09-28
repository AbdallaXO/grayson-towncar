---
date: 2026-09-17
audience: Dispatchers
title: See which cars will need work before they need it
---

# See which cars will need work before they need it

## Send this to the team

> Hey team — the Service tab now has a **Forecast** next to the grid. It works
> out what each car will need over the next 30, 60 or 90 days from how many
> miles that car is actually doing, so a Sprinter running 300 a day shows up
> sooner than a towncar that mostly sits.
>
> Fleet → **Service** → **Forecast**, then pick your window.
>
> A date with a ≈ is an estimate and will move if the car's work changes. A
> date without one is a real deadline, like an annual inspection. It also tells
> you how many cars it can't predict yet and why — usually because nobody has
> recorded their last service.
>
> It doesn't book anything and nothing is scheduled automatically. Use the
> Outlook as always to pick the actual day.

---

## Behind the scenes

**Where it lives:** Fleet → Service → the Grid / Forecast toggle.

**Why:** the projection maths already existed and the desk already used it, but
only for the next fortnight and only four lines of it. Nothing showed the whole
fleet's forward picture, so shop visits got batched by memory.

**Expect to be asked:**
- *"Why is it nearly empty?"* — it can only project what has a last service
  behind it. The line under the list says how many it cannot see and why; fill
  those in on the grid and they appear.
- *"Can I trust the ≈ dates?"* — they are the last 30 days of mileage carried
  forward. A car that starts working harder comes due sooner than shown. It is
  a planning aid, not a booking.
- *"A car is missing entirely."* — a car that has not moved recently gets no
  estimate at all, on purpose. Projecting a parked car produces a date years
  out that someone would plan around.
