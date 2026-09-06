# 03 — The Standby & Handoff Model

**The operational model behind split shifts: who can take a second seat, when a car can change
hands, and what a legal day looks like. This is the config the optimizer will carry — every
number labeled by where it came from.**

| | |
|---|---|
| Produced | 2026-08-23 |
| Status | Numbers verified 2026-08-23 against the committed evidence scripts (`analysis/08–11`, adversarially checked). Reviewed at the build-readiness package (Gate 3). |
| Governed by | [`01_REVISED_SCOPE_AND_PLAN.md`](01_REVISED_SCOPE_AND_PLAN.md) decisions D4 (hours), D6 (mint jobs), D7 (base process), D9 (build order), D11 (additive), D12 (no rushed build) |
| Labels | **[founder-supplied]** · **[measured]** · **[modeled]** · **[shipped-estimate]** (a constant already in production code) · **[assumed]** (no source — needs founder confirmation or stays flagged) |

---

## 1. The standby pool — who can take a second seat

A driver is **standby-eligible** for date D when all four hold:

1. Zero legs on D and zero roster (DVA) row for D — they are not deployed;
2. **Available that day**: an active driver with no approved time-off (`DriverDateOverride`,
   full-day `off` or a blocking partial window) covering D. *No activity-history filter* — an
   earlier draft required a worked leg within ±7 days and the founder struck it: a newly hired
   driver has no history yet and must never be invisible to the pool [founder-supplied];
3. **Rest both sides**: ≥510 min (the live `rest_min_gap_minutes`) between the previous day's
   actual last work and this shift's first pickup lead, and again into the next day's first work.

**Measured sizes under the adopted rule** [measured, re-run 2026-08-23]: the pool runs
**8–14/day (P50 10, mean 10.3)** in the current regime — never below 8. The struck ±7-day
behavioural variant measured 6–9 (P50 8) and prints alongside as comparison. Cores: ~3.1/day
worked yesterday (the second-shift handoff core); ~6.5/day are evening-feasible *and* fully
rested. The founder's working "1–4" describes the *reachable-by-habit* core, not the bench.
Under the adopted pool the replay draws ~3.2 call-outs/day at the central setting and **5–10 on
the heaviest days**. **No daily call-out cap** [founder-supplied, 2026-08-23: "whatever is best
for the business"] — the model proposes what the day needs; dispatchers remain the throttle.

Warnings that ride with the model:

- **Friday is the thinnest bench day** (mean 6.0) — inside the Fri–Sun band that carries 76.3%
  of farm-out. The bench never bound the replay (23 fill-failures for want of a body vs 476 for
  want of a car), but the margin is smallest exactly when the prize is largest.
- **Same-day pull-ins are real and rising** — 0.75/day in the current regime (tripled from the
  plateau), the revealed floor of reachability [measured]. Historically concentrated in one
  now-inactive driver; ten distinct drivers were successfully called same-day in the regime.

## 2. Mint rules — what a second shift is allowed to look like

- **Soft ≥2-job preference (D6), never a hard floor.** Packing order: an unassigned leg goes to
  an existing rostered driver with a feasible hole first, then onto an already-open mint, and a
  *new* mint opens only when neither works — preferring candidates that can feasibly capture two
  or more pool legs. Measured cost of the preference under the adopted pool: **zero** — identical
  net to unrestricted, with fewer call-outs [modeled]. A **hard** 2-job floor would forfeit
  **−2.18 legs/day (~$56k/yr)** by cancelling every structurally single-job mint (~70% of
  call-outs) and re-farming their legs — rejected.
- **Thin mints are flagged, not blocked.** ~Two-thirds of feasible mints are structurally
  single-job (no second leg can join them under the gap/buffer/span constraints) [measured].
  There is **no call-out minimum pay** [founder-supplied] — a single-job mint costs the company
  nothing extra; its cost is driver goodwill. The dispatcher sees "thin shift — worth it?" with
  the dollars it saves, and decides. Offer; let the driver decline.
- **At most 2 drivers per vehicle-day.** Never observed above 2 in either regime [measured];
  enforced as a hard rule.
- **Mints are a day-before decision, never day-of rescue.** Coverage is created at build time and
  leaks in the 24–72 h finalize window (~7.5 legs/day to affiliates); day-of reassignment has
  never been net-positive for coverage in any measured time band [measured].

## 3. The handoff chain — when a car can change hands

**The process being modeled** [founder-supplied, D7]: outgoing driver drops the last guest →
car wash → fuel → base at 6785 Narcoossee Rd → incoming driver (waiting at base ≥1 h before
their own first pickup) takes the car → drives to their first job. House handoffs exist as rare
exceptions and are not modeled.

### 3.1 Components

| Component | Minutes (low/central/high) | Source |
|---|---|---|
| MCO → wash | 14 / 15.5 / 17 | [founder-supplied] |
| Wash — **fixed location: El Car Wash, by MCO** | 15 / 17.5 / 20 | [founder-supplied] |
| Fuel | **8** | [founder-supplied] |
| Wash → base | 20 | [founder-supplied] |
| MCO → base (direct) | 12 | [founder-supplied] |
| Disney/Universal → base | ~30–40 | [founder-supplied "to be safe" = high; shipped zone estimate = low] |
| Other drop zones → wash | shipped zone→MCO estimate + MCO→wash | [shipped-estimate + founder-confirmed routing — wash and base both sit by MCO, so every drop routes via the MCO corridor] |
| Base → next pickup | per zone (MCO 12; Disney 30/35/40; Universal 25/32/40; Port 45/50/55; SFB 55/60/65; hotels/residential per shipped table + offset) | [founder-supplied / shipped-estimate / assumed, per zone] |
| Pre-pickup buffer | 10 (airport pickup) / 15 (other) | [shipped-estimate — production convention] |

**Car ready at base ≈ 55–67 min after an MCO drop** (central); longer from the west side.

### 3.2 The feasibility rule — green / amber / red

For a proposed handoff on one car, with A the outgoing and B the incoming driver:

- **GREEN** — B's first pickup ≥ A's clear time + the **central** chain for that drop→pickup zone
  pair. Schedulable without ceremony. 75% of real handoffs already clear this bar [measured].
- **AMBER** — below central but ≥ the **low** chain, or on the **skip-wash fast path** (≈34 min
  clear-to-pickup for an MCO→MCO pass): feasible **only with an explicit dispatcher plan** —
  wash done the evening prior, or a direct hand at MCO. The tightest handoff ever executed
  (72 min pickup-to-pickup) sits on this path [measured/inferred].
- **RED** — below the low chain with no fast path. Not plannable; never proposed.

**Simplification, by decision [founder-supplied]: every handoff is modeled through the base.**
No house-handoff modeling and no west-side shortcut — even where the geography would allow one
(the measured Disney→Disney passes that bypassed the base), the model charges the full base
chain. Slightly conservative on west-side pairs, deliberately simple; the home/shortcut edge
case is handled operationally when it arises, not modeled.

### 3.3 The volatility guard

An arrival's booked time IS its flight time and moves — mostly on the day itself. The founder's
own 08/20 example absorbed a **4.4 h** swing in the PM driver's first pickup only because the
planned gap was generous [measured]. Rule: **a handoff whose incoming first job is an airport
arrival is priced against the flight-volatility band, not the booked minute** — the gap must
survive the P75 retime (13 min) at GREEN and be flagged at AMBER beyond it. Gap-shaving toward
the observed minimum is never free.

### 3.4 What the measured practice says

Current regime [measured]: handoffs on 8.7% of vehicle-days, 21 of 28 dates, growing ~50% across
the demand step; pickup-to-pickup gaps n=32: min 72 / P25 182 / P50 220 / P75 286 / P90 394
(8 h cap). The shipped `VEHICLE_SHARE_PAD_MIN = 60` sits near the 9th percentile of this —
optimistic nine times in ten — and is superseded by the zone rule above. Every handoff on record
was arranged by hand; the roster columns built to hold split windows
(`planned_start_hour`/`planned_end_hour`) are NULL on all 2,591 rows. **Writing the plan down is
part of the feature.**

## 4. The hours structure (D4)

- **13.5 h soft cap enforced by default** (`SPAN_SOFT_EFFECTIVE_HOURS`, shipped). Today's board
  violates it 4.0 driver-days per day [measured]; enforcement plus mints is a coverage *gain*
  (**+3.00 legs/day central under the adopted pool**, vs −1.25 for cap-only) [modeled], and
  healing: existing rest-floor breach-pairs fall 68→32, with zero new breaches.
- **15 h is the crunch ceiling** (`SPAN_HARD_HOURS_DEFAULT`, shipped) — allowed only as a
  **structured exception**: proposed per driver, with its price shown ("+1.5 h on X keeps 2 legs
  in-house, ≈$142"), visible and dispatcher-approved, never silent. Today 2.18 driver-days per
  day exceed even this, invisibly [measured].
- **Rest floor 510 min** (`rest_min_gap_minutes`, live) — both sides of every shift including
  mints, against *actual* adjacent-day work, not declared availability.

## 5. Where the knobs live

Existing homes are reused, never forked (00 §B4): the rest floor, span caps, and turn buffers
stay where production reads them today. **New config this model introduces** — the occupancy
lead/tail by trip kind, the zone chain table, the availability-based standby definition, the
mint gap parameter (default 120 min; sweep evidence at 90/120/180), and the ≥2-job preference —
has no existing home and goes where the Phase-2 spec (04) designates, exposed as editable
parameters, not constants. Per 00: only `time_scarcity_bonus` is safely repurposable; everything else is new.

## 6. Open inputs

**None.** Both items this section carried were closed by the founder on 2026-08-23: fuel =
**8 minutes**, and the wash is a **fixed location — El Car Wash, by MCO** (the base/warehouse is
also by the airport), which converts the MCO-corridor routing from an assumption into the actual
geography. Nothing in this model now rests on an unconfirmed operational number.

---

*Companion: [`02_BENCHMARK_AND_EVIDENCE.md`] (in progress) holds the reproducible scripts behind
every [measured] figure here. The Phase-2 spec (04) turns this model into a build plan.*
