---
date: 2026-09-27
audience: Both
title: A new driver's welcome flow asks for their email up front, and insists on last name and home address
---

# A new driver's welcome flow asks for their email up front, and insists on last name and home address

## Send this to the team

> Hey team — small change to the new-driver welcome link.
>
> 1. The page the link opens now has an email box next to the username and password. Drivers can fill it in right there.
> 2. On My Details, right after, they have to give their last name and home address before it saves, along with their mobile. Then it sends them on to photograph their permit, same as before.
>
> The idea is to collect everything while they've got the phone in hand and are expecting to fill things in, instead of chasing them for an address weeks later.
>
> The license photo is still asked for on My Details but it never stops them finishing, and neither does a blank email. Nothing changed for drivers who already have a login. My Details still lets them fix a phone number without filling in anything else.

---

## Behind the scenes

**Where it lives:** the welcome link page, and Driver app → My Details when reached from a welcome link.

**Why:** The Onboarding checklist on a driver's profile kept showing "Home address: missing" for weeks after a driver started, because the welcome flow let them skip it. Email is asked twice (welcome page, then My Details) but never forced, so a driver without one can still get in; nothing on either page says "optional", because saying so is an invitation to skip it.

**Expect to be asked:**
- *"Can I skip the address for someone?"* — Not from the welcome link. Fill it in yourself on their profile and they won't be asked.
- *"They didn't add a license photo."* — That's allowed. It shows as missing on the Onboarding checklist and they can add it any time from My Documents.
