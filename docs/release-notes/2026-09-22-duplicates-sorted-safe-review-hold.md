---
date: 2026-09-22
audience: Dispatchers
title: Duplicate bookings are sorted into safe, check first and hold, and can be deleted in one go
---

# Duplicate bookings are sorted into safe, check first and hold, and can be deleted in one go

## Send this to the team

> Hey team — the Duplicate Reservations page now tells you which unpaid twins
> are safe to delete and which ones need a look.
>
> 1. Open Duplicate Reservations. Every unpaid twin has a tag: **Safe to
>    delete**, **Check first**, or **Hold**. The "Why" column says what it saw.
> 2. Click **Select all safe**, then **Delete selected**. Tick any other row you
>    have checked yourself to add it.
> 3. "Check first" means the route or vehicle differs from the paid booking.
>    "Hold" means it may be a real second trip: an extra pickup day, a driver on
>    it, staff already spoke to the guest, or it was booked by us, not the guest.
>
> Ops Control no longer files a "Possible duplicate" task. The page is the place.
>
> What did not change: the paid booking is never touched, a booking that got paid
> since you opened the page is refused, and reminders to unpaid guests still pause
> while they look like a duplicate.

---

## Behind the scenes

**Where it lives:** Duplicate Reservations (superuser only), and the Ops Control
queue no longer receives "Possible duplicate reservation" tasks.

**Why:** the old page listed every paid/unpaid pair as a "Dupe" with one Delete
All button, but the same-name-same-phone-same-day key is not proof: over six
months, 36 groups on that key had two or more *paid* live bookings (second
vehicle, separate return, spouses sharing a phone). On the 2026-09-21 snapshot
the 158 unpaid rows split 99 safe / 25 check first / 34 hold. The verdicts are
in `reservations/duplicates.py` and are re-derived on the server at delete time.
The "Possible duplicate" tasks (four open at the time) were never worked and are
closed by migration.

**Expect to be asked:**
- *"Why is this one on Hold when it's obviously a duplicate?"* Read the Why
  column. If you agree it's a duplicate anyway, tick it and delete it; Hold only
  means the page won't pre-select it for you.
- *"It said 'no paid twin any more' when I deleted."* The paid booking was
  cancelled or changed after the page loaded. Refresh and look again.
- *"A guest stopped getting payment reminders."* While a booking looks like a
  duplicate, reminders pause. Once the twin is deleted, they resume on the next
  30-minute check by themselves.
