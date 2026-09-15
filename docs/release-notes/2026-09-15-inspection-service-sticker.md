---
date: 2026-09-15
audience: Dispatchers
title: The walk-around is shorter, and it now records the service sticker
---

# The walk-around is shorter, and it now records the service sticker

## Send this to the team

> Hey team — the **Inspections** checklist has changed.
>
> 1. Three items are gone: water and amenities, phone chargers, and the
>    spare/jack. Car seats stay, and now sit with the paperwork under
>    **In the car**. Seventeen items instead of twenty.
> 2. At the bottom there are now two numbers: **miles on the dash**, and
>    **next service due at** — the mileage printed on the windshield sticker.
> 3. Put both in and the system works out when that service actually lands,
>    using how hard that particular car is driven. The date on the sticker is
>    written for a car doing thirty miles a day; ours do two hundred and up.
>
> The sticker figure carries over week to week, so you only retype it when the
> car has actually been serviced.
>
> Nothing else about inspecting changed — same tick-good-or-problem, same note
> and photo on every item, same Monday reset, and finding something still files
> a problem report without taking the car off the road.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet** → **Inspections** → any car.

**Why the trim:** founder's call. Water, chargers and the spare are the
chauffeur's domain, not the fleet manager's weekly walk. Any inspection already
recorded against a dropped item still reads back correctly — answers are stored
by item, not by position.

**Why miles and not a date:** a shop prints "or by 3 November" for a car doing
about thirty miles a day. The Sprinters here run 190–350 and the SUVs 320–350, so
the mileage always lands long before the printed date and the date is fiction.
Only the mileage is stored. The date is derived from that unit's own measured
daily mileage, so the same sticker on two different cars gives two different
answers:

> 3,269 mi to go — in about 10 days at 340 mi/day → Fri 25 Sep
> 3,269 mi to go — in about 18 days at 190 mi/day → Sat 3 Oct

Where a car has no mileage history the system says so rather than guessing —
"not enough mileage history to say when". A projection someone books a shop day
around has to refuse when it cannot know.

**The odometer field** also shows what the tracker last read, so the dash figure
can be sanity-checked instead of typed blind. On the two units with no tracker
fitted it says so — for those cars the inspection is the only mileage the system
will ever get, and it is what makes their service intervals work at all.

**Expect to be asked:**
- *"Do I have to fill the sticker in every week?"* — No. It carries forward from
  the last inspection that had one and only needs changing after a service.
- *"What if I don't know the sticker number?"* — Leave it blank. Nothing breaks;
  you just don't get the projection for that car.
