---
date: 2026-09-21
audience: Dispatchers
title: The schedule board now shouts when a job's flight has been cancelled or diverted
---

# The schedule board now shouts when a job's flight has been cancelled or diverted

## Send this to the team

> Hey team — an airport job whose flight has been cancelled now shows it right
> on the schedule board. The chip gets a red dashed ring that pulses and a red
> badge with a plane and "✕ CXL" next to the pickup time. A diverted flight
> gets an amber "DIV" badge. Hover the chip and the popup says "Flight status:
> CANCELLED — call the guest before the driver rolls."
>
> It works in every row, including Unassigned, and it reads the same flight
> tracking the flight alerts use, so it appears as soon as the tracker knows.
>
> When you see one: call the guest, find out the new flight, and fix the pickup
> before the driver heads to the airport. That is the whole point of the badge.
>
> What did not change: everything else on the chip, the drag and drop, and the
> flight alert tasks in Ops Control, which still fire as before.

---

## Behind the scenes

**Where it lives:** the schedule board, both boards, every chip with a
tracked flight.

**Why:** on 21 September the 4:42 PM arrival's flight was cancelled. The
tracker had recorded it and a flight task had been filed, but the chip on the
board looked like any other job. A driver can roll to MCO for a plane that will
never land.

**Mechanics:** the chip reads the tracked flight's status text. "cancel" in it
means cancelled, "divert" means diverted; anything else (Scheduled, Arrived,
Delayed, Not Found) is untouched. The class and badge are added at render, the
popup reads the same data attributes. No new queries: the flight was already
loaded for every chip.

**Expect to be asked:**
- *"The flight was cancelled an hour ago and the chip only just changed."*
  The badge appears when the board is loaded or reloaded, from what the
  tracker knew at that moment. Reload the board if it has been open a while.
- *"Not Found isn't flagged."* Deliberately. That is usually a typo in the
  flight number, and it already has its own task.
