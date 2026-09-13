---
date: 2026-09-13
audience: Dispatchers
title: A farmed-out job reads right and pastes right
---

# A farmed-out job reads right and pastes right

## Send this to the team

> Hey team — three fixes to what a partner company sees when we farm a job out.
>
> Car seats no longer split into a confusing second line. It used to read "1
> Booster, 1 Extra Booster", which looks like it might be one seat or two. Now it
> just says how many of each to bring: "1 Rear-Facing, 3 Booster". Same seats,
> counted once, no guessing.
>
> Gratuity now shows the amount for that one job, not the whole booking's tip and
> percentage. A partner running one leg of a round trip was seeing the total for
> both.
>
> And the paste is fixed. Copy whole job was coming out as code — %20 and %0A
> instead of spaces and line breaks. It pastes as clean text now.
>
> Nothing changed for our own chauffeurs, and nothing changed about how you book,
> price or assign anything. Guest notes, flight, pickup, bags all read the same.

---

## Behind the scenes

**Where it lives:** the partner (operator) portal — the job card, the response
queue, and Copy whole job.

**Why, one at a time:**

Car seats — the portal was showing our internal split between the seats included
in a booking and the "extra" ones added on top. That distinction is ours, not
theirs, and a partner who reads it as one seat instead of two puts a child in a
car without one. The extras are now added into their own kind, so the number on
the page is the number to bring.

Gratuity — two different gratuity notes get stamped on a booking: a whole-trip
line on the reservation ("20% Gratuity Included ($90.00)") and a per-leg split on
each leg ("$45.00 Gratuity Included"). The portal was showing the first. It now
shows the leg's own, and the reservation's line is stripped — it spans every leg
and quotes our pricing, neither of which belongs in front of a partner.

The paste — every line is "Word: value", and a block that STARTS with one parses
as a web address, because "Confirmation:" is a valid scheme. Paste targets that
sniff for a link then escape everything after it, which is where the %20s came
from. The block now opens with a plain title line, so there is nothing to
mistake for a link.

**Expect to be asked:**
- "Did we change how many car seats are on the job?" No. Only how they're
  written out. The count was always right; it just read as two separate things.
- "Can partners see what we charge now?" No — less than before. The reservation
  gratuity line was the only place our pricing appeared, and it's gone.
- "Which copy button?" All of them, including copy-the-whole-day.
