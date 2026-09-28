---
date: 2026-09-21
audience: Dispatchers
title: Legs on a new booking now put themselves in pickup order
---

# Legs on a new booking now put themselves in pickup order

## Send this to the team

> Hey team — when you're building a booking and the guest remembers a stop
> halfway through, just hit Add Another Leg and type it. You no longer have to
> delete the later leg and re-type it to get the numbering right.
>
> 1. Add the leg wherever you are in the call — it appears at the bottom like before.
> 2. Fill in its date and time. The moment you click away from that card, it
>    slides up into its place in the day and the leg numbers update.
> 3. Carry on. The review screen, the price and the saved trip all read in
>    pickup order.
>
> A card only moves once it has both a date and a time, and never while you're
> still typing in it. Cards you haven't dated yet stay where they are.
>
> Nothing else on the booking screens changed — same fields, same flight
> check, same "Fill Reverse Of Leg 1" button. "Leg 1" now always means the
> earliest pickup.

---

## Behind the scenes

**Where it lives:** the Trip Details step of the dispatcher booking wizard
(the step with the leg cards and the Add Another Leg button).

**Why:** legs were kept in the order they were typed. Adding a middle stop
after the return put it last, and the only fix was to remove the return and
build it again. Two things changed: the page re-sorts the cards by pickup date
and time when the cursor leaves a card, and the server sorts once more when
the step is submitted, so the saved order is right even without the page's
help. Flights stay attached to their own leg through the sort.

**Expect to be asked:**
- *"My card jumped — did I lose what I typed?"* No. The card moved with
  everything in it; look for the one with the blue "moved into pickup order"
  note in its header.
- *"Can I put a leg out of time order on purpose?"* No. Legs always run in the
  order the day happens. If two pickups are at the same minute they stay in
  the order they were typed.
- *"Does this touch existing reservations?"* No. Only the new-booking wizard.
  Adding a leg to a trip that already exists is unchanged.
