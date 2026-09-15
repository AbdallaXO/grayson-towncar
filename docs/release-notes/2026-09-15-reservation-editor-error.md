---
date: 2026-09-15
audience: Dispatchers
title: Editing a reservation no longer throws an error page for some customers
---

# Editing a reservation no longer throws an error page for some customers

## Send this to the team

> Hey team — the error page some of you hit when saving a reservation edit is
> fixed. If you opened a booking, changed something and got a server error
> instead of "updated successfully", try it again — it saves now.
>
> It only ever happened on a handful of customers, so most of you never saw it.
> Nothing was lost when it happened: the edit simply didn't save.
>
> Nothing else about editing changed. Same screen, same buttons, same fields.
> A guest who books under the same email and phone as a family member still
> stays their own separate customer — that hasn't changed and shouldn't.

---

## Behind the scenes

**Where it lives:** the reservation editor, on save.

**Why:** the save looked up the customer by name, email, phone and zip and
expected to find exactly one row. For 34 customers there were two identical
rows in the table, so the lookup threw and the whole page came back a 500. It
now takes the oldest matching row and carries on — duplicates are not a reason
to refuse an edit.

**What was deliberately NOT changed:** the lookup still matches on all five
fields rather than just email and phone. A household books on one email and one
phone under different passenger names — 398 email/phone pairs in the system look
like that, one of them carrying three different people. Matching on email and
phone alone would have merged them into one customer and rewritten the name on
their past trips. There is a test that fails if anyone tries.

**Still true, and worth a decision later:** correcting a customer's name in the
editor creates a new customer record rather than renaming the existing one. That
is where most of the duplicate rows came from ("Jef Howard" then "Jeff Howard",
or a zip going from 40291 to 40291-5037). It is the safe behaviour, not
obviously the right one, and changing it would rewrite names across a customer's
whole history — so it stays as is until someone decides otherwise.

**Expect to be asked:**
- *"Did my edit go through the time I saw the error?"* — No. Nothing saved.
  Reopen the booking and make the change again.
- *"Why are there two of this customer?"* — Two rows got created at some point
  with identical details. The editor now just uses the older one. Nothing was
  merged or deleted.
