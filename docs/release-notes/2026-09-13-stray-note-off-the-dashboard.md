---
date: 2026-09-13
audience: Dispatchers
title: The stray note above the driver dropdown is gone
---

# The stray note above the driver dropdown is gone

## Send this to the team

> Hey team — that line of developer text that was showing up on the legs
> dashboard, just above the driver dropdown on a trip, is gone. It was never
> meant to be on the screen.
>
> Two more like it were hiding elsewhere: one on the driver pay rates page, above
> the "places with no zone" warning, and one on the vehicle roster. All cleared.
>
> Nothing else moved. Same trips, same dropdowns, same buttons — you're just not
> reading our notes to ourselves any more.

---

## Behind the scenes

**Where it lives:** the legs dashboard (above each trip's driver dropdown, and
again near the driver roster block), and the driver pay rates page.

**Why:** Django's short comment form is single-line only — its parser runs
without the newline flag, so the moment a comment wraps onto a second line it
stops being a comment and gets printed to the page as text. Four of them had
wrapped. They're now the multi-line comment tag instead, which does not leak. I
swept every template in the project; those four were all of them.

**Expect to be asked:**
- "Was anything broken, or just ugly?" Just ugly — it was a comment, not a
  setting. Nothing behaved differently.
- "Could there be more?" No. The whole template tree was checked, and it's clean.
