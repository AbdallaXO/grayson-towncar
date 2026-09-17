---
date: 2026-09-17
audience: Dispatchers
title: Agency "total paid" now matches what we actually paid
---

# Agency "total paid" now matches what we actually paid

## Send this to the team

> Hey team — heads up, because a number on screen is about to get smaller.
>
> The "total paid commission" on an agency's page has been showing roughly double
> the money we actually sent them. It was counting every agency payout twice. That
> figure is now corrected across all agencies, so some will drop by half.
>
> Nothing was overpaid or underpaid — the payouts themselves were always right, and
> so were the individual agent totals. It was only this one summary figure on the
> agency page and the Affiliate Explorer that was wrong.
>
> If an agency has queried their statement recently and the number looked too high,
> that's why. The corrected figure is the one to quote from now on.
>
> Nothing changed about how payouts are run or what anyone gets paid.

---

## Behind the scenes

**Where it lives:** the agency detail page and the Affiliate Explorer — the
"total paid commission" figure.

**Why:** the amount was added twice for every agency payout. Creating the payout
fires a handler that adds it to the agency, and both callers then added the same
amount again to the very object that handler had just updated. 22 of 50 agencies
carried the doubled figure, $14,170.74 in total — nine agencies whose entire
history is a single payout were storing exactly 2× that one payout.

Both call sites now recompute from the payout rows instead of adding, which is also
self-healing: an agency that drifts for any other reason corrects itself on its next
payout. `manage.py resync_agency_commission_totals` fixes the existing rows, and
has a `--dry-run` that writes nothing.

**Expect to be asked:**
- *"Did an agency get shortchanged?"* No. The payout records were always correct —
  only this display figure was wrong, and nothing pays off it.
- *"Why didn't agent totals have this?"* They did, but the agent path already
  recalculated from the payout rows afterwards, which quietly corrected it.
