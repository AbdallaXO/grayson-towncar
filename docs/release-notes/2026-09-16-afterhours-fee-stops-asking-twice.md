---
date: 2026-09-16
audience: Dispatchers
title: The after-hours fee stops asking you twice
---

# The after-hours fee stops asking you twice

## Send this to the team

> Hey team — the $20 after-hours flag has been asking you to collect money the
> guest had already paid. That's fixed, and there are two changes you'll see.
>
> **When you book a late trip**, the pricing screen now asks one question: is the
> $20 in this price, yes or no? Answer it and nobody gets asked about that trip
> again. That question is the whole fix — you're the only one who knows whether a
> price you quoted has the fee inside it, and before now there was nowhere to say.
>
> **On a trip card**, there's a new **"Already collected — don't ask again"** button
> next to Charge. Press it when the $20 came in some other way — on the balance
> payment, in cash, or because it was in the quoted price. It asks how, then it's
> done for good.
>
> That "for good" is the real change. Until now, closing one of these didn't stick —
> the same trip could come back a day later and ask again. One trip asked three
> times and two of you closed it. That won't happen any more.
>
> Nothing changed about charging. Same button, same $20, same email to the guest.
> And a genuinely unpaid fee still flags — we're not hiding those.

---

## Behind the scenes

**Where it lives:** the after-hours flag on the trip card, and the task that comes
with it.

**Why:** `afterhours_fee` on the leg was meant to record "this $20 is collected",
but it was a second copy of something the money already said, and it drifted —
legs were created with it at zero even when the booking itemised the fee, and a
reservation edit could reset one that was set. So the flag asked again. Across the
history: 49 tasks raised against 16 trips that provably paid the standard rate plus
$20, 28 of which a dispatcher had to open and close. Nobody double-charged — every
one of them correctly declined to collect.

The fix is three parts. The pricing screen now records the dispatcher's own answer
at booking, which is the only reliable source — 85 of the 123 late trips in the last
60 days that would still have flagged are `direct` bookings priced by hand. The flag
also accepts the booking's own extra charges as proof (112 of 374 past tasks would
never have been raised). And settling one writes it to the leg, so it sticks —
previously closing a task only set a status, which is why they came back.

**Expect to be asked:**
- *"Did anyone get charged twice?"* No. Checked every after-hours payment in the
  system — no reservation was ever charged the fee more than once.
- *"What if I press 'already collected' by mistake?"* It's recorded on the trip's
  notes with your name and what you typed, so it can be read back and undone.
- *"Will it still catch a fee we actually missed?"* Yes. 262 of the 374 still
  flag, because there's no after-hours money on those bookings. Those are the real
  ones.
