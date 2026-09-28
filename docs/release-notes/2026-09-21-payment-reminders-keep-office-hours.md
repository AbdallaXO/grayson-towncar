---
date: 2026-09-21
audience: Dispatchers
title: Payment reminder emails keep office hours, and the second one waits a day
---

# Payment reminder emails keep office hours, and the second one waits a day

## Send this to the team

> Hey team — two changes to the automatic "your reservation is still unpaid"
> emails, after a guest got two of them six hours apart with one at 2:20 AM.
>
> 1. They only go out between 8 AM and 9 PM Eastern now, same as the lead
>    texts. One that falls due overnight simply goes in the morning.
> 2. The second reminder goes a full day after the first one, not a day after
>    the booking. For a trip booked weeks ago, that used to mean both landed
>    the same night.
>
> Also fixed: a paid reservation could show as unpaid on the board and in the
> reports right after the guest paid. That was a display flag, not the money.
> Nobody who paid was sent a reminder because of it, and the guest's payment
> was always recorded. If a guest says they got a reminder after paying, check
> the date on the email they are looking at before anything else: in the case
> that raised this, the last reminder went thirteen hours before she paid.
>
> What did not change: the reminders themselves, the 3-day and 24-hour
> warnings, and the cancel link in them.

---

## Behind the scenes

**Where it lives:** the automatic unpaid-reminder engine (`ops/unpaid_reminders.py`)
that runs every scheduler cycle, and the payment signals that keep a
reservation's paid columns in step with its payments.

**Why:** Mary Tomasso, reservation 18593, booked 3 August for 3 October and
paid 19 September. Her booking entered the engine's 14-day window on the 18th,
so the "2 hours after booking" and "24 hours after booking" stages were both
overdue at once. The first went at 8:14 PM, the second six hours later at 2:20 AM
(the minimum gap), and she paid that afternoon. Nothing was sent to her after
she paid. Measured over 1 to 21 September: 93 reminders went at 2 AM Eastern and
118 at 8 PM; 57 bookings got the first two reminders less than 8 hours apart.

The paid flag: the Stripe webhook saves the payment (the signal sets the flag
true) and then saves its stale copy of the reservation, writing the flag false
again. 2,406 of the 2,415 reservations paid since 1 September were flipped back
within ten seconds. A `pre_save` hook now recomputes the paid columns from the
payments before any reservation row is written. The reminder engine already
re-checked payment status in Python, which is why no paid guest was emailed.

**After deploy:** run `python manage.py backfill_paid_state --dry-run` and then
without the flag, to correct the flag on the reservations already affected. The
revenue KPI views query that column.

**Expect to be asked:**
- *"A guest says they got a reminder after paying."* Check the email's date and
  the reservation's reminder log before anything else. Our log records every
  automated reminder; since 1 September none went out after a payment existed.
- *"Why did an overnight booking's first reminder come at 8 AM instead of 2 hours
  later?"* Quiet hours. It is deferred, not skipped.
