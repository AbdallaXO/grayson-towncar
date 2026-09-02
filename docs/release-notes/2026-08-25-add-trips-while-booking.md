---
date: 2026-08-25
audience: Dispatchers
title: Add and remove trips while you're taking the booking
---

# Add and remove trips while you're taking the booking

## Send this to the team

> Hey team — you no longer pick "one way / round trip / multiple trips" at the start of a booking. You just add trips as the guest tells you about them.
>
> 1. Start a new booking — it opens straight on the guest's details now.
> 2. Fill in the guest and passenger info like always.
> 3. On Trip Details you get one trip to fill in. Need a return? Hit **Add another leg**. Need three? Hit it again. Added one by mistake — hit **Remove** on that one.
>
> The system works out whether it's a one-way, a round trip, or a multi-stop from how many you ended up with, so the price suggestion and everything after it behave exactly as before.
>
> Nothing else about booking changed — same guest info, same pricing screen, same review-and-confirm at the end. Five steps now instead of six.

---

## Behind the scenes

**Where it lives:** New Booking → the Trip Details step

**Why:** On a live call you don't know the trip count until you're already deep into the details — the old first step made you guess, and guessing wrong meant starting over. It also quietly fixes a long-standing annoyance: a round trip used to render a third, empty trip card that nobody asked for.

**Expect to be asked:**
- *"Where did the trip type screen go?"* — Gone on purpose. Booking now starts on the guest's details, and the trip type is worked out from how many trips you enter.
- *"How many trips can I add?"* — Up to five, same limit as before.
- *"Does removing a trip delete anything?"* — No. Nothing is saved until you confirm at the end, so removing a card just takes it off the screen.
