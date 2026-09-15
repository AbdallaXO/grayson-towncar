---
date: 2026-09-15
audience: Dispatchers
title: Fleet can see what every car is doing today, hour by hour
---

# Fleet can see what every car is doing today, hour by hour

## Send this to the team

> Hey team — Fleet has a new screen called **The day**.
>
> 1. Open **Fleet**, then **The day**. One line per car, its trips laid out
>    across the clock, and the driver holding it.
> 2. The gold blocks are gaps long enough to put a car in the shop. Hover any
>    trip to see the guest, the pickup and the drop.
> 3. **Today / Tomorrow / Thu** at the top switches days.
>
> The line at the very top tells you how much of that day is actually assigned.
> If the day isn't built yet it says so plainly — an empty car on an unbuilt day
> is **not** a free car, and the screen will never pretend otherwise.
>
> Nothing about the board or assigning changed. This is a read-only view of
> trips you have already assigned — fleet cannot move a job, and nothing here
> takes a car out of the pool.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet** → **The day**. New tab, sits between
Desk and Vehicles. Also reachable for any staff member by URL.

**Why:** the fleet manager's real question is about a *car* — "what is #007
doing, and when is it standing still?" — and the only screen that knew the
answer was the dispatch board, which is organised by driver and is not in the
fleet top bar. Every fleet screen so far could say a car was "free today", and
all of them were reading the same thing: no assignment row. Before Day Setup
runs, that is true of every car on the lot.

**What it actually reads:** a leg has no link to a physical car, so the chain is
the chauffeur — the car's driver that day, then that driver's trips. A car held
by two chauffeurs (an AM/PM share) merges both, and the seam between them is
labelled a handoff, never offered as shop time.

**The honesty line:** the page leads with the share of that day's trips that
have a chauffeur. Measured on the live data, the board fills in as a gradient —
about 97% assigned today, 85% tomorrow, 62% the day after, nothing beyond. Below
90% every empty car reads "not assigned yet" instead of "free".

**Expect to be asked:**
- *"Why does it only go three days out?"* — Because that is as far as the board
  is genuinely built. Day four onward has trips but no cars on them, and the
  page would be nineteen rows of "not built yet".
- *"The gold gap disappeared when I looked again."* — An airport arrival's
  pickup time moves with the flight, so a gap behind one needs to be an hour
  longer before it counts as usable. Times shown are planning estimates, the
  same ones the shop-window finder uses.
- *"Can I book the shop from here?"* — Not yet. Use **When can I take a car
  down?** on the Desk.
