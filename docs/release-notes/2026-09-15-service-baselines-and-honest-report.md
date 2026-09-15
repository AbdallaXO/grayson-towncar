---
date: 2026-09-15
audience: Dispatchers
title: Service intervals stop inventing dates, and the Report stops flattering
---

# Service intervals stop inventing dates, and the Report stops flattering

## Send this to the team

> Hey team — three changes on **Fleet**.
>
> 1. **Adding the standard intervals no longer makes anything up.** It used to
>    stamp today as the last-service date on every car, which meant all nineteen
>    came due in the same fortnight on dates nobody had ever serviced anything.
>    Now it adds the intervals and leaves the clock unset until there's a real
>    date — and the Sprinters get a longer oil interval than the SUVs, which they
>    always should have.
> 2. **The windshield sticker now sets the clock.** Put the sticker's mileage
>    into a walk-around and the system works backwards to when the last service
>    was — a real baseline off a real number, instead of a guess.
> 3. **The Report stops claiming 100%.** With no shop visits ever logged it was
>    reporting a perfect fleet with no downtime and no spend. Those figures are
>    blank now, with a line saying why. **Becoming unreliable** and **Mileage
>    balance** are unaffected — they come from the trackers, not from paperwork,
>    so they were always real.
>
> **The day** also got easier to read: faint hour lines behind each car, the
> dotted lines between trips removed, and only a genuine shop window highlighted.
>
> Nothing changed about what the cars do, who's driving them, or how anything
> gets assigned.

---

## Behind the scenes

**Where it lives:** the setup strip on the Fleet desk, the walk-around form, the
Report, and **The day**.

**The invented baseline.** `_apply_standard_intervals` stamped
`last_done_on = today` and the current odometer. Pressed fleet-wide that writes
nineteen service histories that never happened; two of the units have no
odometer at all, so theirs were fabricated as NULL anyway. Intervals are now
created with no baseline, which `fleet_health.service_findings` already handles
correctly — it returns nothing without one, because "an invented due date is
worse than none, someone will plan a shop day around it".

Removing it could have made the maintenance half of the desk go *silent*, and
silence and health look identical. So the setup strip now has a second state:
once the intervals exist it says **"N units have intervals but no baseline"** and
points at the walk-around.

**The sticker closes the loop.** A sticker says when the next oil change is DUE;
a schedule stores when the last one was DONE. Those are the same fact either side
of the interval, so `due_at_odometer − interval_miles` gives a baseline nobody
invented. It only fills an oil schedule that has none — a logged service is
somebody saying what they did, a sticker is an estimate written by whoever last
held a marker, and the record always wins. A sticker below one interval is
refused as a typo rather than writing a negative odometer.

**Type-aware intervals.** A diesel Sprinter on a Suburban's 5,000-mile oil
interval comes due twice as often as it needs to; at 190–350 miles a day that is
a shop visit a fortnight the van never needed. Sprinters are on 10,000.

**The Report.** The flags are per-dependency, not one switch: availability and
downtime come from the downtime ledger, spend and adherence from service records.
A reported issue does not make availability meaningful, and one logged oil change
does not make downtime meaningful. Each tile goes blank and says which record it
is waiting on.

**The day.** Trip blocks are drawn to true duration, so a short transfer is
genuinely small and an airport arrival genuinely long — which read as scattered
confetti once dotted connectors ran between every pair. Now: hour rules behind
every row so position carries the timing, no connectors at all (the space between
two blocks *is* the gap), times inside blocks only where they fit and without the
AM/PM the axis already gives, and hatching reserved for a window long enough to
actually use.

**Expect to be asked:**
- *"I pressed Add the intervals and nothing came due."* — Correct. It will stay
  quiet until a car has a real baseline. Read a sticker in on the next walk-around.
- *"Why is the Report empty?"* — Because nothing has been logged yet. It will
  fill as shop visits get closed out.
