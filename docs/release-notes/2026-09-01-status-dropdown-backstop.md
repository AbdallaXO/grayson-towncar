---
date: 2026-09-01
audience: Chauffeurs
title: The status dropdown double-checks before moving a trip backwards
---

# The status dropdown double-checks before moving a trip backwards

## Send this to the team

> Drivers — small safety net on the trip status control. The dropdown was easy
> to nudge one step backwards by accident on a phone (On location slipping back
> to On the way, Picked up slipping back to On location), and it saved the
> change instantly.
>
> Now, picking an EARLIER status pops a quick "are you sure?" first. Tap OK and
> it moves back — going back on purpose still works exactly as before, for
> fixing a mis-tap. Tap Cancel and nothing changes.
>
> The big forward buttons are untouched: one tap, same as always. Nothing
> changes for dispatch either.

---

## Behind the scenes

**Where it lives:** the trip status dropdown in the driver app (day view and
weekly schedule).

**Why:** two weeks of status history showed about twenty cases of a driver's
own status going exactly one step backwards within seconds of going forward —
the phone's picker wheel slipping a notch and firing instantly. Dispatch-made
resets (a reassigned trip going back to the start) are deliberate and unchanged.

**Expect to be asked:**
- "Did my old statuses get messed up?" — nothing historical was touched; this
  only adds a confirmation going forward.
- "Can I still correct a wrong tap?" — yes, same dropdown, just confirm it.
