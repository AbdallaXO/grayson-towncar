---
date: 2026-09-16
audience: Dispatchers
title: Taking a car off the road now asks when it's back, and shows what moves
---

# Taking a car off the road now asks when it's back, and shows what moves

## Send this to the team

> Hey team — **Take off the road** on the Fleet desk now asks two things it
> never used to.
>
> 1. **When is it back?** Tap **Tomorrow**, **In 2 days**, **In 3 days**,
>    **Next week**, or pick a date. It used to assume tomorrow without asking.
> 2. **What does that cost?** The moment you choose, it tells you what that car
>    is carrying on every day it would be off — for example *"#004 has 7 trips
>    Wed 16 Sep and 5 trips Thu 17 Sep — 12 jobs move."* Change the date and the
>    line changes with it.
>
> Underneath, the old line about the whole fleet being short is still there.
> The car's own jobs now come first, because that is what you're about to move.
>
> Nothing about the car coming back changed — it still returns to the board by
> itself on the date you pick, and you can still close it early. It is called
> **Take off the road** on every screen now; the car's own page used to call it
> something else.

---

## Behind the scenes

**Where it lives:** Fleet → **Desk** → any row in *Do now* → **Take off the
road**. Also the button on a single car's page, which now uses the same name.

**Why:** the confirmation gave the fleet-wide check ("19 needed at 10:36 AM, 18
left") and nothing at all about the car in front of you. #004 could be halfway
through seven trips with a named chauffeur on it and the panel said *"Dispatch
would need to farm out or move the job"* — singular, with nobody named. And the
return date was hard-coded to tomorrow, so a car booked solid on Wednesday came
off until Thursday without Wednesday ever being mentioned.

**What it reads:** the car's chauffeur for each blocked day and that
chauffeur's trips, through the same job-end estimator **The day** uses — so the
two screens can never disagree about when a trip is over. A job still running
counts as affected; pulling a car mid-trip is the case worth warning about.

**Expect to be asked:**
- *"It says nothing moves but the car has trips today."* — It counts what is
  still ahead. A car that finished at 3:15 PM has nothing left to move at 6 PM.
- *"Why does 'Tomorrow' mean one day off the road?"* — The date is the first day
  it is **back**, so the car is blocked up to but not including it.
- *"It says a day is 'not assigned yet'."* — The board is only built two or
  three days out. Beyond that nobody knows which car takes which job, so it says
  so rather than reporting zero trips.
