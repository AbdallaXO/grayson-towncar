---
date: 2026-09-14
audience: Dispatchers
title: The Fleet desk now says what to do, and which hour to put a car in the shop
---

# The Fleet desk now says what to do, and which hour to put a car in the shop

## Send this to the team

> Hey team — the **Fleet** desk and its **Outlook** page have been rebuilt.
>
> 1. Open **Fleet**. The dark band at the top shows every car in one of five
>    states — ready, watch, booked in, back-but-not-confirmed, off the road —
>    and what's in the shop right now.
> 2. **Do now** is one line per car that needs a decision, with the buttons on
>    it: **Find a window**, **Take off road**, **Mark back on the road**.
>    Tap a fault code to see what the car is reporting.
> 3. **When can I take a car down?** — pick the car and how long the job is,
>    and it names the best shop window this week, hour by hour, from the trips
>    already on the board. **Book it in** puts it on the board; **Full outlook**
>    goes 28 days out.
>
> Paperwork is now grouped — one line for "MCO permits, all units" instead of
> one per car.
>
> Nothing about assigning changed: a car booked in or off the road still shows
> red in the planner's pool, and you can still override it when you know it's
> back. Reporting a problem from the car's page works exactly as before.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet** (the desk) and **Outlook**. The
Vehicles table, the car page and the Report are unchanged.

**Why:** the first version of the desk was a report — 24 undeduplicated
attention lines (17 of them the same permit expiry per car), five equal stat
tiles, the vehicle table repeated, and a 14-day strip that called 11 of 14 days
"conflict". This is the design handoff for the fleet manager: an action queue
and an hour-level planner that read from one capacity model.

**What changed underneath:**
- The hour grid is real: for every day, every shop hour and every vehicle
  type, the most trips under way at once that need that type or bigger — the
  same in-flight arithmetic the planner uses for its peak, with the same trip
  end estimates. Spare cars per hour decide the colour; the number is trips.
- **Book it in** creates a normal downtime for that day (the hours go in the
  reason, so the board reads "Shop 7:00 AM – 11:00 AM"). It runs the same
  demand check as the downtime form — a day this car tips into short asks
  once, then saves on the second click. Dispatch sees it the way it always has.
- The Short / Tight / Clear badge on each Outlook day is the downtime form's
  own verdict with that car removed, so the page can't promise a day the form
  then refuses.
- **Take off road** opens a same-day downtime (back on tomorrow's board);
  **Mark back on the road** closes it with the real date.

**Left out on purpose (no data behind them yet):** the weekly inspection
walk, the plain-English fault-code dictionary (chips show the code the car
reports), and a "restricted use" state.

**Expect to be asked:**
- *"Where did the vehicle table on the desk go?"* — It's the **Vehicles** tab,
  one click away. The desk shows only the cars that need a decision.
- *"It said Short but the squares are green?"* — The squares are booked trips
  in shop hours; the badge also knows what that weekday has actually run
  lately and where the day's peak is (often before 7 AM). Hover the badge.
- *"Booking a two-hour window took the car off the whole day."* — Yes. The
  board plans by day; the reason carries the hours so dispatch can use the
  car after the shop if they want to.
