---
date: 2026-09-21
audience: Dispatchers
title: No more "QUOTE NEEDED" tasks. The price is on the GoHighLevel card when the guest replies
---

# No more "QUOTE NEEDED" tasks. The price is on the GoHighLevel card when the guest replies

## Send this to the team

> Hey team — the "QUOTE NEEDED" tasks in Ops Control are gone. They were a
> second place to look at a conversation you were already handling in
> GoHighLevel.
>
> Here is how a custom-route quote request works now:
>
> 1. The guest asks the website for a price on a route we have no online rate
>    for. They get the usual automatic text, and the reply lands in GoHighLevel
>    like any other.
> 2. Before they even reply, a note is on their contact card: "Suggested price:
>    $185 — one-way SUV, Sanford to MCO on Nov 29 (custom estimate, 41 mi, 52
>    min)". That is the quote calculator's number for their exact trip.
> 3. When they reply, send that price, or adjust it if you know better, from
>    GoHighLevel. Nothing else to close.
>
> If the note says "No suggested price", the calculator could not price the
> addresses. Open the quote calculator and price it by hand.
>
> The handful of QUOTE NEEDED tasks still open in Ops Control can be completed
> as you finish them. No new ones will appear.

---

## Behind the scenes

**Where it lives:** the website quote form, the GoHighLevel contact notes, and
the lead's activity log (visible on the leads board detail).

**Why:** founder decision, 21 September. Measured since mid-July: 421 quote
tasks, worked a median of ten hours after the guest asked, while the guest had
already replied to the automatic text in GoHighLevel a quarter of the time. The
task added a screen without adding information. The one thing the person in
GoHighLevel lacked was the price, so the engine now supplies that where they
are looking.

**Mechanics:** on a no-rate request the form schedules `price_lead` in the
background. It prices the route with the same engine as the quote calculator,
writes a "Suggested price" line to the lead's activity log with the breakdown,
waits up to 30 seconds for the contact to exist in GoHighLevel, and posts an
internal note on it. The website's own quote price on the lead stays empty, so
the follow-up texts that quote a price still skip these guests and the quote
email does not show a number nobody approved.

**Expect to be asked:**
- *"Is the suggested price the real price?"* It is the quote calculator's
  number, the same one you would get by typing the trip in yourself. Use your
  judgement as before. It is not shown to the guest anywhere.
- *"Where did the task page go?"* It still opens for the tasks that were
  already filed. New requests do not make one.
