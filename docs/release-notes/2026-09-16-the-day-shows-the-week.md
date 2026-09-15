---
date: 2026-09-16
audience: Dispatchers
title: The day now covers a week, and leads with the cars that have room
---

# The day now covers a week, and leads with the cars that have room

## Send this to the team

> Hey team — **Fleet → The day** now runs a week instead of three days.
>
> 1. Seven buttons across the top, and each one tells you what it is before you
>    click: *97% assigned*, *62% assigned*, or *256 trips · not built*.
> 2. Days that are built look exactly as before. Days nobody has assigned yet
>    show the trips booked and what that weekday normally takes to run — and no
>    car rows at all, because which car is free that day is not known yet.
> 3. Cars with the most room now come **first**, so the ones standing still are
>    at the top instead of the bottom.
>
> Every trip on a row now shows its start and finish time without hovering, and
> on a phone the clock becomes a plain list you can actually read.
>
> Nothing about assigning changed. This is still a read-only view of trips you
> have already assigned, and an unbuilt day still never shows a free car.

---

## Behind the scenes

**Where it lives:** Fleet → **The day**.

**Why:** three days was the honest limit while the page only knew how to draw a
clock. The board fills in as a gradient — measured on the live data, 97% of
today's trips carry a chauffeur, 87% tomorrow, 62% the day after and **0% for
the four days beyond**, against 162, 256, 193 and 154 booked trips. Simply
extending the clock would have shown every car as "free all day" on a Saturday
carrying 256 trips.

**So there are two row treatments**, chosen by that day's own coverage rather
than by how far away it is: a built day gets the clock, a day with no chauffeurs
on it gets its demand and nothing else. The Saturday figure is real and is
exactly what a shop day should be planned against.

**Expect to be asked:**
- *"Why can't I see which car is free on Saturday?"* — Nobody has assigned it
  yet. The Outlook plans that far out without naming a car.
- *"The order changed."* — Cars with the longest usable gap lead now; the page
  exists to find room, and the busiest car is the one you can't act on.
- *"Addresses got shorter on my phone."* — Only the venue is shown in the list;
  the full address is still there when you hover on a computer.
