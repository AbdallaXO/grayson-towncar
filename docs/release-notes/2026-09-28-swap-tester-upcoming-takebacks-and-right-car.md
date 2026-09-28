---
date: 2026-09-28
audience: Dispatchers
title: The Swap Tester only suggests takebacks and swaps a driver can actually do
---

# The Swap Tester only suggests takebacks and swaps a driver can actually do

## Send this to the team

> Hey team — four fixes on the Swap Tester.
>
> 1. Affiliate Takeback only lists jobs still ahead. At 4 PM you won't see the 8 AM job, or one the affiliate's chauffeur is already standing at.
> 2. Take Back no longer names a driver whose shared car is with the other driver at that time, or whose day would run past 15 hours. Those jobs show under Needs Swap.
> 3. Find Swaps goes by the car each trip needs. If a booking is an SUV one way and a Van the other, the Van trip only goes to a Van driver.
> 4. Find Swaps never moves a run that has already happened.
>
> If a swap or takeback would still break one of these, it stops and tells you why.
>
> Nothing else changed. Take Back and Apply this swap work the same as before.

---

## Behind the scenes

**Where it lives:** the Swap Tester page (right-hand column, Affiliate Takeback, and the Find Swaps results). The car-size fix and the "never move a finished run" fix also cover Find Swaps on the capacity planner, because it uses the same search. The Recovery Advisor's suggestions now read the trip's own car size too.

**Why:**
- The swap search read the car size off the booking, not off the trip. A booking can be an SUV out and a Van back, so the Van trip looked like an SUV job. On 9/28 it offered Angel (SUV) the 9:28 AM Van trip an affiliate was holding.
- Take Back only checked the driver's own trips, never the car. On 9/29 it offered CarlosG nine late-morning arrivals while Leo had their shared Suburban. Find Swaps already checked this; Take Back now uses the same check, including the 15-hour day.
- The takeback list showed jobs whose pickup had long gone by. And to make room, a swap could hand someone else a run finished hours earlier (a 4 AM job at 4 PM), which would also have moved its pay.

**Expect to be asked:**
- "The takeback list is shorter than the affiliate count at the top." The top count is every affiliate job that day. The list is only the ones still ahead; a line under it says how many were left out.
- "An arrival is still listed a few minutes after its pickup time." Arrivals stay until 10 minutes after the plane lands (the meet time), so a late plane keeps the job listed.
- "A job moved from Direct Takeback to Needs Swap." The driver it used to name couldn't really take it: the car was with their share partner, or it would have run their day past 15 hours.
- "Find Swaps says no swaps for a trip it used to have one for." The old answer put the trip in a car too small for it, or moved a run that had already happened. The breakdown under it now says "Wrong vehicle" or "Shared car" for each driver who can't take it.
