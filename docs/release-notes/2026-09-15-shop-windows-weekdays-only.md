---
date: 2026-09-15
audience: Dispatchers
title: The shop-window finder stops offering Saturdays and Sundays
---

# The shop-window finder stops offering Saturdays and Sundays

## Send this to the team

> Hey team — **When can I take a car down?** no longer offers weekend windows.
> The shops are shut, so a Saturday slot was never a real slot.
>
> Saturday and Sunday still appear on the grid with their trip numbers — worth
> seeing, since Saturday is our busiest day — but the squares are greyed out,
> can't be picked, and the day reads **Shop closed**. Every recommendation and
> every "next best" window is now Monday to Friday.
>
> This only changes what the finder *suggests*. Taking a car off the road is
> untouched: if something breaks on a Sunday you record it exactly as before,
> and a Friday shop visit that runs into the weekend is still fine.

---

## Behind the scenes

**Where it lives:** the **When can I take a car down?** panel on the Fleet desk,
and the full **Outlook**.

**Why:** the finder ranks windows by what taking that car out would cost
dispatch — and by that measure Saturday looked ideal. For a unit the weekend
doesn't need, Saturday is the emptiest-looking row on the grid, while being the
day the fleet actually works hardest. So it kept recommending a day no shop would
have taken the car. The Phase 3 audit named this specifically.

**What it does not do:** touch the downtime ledger. A weekend breakdown must
stay recordable, and a booked visit legitimately spans a weekend. The rule
governs offers, not records.

**Where the rule lives:** one tuple, `SHOP_WEEKDAYS = (0, 1, 2, 3, 4)`, in
`dispatching/fleet_windows.py`. If a yard ever starts opening Saturdays that is
the only edit, and the grid, the ranking, the tooltips and the Outlook all follow
from it. Four tests pin the behaviour.

**Expect to be asked:**
- *"Why can I still see Saturday's numbers?"* — Because knowing Saturday runs 40
  trips against 19 cars is exactly what makes a Tuesday window the obvious
  choice. Hiding it would remove the reason.
- *"What if our shop does open Saturday?"* — Tell whoever maintains the system;
  it is a one-line change, not a rebuild.
