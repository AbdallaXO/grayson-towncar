---
date: 2026-09-12
audience: Dispatchers
title: The capacity planner shows every Publix stop and car seat again
---

# The capacity planner shows every Publix stop and car seat again

## Send this to the team

> Hey team — the Daily Capacity Planner's driver timeline was quietly dropping
> two things: the little grocery-cart Publix badge, and car seats, on some
> trips that actually had them. That's fixed — both show up on every trip that
> needs them now.
>
> Also: "Use Previous Day" on the vehicle assignments board could look like it
> did nothing right after you used it — the planner could still show the old
> lineup for a few seconds after the page reloaded. It now updates immediately.
>
> Nothing else changed. Same board, same drag-and-drop, same buttons.

---

## Behind the scenes

**Where it lives:** the Daily Capacity Planner's driver timeline (the bars next
to each driver's name), and the "Use Previous Day" copy button on the Vehicle
Assignments panel.

**Why:** the timeline badges were built from a separate, older calculation than
the one the rest of the app uses — it only flagged a Publix stop on a plain
airport arrival, so a cruise-transfer pickup or a reservation with an odd leg
order could lose the badge. Car seats had the same gap: extra seats and
boosters, and trips flagged "needs a seat" with no count yet, weren't being
counted. Separately, copying vehicle assignments forward from the previous day
never told the planner's cache to refresh, so a reload right after copying
could still show the stale board for up to a minute.

**Expect to be asked:**
- "Was a Publix stop or car seat actually missing before, or just not shown?"
  Just not shown — the trip itself was correct, this only affected the badge
  on the planner's timeline.
- "Do I need to redo any copies I already did?" No — this only affects how
  fast the planner reflects a copy, not what got copied.
