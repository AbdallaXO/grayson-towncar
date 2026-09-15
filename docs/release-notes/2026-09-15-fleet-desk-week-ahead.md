---
date: 2026-09-15
audience: Dispatchers
title: The Fleet desk shows the week's inspection round and what's coming
---

# The Fleet desk shows the week's inspection round and what's coming

## Send this to the team

> Hey team — two additions to the **Fleet** desk, plus one button that was
> broken.
>
> 1. **This week's round** now sits beside Paperwork: how many cars have been
>    walked, how many are left, and roughly how many a day finishes the week.
>    The bar goes amber if you're behind and red if Saturday arrives with cars
>    still due.
> 2. **Coming up**, below the Do-now list, is the stuff that isn't today's
>    problem but shouldn't surprise you — a battery drifting low, a date
>    creeping closer. It's deliberately quiet.
> 3. On a car whose tracker has gone silent, the **Check the tracker** button
>    was showing up as a blank black rectangle. It has text now.
>
> Nothing moved. Do now, Paperwork and the shop-window finder are all exactly
> where they were, and none of this changes what a car does or doesn't do.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet**.

**Why the round:** the inspection screen has existed since this morning and the
desk never mentioned it. On a Tuesday with zero of nineteen cars walked, the
fleet's largest recurring obligation was reachable only by remembering to click
a tab. It is now on the page he actually lives on.

**Why "Coming up":** the desk has been computing a full Now / This week / Later
breakdown on every single page load since the module shipped, and rendering none
of it — the component that would have displayed it had no references anywhere.

It is not shown raw. Those groups are per-CAR facts, and today's "this week"
group is seventeen copies of one MCO permit expiry — the same wall of chips the
desk redesign removed, and which the Paperwork block already groups into a single
line. So the horizon is now folded first: anything the page shows elsewhere is
dropped, and any kind with three or more units collapses to one line naming them.
Today that leaves one honest line about #11's battery.

The empty state matters: it says "nothing this week beyond the paperwork above",
never "nothing due this week", because seventeen permits expire in a fortnight
and the Paperwork block is carrying that.

**The blank button:** `.fl-page a` is a more specific CSS selector than any
`.fl-btn-*` class, so an anchor wearing a button class lost its own colour to the
page's link colour — white text became ink on an ink pill. It affected every
link-style action on the Do-now queue. Fixed for all seven button classes.

**Expect to be asked:**
- *"Why is Coming up almost empty?"* — Because there are no service intervals
  set yet. Once a car has a service baseline, that is where "due in nine days"
  will appear. Today the only forward-looking thing the fleet has on record is
  paperwork, and that has its own block.
- *"Does the round count a car that's in the shop all week?"* — No, and it is not
  counted as missed either.
