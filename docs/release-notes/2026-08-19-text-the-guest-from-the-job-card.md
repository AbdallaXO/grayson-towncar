---
date: 2026-08-19
audience: Both
title: Text the guest from the job card
---

# Text the guest from the job card

## Send this to the team

> Hey team — chauffeurs can now text the guest straight from the driver app,
> without typing a word.
>
> Every job card has three new buttons: **On the way**, **On location**, and
> **Review**.
>
> 1. Tap one when you reach that point in the trip.
> 2. Your own texting app opens with the message already written.
> 3. Read it, hit send.
>
> The wording matches the job on its own — an airport arrival names the flight and
> tells the guest you'll meet them inside at baggage claim, a cruise pickup names
> the terminal. Once you've tapped one it turns green with a tick, so you can see
> at a glance what's already gone out.
>
> Nothing sends by itself. Nothing leaves your phone unless you hit send, and the
> guest's reply comes back to you, not to the office.

---

## Behind the scenes

**Where it lives:** the driver app — both today's job list and the weekly schedule.
Admins can see how it's being used under **Analytics → Guest Communication**
(superusers only — dispatchers don't deal with KPIs). Per-chauffeur rates also
appear on the driver profile, and the raw log is in Django admin under
**Client Messages Sent**.

**Why:** guests were being left in the dark between booking and pickup, and the
texts drivers did send varied wildly in tone. This gives every chauffeur the right
words for the moment, in one tap, without the company sending anything on their
behalf.

**Expect to be asked:**
- *"Did the guest get it?"* — we can't tell. We log that the driver **tapped** the
  button, not that the text was delivered. A green tick means "he opened the message
  ready to send", not "the guest read it."
- *"Why don't I see the buttons on this job?"* — affiliate and operator jobs don't
  get them, and neither does a job the system can't classify. Those cards look
  exactly as they always did.
- *"Can I change the words?"* — yes, but not on the phone. The wording is fixed so
  every guest hears the same voice; edits go through the office.
- *"Does it tell the guest to wait at the curb?"* — no. Airport texts always say the
  chauffeur meets them inside. Don't let anyone reword that.
