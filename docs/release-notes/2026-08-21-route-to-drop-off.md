---
date: 2026-08-21
audience: Dispatchers
title: Right-click a trip that's already been picked up and it routes to the drop-off
---

# Right-click a trip that's already been picked up and it routes to the drop-off

## Send this to the team

> Hey team — right-clicking a trip and choosing the route now follows the car
> through the trip instead of always pointing at the pickup.
>
> 1. Right-click the trip on the board, same as always.
> 2. If the chauffeur hasn't got the guest yet, you'll see **Route to pickup** —
>    exactly what you've been using.
> 3. Once he's marked picked-up (or on location), it says **Route to drop-off**
>    and opens directions from where the car is right now to where the guest is
>    going. That's your "how much longer is he?" answer.
>
> On a trip already marked completed you just get the car's position on the map —
> there's nowhere left to route it.
>
> Nothing else about the menu changed. Mapping, the flight tracker, the unit
> number and the "last seen" line are all where they were, and nothing here
> touches the driver app or messages anyone.

---

## Behind the scenes

**Where it lives:** the right-click menu on any dispatch board or trip list, and
the Fleet page rows (unchanged there — no job in view, so it's still a map pin).

**Why:** the row always aimed at the pickup, so on a trip in progress it offered
directions to an address the chauffeur had already left. The live ETA badge on
the board was already measuring to the drop-off in that situation, so the two
now say the same thing.

**Expect to be asked:**
- *"Why does it still say pickup on this one?"* — the chauffeur hasn't marked
  picked-up yet. The menu follows his status, not the clock.
- *"It only gives me a map pin now."* — that trip is marked completed or
  cancelled, or the drop-off address is blank. The wording under the row says
  which.
- *"Does on location count?"* — yes. He's standing at the pickup, so routing him
  to it would be directions to where he already is.
