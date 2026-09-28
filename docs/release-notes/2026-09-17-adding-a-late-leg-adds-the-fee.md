---
date: 2026-09-17
audience: Dispatchers
title: Adding a late-night leg now adds the $20
---

# Adding a late-night leg now adds the $20

## Send this to the team

> Hey team — when you add a leg to an existing trip and the pickup is between
> 10 PM and 6 AM, the $20 after-hours fee now goes on the price automatically.
> You don't have to remember it any more.
>
> You'll see the trip total go up by $20 the moment the leg is added — same as it
> would if you'd booked that leg from the start. Two late legs, $40.
>
> Before now, adding a leg skipped the fee completely: the guest was never billed
> for it, and on some trips the board then turned around and asked you to collect
> it. Both of those stop.
>
> Nothing changed about adding a daytime leg, and nothing changed about how you
> charge a fee on the trip card. If a guest shouldn't be charged the $20, take it
> off the price the way you always have.

---

## Behind the scenes

**Where it lives:** the "add a leg" action on an existing trip.

**Why:** `add_leg_to_reservation` created the leg and touched neither place the
after-hours fee lives — not the per-leg marker and not the reservation's charges.
So the money was never billed (23 legs driven without it, about $170/month) and,
because the marker was also unset, a later flight-delay pass could raise a task
asking a dispatcher to collect a fee nobody had been charged.

The booking wizard has always priced this correctly; only the add-a-leg path was
blind to it. Both homes are now written together, which is the invariant the rest
of the after-hours logic depends on.

**Expect to be asked:**
- *"Will it double up with the Charge button?"* No. The leg is marked as carrying
  the fee, so the board no longer flags it as owing.
- *"What about legs added before today?"* They keep the price they have. This only
  applies going forward.
- *"Can I still add a late leg without the fee?"* Yes — adjust the price on the
  trip the same way you would for any other discount.
