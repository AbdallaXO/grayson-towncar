---
date: 2026-08-28
audience: Dispatchers
title: The suggested rate on a multi-leg booking now prices every leg
---

# The suggested rate on a multi-leg booking now prices every leg

## Send this to the team

> Hey team — the suggested rate box in the booking wizard now prices every leg of a
> trip, not just the first one.
>
> Before, if you built a reservation with two legs that weren't a true there-and-back
> (say, airport to Disney, then Disney to the port on a different vehicle), the
> suggested price quietly treated it as a round trip on leg one's route and vehicle
> only — the second leg's price never made it into the number. You'd see one number
> that was way off, with no sign anything was missing.
>
> Now each leg is priced on its own route and its own vehicle, and the suggested
> total is the sum. The box also spells out each leg it priced, so you can see exactly
> what went into the number before you use it.
>
> What did NOT change: a real round trip — same two stops, there and back, same
> vehicle — still shows one bundled round-trip price like it always has. You still
> have to hit "Use Suggested" or type your own number; nothing books itself.

---

## Behind the scenes

**Where it lives:** the Pricing step of the dispatcher booking wizard (the "Suggested
Rate" box)

**Why:** the wizard decided "round trip" just by counting legs — any reservation with
exactly two legs got treated as one, even when the second leg went somewhere else
entirely on a different vehicle. That collapsed the price down to a single leg-one
rate and silently dropped the rest. A real reservation was built this way: airport to
Disney by SUV, then Disney to the port by van, and the suggested price came back as a
single MCO-to-Disney SUV round trip — missing the van leg completely.

**Expect to be asked:**
- *"Why did the suggested number change for a trip I've built before?"* — Only
  multi-leg trips that aren't a genuine round trip are affected. A normal one-way or
  a real there-and-back prices exactly as before.
- *"What counts as a real round trip now?"* — Two legs, same two stops in reverse,
  same vehicle on both. Anything else prices leg by leg.
