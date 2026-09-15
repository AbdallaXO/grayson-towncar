---
date: 2026-09-15
audience: Dispatchers
title: The weekly inspection round — a few cars a day, the whole fleet by Sunday
---

# The weekly inspection round — a few cars a day, the whole fleet by Sunday

## Send this to the team

> Hey team — Fleet has an **Inspections** screen.
>
> 1. Open **Fleet** → **Inspections**. The top says how many cars have been
>    walked this week, out of the whole fleet.
> 2. **Do these today** suggests five. It puts the cars that are sitting still
>    first, because a car out on a run all day can't be looked at. Pick a
>    different one any time — every car still due is on the page.
> 3. Tap a car, run the checklist — **Good**, **Problem** or **N/A** on each
>    item, with a note and a photo wherever you want one — then save.
>
> Tick **I found something** and it files a problem report for that car, the
> same one Dispatch already sees.
>
> It resets every Monday on its own. A car in the shop all week isn't counted
> against you. Finding something does **not** take the car off the road — that
> is still **Take off road** on the Desk, same as always.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet** → **Inspections**.

**Why:** there was no record anywhere of a car being looked at. The Report could
tell you what broke, never whether anyone had checked. Five a day across
nineteen active units means the whole fleet is walked every week with room to
fall a day behind and still finish.

**How the reset works:** an inspection is stamped with the Monday of its week,
and a car can only have one per week. Asking about a different week is what
makes the round empty again — there is no job to run, nothing to clear down, and
last week's record is kept rather than overwritten.

**The suggestion** reads the same engine as **The day**: no chauffeur today
first, then the cars with a genuinely usable gap, then whoever has gone longest
without being looked at. A car in the shop all week is marked as such and does
not count as missed.

**The odometer field** is optional for most of the fleet, and the only mileage
the system will ever get for the two units with no tracker fitted — which is
what makes their service intervals work at all.

**Changing the checklist:** the items live in one list at the top of
`dispatching/fleet_inspection.py`, in plain English, and can be added to,
reworded or removed without a migration. Old inspections keep their answers even
after an item is dropped.

**Expect to be asked:**
- *"I walked a car twice this week."* — The second save updates the first
  record; it never makes two, and the count can't double.
- *"It suggested a car that's out all day."* — It falls back to that only when
  everything easier is already done, or when the day isn't built yet.
- *"Do I have to fill in every item?"* — No. What you leave blank simply isn't
  recorded.
