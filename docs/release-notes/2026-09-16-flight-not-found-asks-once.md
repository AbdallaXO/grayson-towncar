---
date: 2026-09-16
audience: Dispatchers
title: A wrong flight number asks once and stays put
---

# A wrong flight number asks once and stays put

## Send this to the team

> Hey team — when we can't find a guest's flight, that now shows up once and
> stays until someone fixes it. It used to appear, vanish on its own, and come
> back a while later.
>
> You'd have seen this as the same flight nagging you over and over. One trip
> asked about the same flight 36 times. If you ever fixed one of these and it
> reappeared the next morning, that wasn't you — it was closing itself behind
> your back.
>
> Now it sits in your list until the flight number is corrected. Once the flight
> is found, it clears itself like always.
>
> Nothing changed about how you fix it — same trip card, same place to correct
> the flight number. Ordinary flight-time changes are untouched; this is only
> about flights we can't find at all.

---

## Behind the scenes

**Where it lives:** the "Flight not found" item on the trip card and in the task
list.

**Why:** a closed loop, running every half hour. A refresh that can't find the
flight wipes the arrival times and raises a task. Thirty minutes later the
auto-closer asks "is the arrival time still different from the pickup time?" —
finds no arrival time at all, because the refresh just wiped it — and reads that
as resolved. Next refresh, same thing. 1,048 tasks across 307 legs; leg 25674
collected 36 for flight 4Y068.

Two fixes. Both refresh paths now go through the shared task creator, which
dedupes and holds a two-hour cooldown (they were bypassing it with a direct
create). And a not-found task is marked with its reason, so the auto-closer skips
it until the flight is actually found — a wrong flight number cannot resolve
itself.

Expected effect: roughly 1,048 of these become about 307, one per trip.

**Expect to be asked:**
- *"Will it pile up now that it doesn't close itself?"* No — it's one per trip
  instead of one every half hour, so the list gets shorter, not longer.
- *"What if the guest gave us a flight that genuinely doesn't exist?"* It stays
  until someone corrects it or the trip passes, which is the point — that's a
  guest who needs a phone call.
