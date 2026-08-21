---
date: 2026-08-21
audience: Dispatchers
title: Right-click a trip for directions from the car to either end of it
---

# Right-click a trip for directions from the car to either end of it

## Send this to the team

> Hey team — right-click a trip and you can now get directions from the car to
> either end of that trip.
>
> 1. Right-click the trip on the board, same as always.
> 2. You'll see **Route to pickup** and **Route to drop-off**. Either one opens
>    Google Maps with directions from where that car is sitting right now.
> 3. One of them is marked **next** — that's the one the chauffeur is actually
>    driving to. It's the pickup until he marks picked-up, then it's the drop-off.
>
> So "how far out is he?" and "how much longer has he got?" are both one click
> away now.
>
> If a trip is missing one of its addresses you'll only get the end we have, and
> it tells you which one is missing. Nothing else in that menu changed — mapping,
> the flight tracker, the copy-address rows, the car number and the last-seen
> line are all where they were. Nothing here touches the driver app or texts
> anyone.

---

## Behind the scenes

**Where it lives:** the right-click menu on any dispatch board or trip list. The
Fleet page rows are unchanged — no trip in view there, so it's still just the
car's position on a map.

**Why:** the row only ever pointed at the pickup, which goes stale the moment the
chauffeur has the guest. Rather than swapping which end it points to, the menu
now offers both and marks the live one, so neither question needs a second
screen. The "next" mark uses the same rule as the live ETA badge on the board, so
the two always agree.

**Expect to be asked:**
- *"Why is nothing marked next on this one?"* — the trip is marked completed or
  cancelled. Both routes still open; there's just no next stop to point at.
- *"Does on location count as picked up?"* — for this, yes. He's standing at the
  pickup, so the drop-off is what's left.
- *"I only see one route."* — that trip is missing the other address. The line
  under the rows says which.
