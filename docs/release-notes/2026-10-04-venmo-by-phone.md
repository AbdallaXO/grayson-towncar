---
date: 2026-10-04
audience: Dispatchers
title: The PayPal & Venmo file now uploads cleanly
---

# The PayPal & Venmo file now uploads cleanly

## Send this to the team

> Hey team — PayPal was rejecting Venmo rows in the payout file ("Receiver is invalid"). That's fixed. Download a fresh file and it uploads cleanly.
>
> PayPal can only pay Venmo by phone number or email, not by @handle. So for agents who gave us a Venmo @handle, the file now uses the phone number on their profile. The batch page shows their @handle next to it so you can see who's who.
>
> If an agent has a @handle but no phone on their profile, they're listed under "Left out" until someone adds it.
>
> Nothing else changed. PayPal rows always worked, and "Mark these paid" still only marks what's in the file.

---

## Behind the scenes

**Where it lives:** Affiliate Management → **PayPal & Venmo batch**.

**Why:** The first real upload on Oct 4 failed on all 58 Venmo @handle rows. PayPal's upload accepts only a US mobile number or email for Venmo; @handles work only through their API.

**Expect to be asked:**
- *"What if the phone isn't on their Venmo?"* — Venmo texts that number a link to claim the money. If nobody claims it in 30 days, it comes back to us. The number is the agent's own, from their profile.
- *"Did anyone get paid twice?"* — No. PayPal refused the whole file, so nothing was sent.
