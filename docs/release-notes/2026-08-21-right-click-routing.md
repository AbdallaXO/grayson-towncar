---
date: 2026-08-21
audience: Dispatchers
title: Right-click a trip for directions from the car — including through the job it's on
---

# Right-click a trip for directions from the car — including through the job it's on

## Send this to the team

> Hey team — right-click a trip and you can now get driving directions from that
> car to either end of the trip, and to a job it hasn't started yet.
>
> 1. Right-click the trip on the board.
> 2. **Route to pickup** and **Route to drop-off** both open Google Maps from
>    where that car is sitting right now.
> 3. One of them is marked **next** — the end the chauffeur is actually driving
>    to. It's the pickup until they mark picked-up, then it's the drop-off.
> 4. If they're still finishing another job, you also get **Route via current
>    drop-off**: the car, then the drop-off they're running, then this pickup —
>    one route, both stretches timed. That's the honest "when can they be here?"
>
> If a trip is missing an address you only get the end we have, and it says which
> one is missing. Nothing else in that menu changed, and nothing here touches the
> driver app or messages anyone.

---

## Behind the scenes

**Where it lives:** the right-click menu on any dispatch board or trip list. The
Fleet page rows are unchanged — no trip in view there, so it's still just the
car's position on a map.

**Why:** the row only ever pointed at the pickup, which goes stale the moment the
guest is aboard. It now offers both ends and marks the live one. The third row
answers the back-to-back case: a car heading to MCO with a guest, next job back at
Port Orleans — measuring the car straight to Port Orleans is a number nobody
should act on, because nobody is driving there next.

**Expect to be asked:**
- *"Why don't I see the via row?"* — it only shows when that driver has another
  job in progress. Right-click the later job, not the one they're on.
- *"Does it allow for the time at the drop-off?"* — no, it's driving time. The
  card's own pickup-risk line does add that allowance.
- *"Why is nothing marked next?"* — the trip is completed or cancelled. Both
  routes still open; there's just no next stop to point at.
