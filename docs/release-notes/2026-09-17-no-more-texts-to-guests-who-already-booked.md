---
date: 2026-09-17
audience: Dispatchers
title: We stop texting people who have already booked
---

# We stop texting people who have already booked

## Send this to the team

> Hey team — the follow-up texts will no longer chase someone who has already
> booked the trip they're being asked about.
>
> You'll notice it as fewer confused replies on the leads board. Up to now, if a
> guest booked a round trip, or booked under a different email from the one on
> their enquiry, the system often didn't connect the two — so it kept texting
> "still looking for a ride?" to someone who had already paid. One guest got that
> on the morning of her trip, while her driver was on the way.
>
> Now, before any follow-up goes out, we check for a real booking on that person's
> phone or email for that same date. If there is one, the whole sequence stops.
>
> Nothing changed about the texts themselves, and a genuine new enquiry from a past
> customer still gets followed up as normal.

---

## Behind the scenes

**Where it lives:** the follow-up sequence behind the leads board, and the
pre-pickup nudge.

**Why:** the sequence only stopped when *that particular lead* was marked
converted, and conversion marks exactly one lead per booking. Round-trip quotes
create twin leads, a lead created after the booking can never be matched, and a
booking under a spouse's or agent's email shares only a phone number. 248 messages
reached 145 people who had already paid; 112 of them replied, and every reply
landed on the board for someone to untangle.

The new check looks for a **Reservation**, not a converted lead, matched on email
or phone and scoped to the lead's own pickup date. The date scoping is deliberate —
without it, any past customer making a genuine new enquiry would be silenced.

**Expect to be asked:**
- *"Will it stop following up real enquiries?"* No. It only suppresses when there's
  a live booking for that person on that same date.
- *"What about the ones already queued?"* Cleared. Three were still armed, all for
  the same guest.
- *"Does this mark the lead as converted?"* No — it only stops the messages. Linking
  the lead to the booking is still the "recheck conversions" action.
