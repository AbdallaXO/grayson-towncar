# Fleet desk — Phase 3 audit

**From a picture of the fleet to a plan for the day.**

> Date: 14 Sep 2026 · Read against the production snapshot cut 18:37 UTC that day,
> the then-uncommitted Phase 2 tree, and seeded preview screenshots.
> Tests at the time: 80 of 80 desk tests passing.

The pieces are right and well built. The desk shows facts in four places and leaves
the fleet manager to join them. The next phase should be **composition** — a plan for
the day, a handled state, an impact measure, intraday windows, a weekly inspection
round — not new subsystems.

---

## 1. Ground truth

Every recommendation rests on one of these rows.

| Fact | Measured |
|---|---|
| Active units | 19 (17 on Samsara; #18 and #19 have no gateway) |
| Units the board actually runs | Median 13–16 by weekday over eight weeks; built days carry 16–17 vehicle rows, so 2–3 units are spare most days |
| Intervals · service records · downtimes · issues in production | 0 · 0 · 0 · 0. Phase 1 intervals available since Aug 5 and unused. Registration dates on 16 of 19 cars; insurance and inspection dates on none |
| Open fault codes | 8 on 4 units: #004 four DEF/reductant codes, #008 misfire + catalyst, #10 turbo underboost (critical), #005 cylinder-5 misfire open since Aug 31 |
| Batteries | #005 at 11.9 V. #11 at 12.2 V, gateway silent since Sep 9, no chauffeur today or tomorrow |
| Mileage | SUVs 320–350 mi/day, Sprinters 190–350, Metris 256–286, the Transit van 160 |
| Oil cadence at that mileage | 5,000 mi falls due every 14–20 days on most units; 7,500 mi every 21–30. Across 17 units that is roughly **one oil service every business day** at 5k |
| Farm-outs | 17% of legs in the last four weeks (615 of 3,651). Saturday peaks at 40 trips in flight against 19 cars |
| Booking fill | 81% of a day's final legs exist 7 days out; 61% at 14; 48% at 21; 38% at 28. Saturdays fill earliest (85% at 7 days) |
| Board horizon | Today 98% of legs assigned, +1 day 97%, +2 87%, +3 62%, +4 onward 0%. Per-car windows are knowable about three days out |
| Sprinter tier | 5 capable units, never more than 3 needed at once in the next 10 days |

**Fleet-wide there is no quiet mid-day.** 09:00–14:00 is the peak every day; the
quietest business-hours block is 07:00–08:30. Per car the picture is different and
more useful: on built days specific cars have long gaps (Monday #007 idle 10:51–14:00
between its morning and evening chauffeurs, #002 until 11:15; Tuesday #18 free from
12:36; Wednesday #10 until 10:54, #17 until 10:43, #13 no jobs at all). Roughly a
quarter of driver-days carry a four-hour-plus gap between pickups.

**Maintenance here is a rotation, not an event.** At any realistic interval each car
needs an oil service about monthly, so the fleet manager places a service nearly every
business day. A "find a gap for this one job" tool is the wrong shape; a rolling queue
with the next free slot beside each car is the right one.

**Conflict is the normal state.** Weekday peaks of 21–26 and weekend peaks of 34–40 in
flight against 19 cars mean six of the next seven days are "short with every car". A
binary verdict cannot rank options. What ranks them is how much more is farmed if one
specific car is gone — and that is computable.

---

## 2. The ten workflows, walked

**Arrives and opens Fleet.** Feed line, five tiles, attention list, three side panels,
a 14-day strip, a 19-row table. An inventory, not an agenda: nothing says "do this
first". "Free today" is only right after Day Setup has been applied; before that every
car reads as free. The red pill counts units with any open fault, so with #005's code
open since Aug 31 it has been lit for two weeks — the fatigue the notify module guards
against, reproduced in the bar.

**Understands today's priorities.** The NOW group has the right facts in the wrong
shape. Ordering inside a level uses per-kind sort keys that do not compare across
kinds. Every item carries a generic italic action repeated verbatim. No buttons. The
same overdue return appears as a NOW item and in the In-the-shop panel. There is no
"handled" state: a fault already booked for Tuesday screams every morning.

**A car approaches maintenance.** One service line per unit, projected dates collapsed
after four — sound rules. But production has no intervals, so the service half of the
desk is silent and the Setup fold is collapsed; silence and health look the same. The
bulk "standard intervals" button clocks every baseline from today, so every car comes
due in the same fortnight, and diesel Sprinters land on a 5k schedule.

**Finds the least disruptive time.** The Outlook judges days clear, tight or conflict.
The double judgement — with every car, then with this car — is the best idea in the
module and must stay. But the gap suggester ranks clear days by utilisation, so it
prefers the farthest-out days because they are the emptiest. It offers Saturdays. It
renders utilisation 0.67 as "fleet 1% busy". There is no notion of shop hours.

**Schedules the downtime.** The vehicle-page modal is good: live check, plain-words
reason, a tick to save over a short day. Its granularity is the day, so a 90-minute oil
change either goes unrecorded or blocks a whole car-day. A fault cannot be linked to
the downtime it caused; only an issue can.

**Dispatch understands the impact.** Solid: the pool card strikes the unit through with
reason and return date, an amber "back? not confirmed" tag, Day Setup excludes it and
says why, copy-forward skips it, the assignment endpoint answers 409 with an override.
Gaps: dispatch never sees readiness on the pool; partial-day work is invisible; and
fleet, having no board, cannot see when a car it wants is free.

**Weekly inspections.** No concept exists. The "next inspection" date is the annual
one; the "inspection" interval is a 365-day service. Nothing to see, nothing resets.

**An inspection finds a problem.** The Report-a-problem modal is the right tool. It
needs a one-tap path from the inspection.

**The problem enters the maintenance workflow.** Issue, take down, close-with-resolve
works. But closing a downtime does not log the service; the service form still carries
its own out-of-service dates that gate nothing; the downtime-to-service link is never
set by the UI. One oil change is three separate entries — which is why production has
zero service records and the report's PM adherence, spend and "what keeps repeating"
cannot work.

**Management opens Fleet.** The Report is sound and entirely dependent on a ledger
nobody is filling. Nothing says "is the fleet inspected this week".

### Friction, named

- **Duplicated:** the overdue car in two panels; five reason lines per conflict day; two out-of-service concepts.
- **Misleading:** pre-build "free"; the permanent red pill; "conflict" on a fleet that farms weekly; far-out "quiet" days; "watch" covering both two fault codes and a registration due in 12 days; "1% busy".
- **Unused data:** assignment rows plus leg times (per-car windows); the Day Setup concurrency series (intraday); booking lead times; daily miles (return detection); battery samples (trend).
- **Forced joins:** overdue service × idle today × quiet day; fault × no chauffeur × silent gateway; marked down × drove 80 miles.

Today's real case: #005 has a misfire open two weeks, a battery at 11.9 V, and no
chauffeur today, tomorrow or Wednesday. The desk puts those in three places and never
says "take #005 in now; it costs dispatch nothing".

---

## 3. Recommendations

### Tier 1 — high value, should probably do

**R1 · A "Today" panel.** A panel above the tiles, three to seven lines, each a verb
plus a button. *Do now* — a service, fault or issue on a car free today or with a
usable gap. *Confirm* — overdue returns, with It's back / Push date. *Call* — a
do-not-drive or critical fault on a car with a chauffeur today. *Decide* — a planned
window that now collides. Ordered by consequence. Composes functions that already
exist; no new data.

**R2 · A "handled" state, so NOW means unhandled.** Any item can be planned by linking
it to a downtime; a planned item moves to a Planned group and returns to NOW if the
date passes without a close. Watch-level faults can be acknowledged. The navbar pill
counts unhandled NOW items only.

**R3 · Choose downtime by marginal impact, not a binary verdict.** Keep the day
judgement and the window check. Add a continuous measure from the concurrency series
Day Setup already builds: **marginal farmed car-hours**. Say it in words — "Thursday
costs nothing; Saturday adds about 8 farmed car-hours". Rank suggestions by that cost,
then soonest; default to shop days and hours; cap the horizon at ten days; label
farther days "too early to call — N% of bookings in". Fix the percent rendering.

> One SUV, next ten days: Mon 1.5h · Tue 0.5 · Wed 1.0 · Thu 0 · Fri 3.0 · Sat 8.5 ·
> Sun 5.0 · Mon 3.5 · Tue 0 · Wed 1.0. Any Sprinter: zero on every day.

**R4 · Intraday.** (a) **Per-car free windows**: assignment rows → every chauffeur on
that car → their legs → merged busy spans using the job-end estimator → gaps inside
shop hours, padded 45–60 minutes each side for flight drift, only on days whose board
is built. Show the two neighbouring jobs; flag a window that follows an airport arrival
as flight-dependent. Beyond built days say "not built yet", never "free".
(b) **A timed downtime row**: optional start and end times; does not remove the car
from the pool; shows as a chip on the pool card and the chauffeur's lane; assigning an
overlapping leg warns, advisory.

**R5 · The weekly inspection round.** One row per car per week — vehicle, week start,
when, by whom, outcome, note, linked issue, optional odometer — unique per car and
week. A grid of active units for the ISO week; tiles due / done / done-with-issue / in
the shop. Header: "12 of 19 inspected · 2 found something". Tap a tile and it is done;
"Found something" opens the existing Report-a-problem modal. The week resets on Monday
by construction. The optional odometer field is the only mileage source for #18 and #19.

**R6 · One motion for "it's back".** The close modal gains a structured "what was
done", creating the service record, linking it, and advancing the interval. Hide the
out-of-service fields on the service form. Add a service queue panel.

**R7 · Honest onboarding.** Seed intervals with type-aware defaults **without
baselines**, so rows read "needs a baseline" instead of coming due together. Show an
onboarding panel prominently until every active car has a baseline and its dates.

### Tier 2 — good improvement, consider

**R8 · Dispatch sees what Fleet knows.** Readiness chips on the pool card and
assignment modal — advisory only, never a gate.
**R9 · Desk hierarchy pass.** Five tiles become three; inline buttons; cross-kind NOW
order by consequence; the strip recoloured by marginal cost; "watch" split into problem
and note.
**R10 · Honesty labels on the horizon.** "N% of bookings in" beyond a week; cap
suggestions at ten days.
**R11 · The car tells you it's back.** A live downtime whose unit drove 20+ miles
today; a unit off the board two days with a silent gateway and no downtime.
**R12 · Report additions.** Inspection compliance by week; age of unhandled items;
placement graded by marginal cost.

### Tier 3 — only if justified

**R13 · Battery trend** (warn on four days below 12.0 V, not one sample).
**R14 · Rotation hint within a type** (Sprinter tier at 1.8× imbalance).
**R15 · "Ask dispatch"** button filing a manual task.
**R16 · Mileage backfill** — low urgency.

### Do not build

- Per-item inspection checklists or photos, until the notes show a pattern.
- A demand forecast beyond the fill curve.
- Any automatic message to a chauffeur about a shop window.
- Samsara DVIR or Maintenance — not licensed, and a driver-app change.
- Any gate on machine-inferred data.

---

## 4. Bugs named at the time

1. Outlook suggestion rows: utilisation is a 0–1 ratio rendered as "fleet 1% busy".
2. "Free today" fires for every non-down unit when the day has no assignment rows.
3. The desk's "It's back" uses a browser prompt; the vehicle page has a proper modal.
4. The Vehicles page carries its own copy of the chip and feed CSS.
5. The gap suggester's far-out bias and Saturday offers.

## 5. What to preserve

Pure modules with one loader; DB-only pages; the ledger's three gating rules and rule 2
in particular (a forgotten row costs a nag, never a car); the double-judged window
check; informs-never-blocks; the feed line first; NULL is not zero; the release-note
discipline; the tests.

---

## 6. Ideas added beyond the brief

- **Treat maintenance as a rotation** and the oil change as a 90-minute intraday job.
  The cadence finding is the most important thing in this audit; its design consequence
  is a rolling service queue matched to per-car idle windows, not a new subsystem.
- **Let the car report its own return.** Daily miles and gateway silence already say
  when a "down" car is driving and when an "up" car is secretly parked.
- **Tier slack as a standing fact.** The Sprinter tier has five units and never needs
  more than three at once this fortnight; the SUV tier is what binds.
- **Make the inspection the odometer source** for the two cars without a gateway.

---

## 7. Status — what has shipped since

| | Shipped | Where |
|---|---|---|
| R1 Do-now queue | ✅ 2026-09-14 | `fleet_queue.py` |
| R3/R4 hour-level shop windows (fleet-wide, per tier) | ✅ 2026-09-14 | `fleet_windows.py` |
| R9 desk hierarchy (partial) | ✅ 2026-09-14 | five-state band, grouped paperwork |
| **R4a per-car free windows** | ✅ 2026-09-15 | `fleet_day.py` — "The day" |
| **R5 weekly inspection round** | ✅ 2026-09-15 | `fleet_inspection.py` — "Inspections" |
| R2 handled state | partial | queue has `handled`; no plan link |
| R3 marginal farmed car-hours | ❌ | still a three-way verdict |
| R6 close-and-log, R7 onboarding, R8, R10–R16 | ❌ | |

Two deviations from this document, both on the founder's instruction (2026-09-15):

- **Per-item checklists and photos were built**, against the "Do not build" entry
  above. The founder asked for a tickable checklist with a note and a photo per item.
- **The audit's per-car windows were given their own page** rather than only a column
  on the Vehicles table — a car's real day runs 04:30–22:30 and does not fit a cell.
