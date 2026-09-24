---
date: 2026-09-23
audience: Dispatchers
title: Show only certain vehicle types in the Unassigned row
---

# Show only certain vehicle types in the Unassigned row

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
  No file names, no field names, no jargon. Say what to click, what is
  different, and what did NOT change.
-->

## Send this to the team

> Hey team — the Unassigned row on the schedule board can now show just the vehicle types you're working on, like only the vans and the 14-passenger vans.
>
> 1. On the board, click the little gold car button next to the driver filter.
> 2. Tick one or more vehicle types. The other unassigned jobs drop out of the row straight away.
> 3. Click "Show every vehicle" to get the full backlog back.
>
> A gold line under the header tells you what the row is showing and how many backlog jobs are hidden. The choice sticks when you page to the next day or switch between the In-House and Affiliate boards.
>
> Nothing else changed. The driver lanes always show every job, the counts at the top are still the whole day, and the driver filter and passenger search work exactly as before.

---

## Behind the scenes

**Where it lives:** the schedule board header, a small gold car icon right after the driver dropdown. Both boards. It only ever touches the Unassigned row.

**Why:** on a busy day the backlog is a wall of chips of every size. Working it one vehicle class at a time (all the Sprinter jobs first, then the vans) is how it actually gets assigned, and the driver lanes have to stay whole while you do it.

**Expect to be asked:**
- "The Unassigned row looks empty" — check the car icon; if it has a number on it, a filter is on. The gold line under the header says so too, and has the link to clear it.
- "Does it hide anything in the driver lanes?" — no. Only unassigned jobs are ever hidden. Lanes and the header counts are always the whole day.
- "It's still on when I come back" — it lives in the page address, so it survives refresh and the date arrows. Clear it with "Show every vehicle" or by unticking.
- The list only offers types that have an unassigned job that day. Unassigned jobs with no vehicle on the reservation show up as "No vehicle set" so they can be found and fixed rather than silently hidden.
