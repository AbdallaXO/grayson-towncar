---
date: 2026-09-21
audience: Dispatchers
title: This morning's stray payment reminders and duplicate texts came from a laptop, and that can't happen again
---

# This morning's stray payment reminders and duplicate texts came from a laptop, and that can't happen again

## Send this to the team

> Hey team — if a guest says they got a "finalize your reservation" email this
> morning after they had already paid, or two copies of the same follow-up text,
> here is what happened. A copy of the app running on a laptop, with a week-old
> copy of the bookings, sent the automatic emails and texts as if it were the
> real system. 48 payment reminder emails went out that way since the 15th, 14
> of them to guests who had already paid, and 151 follow-up texts this morning,
> most of them duplicates of texts the real system also sent.
>
> Their bookings and payments are all fine. Nothing was charged, cancelled or
> changed. If a guest asks, tell them their payment is on file and the email
> was sent in error, and log it on the reservation.
>
> That laptop has been stopped, and the app now refuses to send anything
> automatic unless it is the real production system, no matter what settings
> it is given. Nothing about the real reminders or texts changed.

---

## Behind the scenes

**Where it lives:** `settings.OUTBOUND_AUTOMATION_ENABLED`, on in production
(Railway) and in tests, off anywhere else. Checked in the app hooks that start
the schedulers, in the scheduler threads themselves, in the batch they run, in
the reminder engine, in the dedicated `run_schedulers` command, and in the two
senders every automated message passes through: `GoHighLevelService.send_sms`
and `send_payment_reminder(automated=True)`. Dispatcher-triggered sends by hand
are not affected. `OUTBOUND_AUTOMATION=1` forces it on for a local run that
knows what it is doing.

**Why:** the `.env` on the founder's Mac carries the real mail and GoHighLevel
credentials, and `manage.py runserver` starts the same background schedulers
production runs. Against the local snapshot of the database (pulled 14 Sept,
refreshed 21 Sept) those schedulers acted on stale facts: Mary Tomasso
(reservation 18593) had paid on the 19th in production but was still unpaid in
the copy, so the laptop emailed her a first reminder at 9:38 AM on the 21st.
The full list of what the laptop sent is in the session scratchpad
(`laptop_emails_after_sep15.csv`) and in the backed-up local database
`content/db_backup_20260921_120442.sqlite3`, table `ops_emaillog`, which the
production log does not have.

**Expect to be asked:**
- *"Do I need to do anything for these guests?"* Only if they ask. Their
  records are correct. The 14 who had paid are listed in the CSV above.
- *"Can I still run the app on my machine?"* Yes. It will no longer send
  texts or reminder emails, poll Samsara, or run the wake-up sweep. Booking
  confirmations and anything a person triggers still go where `.env` points
  them, so use the console email backend locally.
