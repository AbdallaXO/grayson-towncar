---
date: 2026-10-04
audience: Dispatchers
title: Edit a travel agent right on their profile
---

# Edit a travel agent right on their profile

## Send this to the team

> Hey team — you can now edit a travel agent right on their profile. No more going into the admin.
>
> 1. Open the agent's profile and click **Edit profile**, or **Change** next to how they get paid.
> 2. Fix their name, email, phone, rate, payment method or handle, then **Save changes**.
>
> If what you saved means we can't pay them (no method, or a handle PayPal can't use), it tells you right there.
>
> Also: **Agent View** on the profile opens again instead of saying Forbidden. And the **Agency links** page has a search box, plus a column showing what each agent picked, so you can search "Best Day" and link that agency's agents together.
>
> Nothing changed about who gets paid or how much.

---

## Behind the scenes

**Where it lives:** any agent's profile (Affiliate Management → click a name). Agency links is on Affiliate Management.

**Why:** Fixing a payment handle meant a trip to the Django admin, and Agent View refused staff because only the agent or their agency head could open it.

**Expect to be asked:**
- *"Who changed this agent's Venmo?"* — Every field changed here is recorded with who changed it and what it was before.
- *"I searched and clicked Link. Did it link the ones I couldn't see?"* — No. Link only covers the rows on screen.
- *"Can I still use the admin?"* — Yes. It just isn't needed for these fields anymore.
