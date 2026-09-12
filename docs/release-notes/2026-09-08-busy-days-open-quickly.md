---
date: 2026-09-08
audience: Dispatchers
title: The busiest days open in a couple of seconds
---

# The busiest days open in a couple of seconds

## Send this to the team

> Hey team — the big days don't make you wait any more. A Saturday with 240
> trips on it used to take twenty-odd seconds to come up on the legs dashboard.
> It's a couple of seconds now, and the schedule board and capacity planner got
> quicker too.
>
> One thing looks slightly different. On the legs dashboard, the driver dropdown
> on a trip starts out showing only whoever is already on the job. Click it and
> the full list of drivers drops down the way it always has. Nothing is missing —
> it just waits until you ask for it instead of loading every driver onto every
> trip before you've even touched the page.
>
> Nothing else moved. Same trips, same assignments, same buttons in the same
> places. Assigning a driver, changing a status and everything else works exactly
> as it did, and drivers see nothing different in their app.

---

## Behind the scenes

**Where it lives:** the legs dashboard, the schedule board and the capacity
planner — the three date pages. Most of the gain is on the legs dashboard, which
is where a busy day hurt most.

**Why:** the pages were being sent to the browser uncompressed. A 190-trip day was
a 12 MB download; it now goes over the wire at about 380 KB. On top of that, the
legs dashboard was building the complete driver list into every single trip's
dropdown, twice per trip — 23,000 hidden dropdown entries on a busy day, which the
browser had to build before it would show anyone anything.

**Expect to be asked:**
- "The driver dropdown looks empty." It isn't — click it and the list is there.
  Only the driver already assigned shows before you click.
- "Did anything change about who's assigned?" No. Nothing about assignments,
  suggestions or Auto-Assign was touched.
- "Is it faster on my phone too?" Yes, and more so — the download was the bigger
  half of the wait on a phone.
