# Live Saturday Sandbox — 2026-06-13 Shift-Length Calibration

**Session 2026-06-11.** Engine sandboxed on the real upcoming Saturday
(133 booked / 131 live legs, 130 paid per the reservation-level predicate;
founder draft = the 13 DVA driver->vehicle rows, 0 legs assigned).
All 9 runs restored byte-identical (extended snapshot incl. pay-autofill fields);
final state = founder draft. Deterministic (USE_LIVE_DISTANCE off).
Harness: `scratch/sandbox_0613.py` (run / report / restore / leave / resnap).

## Decision table

| run | basis | cap | in-house | farmed | days>13h raw | worst raw/eff | median raw | 2nd-shift cards | notes |
|---------|---------|----------------------|-----|----|----|------------|------|-----|------------------------------|
| A17 | draft | 17 (today's default) | 110 | 21 | 11 | 16.9/16.9 | 16.1 | 4/0 | six 16h+ days |
| B17 | scratch | 17 | 111 | 20 | 12 | 16.9/16.9 | 15.7 | 4/0 | 2 rescue lifts |
| A15 | draft | 15 uniform | 105 | 26 | 10 | 14.9/14.9 | 14.2 | 4/0 | -5 jobs |
| A-MIX | draft | 13.5 + shelley/rizwan @16 | 100 | 31 | 8 | 15.0/14.2 | 13.2 | 5/0 | -10 jobs |
| A-MIX2 | draft | 13.5 + sereen/roberto @16 | 101 | 30 | 6 | 15.7/15.7 | n/a | 8/0 | -9 jobs; best mixed variant |
| B-MIX | scratch | 13.5 + shelley/sereen @16 | 99 | 32 | 7 | 15.1/15.1 | 13.2 | 4/0 | -12 jobs |
| A13.5 | draft | 13.5 uniform | 98 | 33 | 6 | 13.5/13.4 | 12.8 | 4/0 | -12 jobs |
| B13.5 | scratch | 13.5 uniform | 96 | 35 | 5 | 13.5/13.3 | 12.9 | 4/0 | -15 jobs |
| B-SPLIT | scratch | 13.5 + car shares | 96 | 35 | 1 | 13.1/13.1 | 12.2 | 4/0 | most humane board; 2 AM/PM shares |

**Round 2 — "cards we're dealt" (founder ask: max coverage AND humane on the
current 13 cars):**

| run | strategy | in-house | farmed | days>13h | worst | notes |
|---|---|---|---|---|---|---|
| A15+2 | 15h + sereen/roberto @16 | 105 | 26 | 10 | 14.9h | leash adds ZERO at 15 — the marginal evening legs need 16.4-17.8h |
| B-SPL15+2 | 15/16 typed + shares | 103 | 28 | 8 | 15.9h | sereen burns his full leash |
| B-SPL15 | 15h + AM/PM shares | 102 | 29 | 8 | 14.9h | first ACTIONABLE 2nd-shift card of the study (driver 68 on freed veh9 17-20, overload relief); ken/runer 6.2/6.6h PM shifts, Junaid 9.1h, Olmo 11.3h |

Round-2 insight: at a 15h default the 16h leash recovers nothing — the
strict_blocked texts show the next jobs back need 16.4-17.8h stretches, i.e.
re-creating the 17h day. Shares DO unblock the second-shift advisor (a freed
unit finally exists) but on this day the accepted card relieves an overload
rather than adding net coverage. The 15h frontier: 105 solo (his roster) vs
102 split (four drivers get genuinely lighter days for -3 jobs).

All boards: 0 share overlaps, 0 nights-on-flexible, 0 hollow days.

## The three load-bearing findings

1. **Saturday 06-13 is CAR-bound before it is hours-bound.** A stable core of
   ~16-20 residuals farms in EVERY scenario, even at cap 17: the midday Port
   Canaveral / MCO cruise cluster + Van(14 Pax) legs. Peak cumulative
   towncar-capable demand = 16 in flight @ 13:30 vs 13 active cars. The cap
   debate only moves the EVENING tail (the 6:15 PM-11:10 PM legs named in the
   ceiling warnings).
2. **Second shifts cannot fire on this day — there is no car to give anyone.**
   Every second-shift card the advisor proposed had NO source option
   (best=None): zero spare units (all 13 active vehicles rostered dawn-to-night)
   and zero freed windows. The "+16 more potential shifts" info card is the
   demand signal; the missing 14th car is the binding constraint (consistent
   with the 05-16 finding: a spare unit = ~+5 in-house/day when checked
   drivers > cars). Vehicle #015 sits inactive.
3. **Two long-leash drivers recover only a quarter of the uniform-13.5 loss.**
   MIX (13.5 for everyone, 16h for two) recovers 2-3 of the 12 lost jobs —
   even when the leash goes to the drivers the engine names as sole-feasible
   (sereen, roberto). The evening tail is spread across too many drivers for
   1-2 exceptions to absorb.

## Strategy verdict (uniform-17 vs uniform-13.5 vs mixed vs splits)

- **Coverage order:** 17 (110) > 15 (105) > MIX2 (101) ~ MIX (100) > uniform
  13.5 (98) = SPLIT (96, B-basis).
- **Humane-day order:** SPLIT wins outright (worst 13.1h, median 12.2h, ONE day
  over 13h; second-shift drivers ken/runer work clean 6h evening shifts on the
  shared cars) >> uniform 13.5 >> MIX (worst 15-15.7) >> 15 >> 17 (six 16h+ days).
- **The trade in dollars-ish:** each step of humanity costs ~5 in-house jobs:
  17->15 = -5, 15->MIX = -4..-5, MIX->13.5/SPLIT = -2..-5. At $70-230 farm vs
  $25-50 in-house, 12 extra farmed legs on a Saturday is real money — but so are
  six 16h+ driver-days every Saturday.
- **Splits vs mixed:** splits give a far better board shape at the same coverage
  as uniform 13.5; mixed buys ~3 jobs back at the price of two 15-16h days.
  Neither recovers the evening tail fully — only hours (cap 15+) or a 14th car
  does.

## Caveats for reading the boards

- Warning copy renders 13.5 as "the 14h absolute day ceiling" (a `:.0f`
  format artifact); the enforced value is 13.5.
- B17's two "rescued past the 13h/12h cap" lines are PERSONAL (stub) caps
  lifted within the global 17 — not the global cap.
- MIX typed caps are STRICT: rescue is disabled for typed drivers
  (strict_blocked residuals) — slightly stricter than the same number as a
  global cap. A typed 16 also cannot raise a driver whose stub cap is lower.
- B-basis Day Setup rosters ernesto + Francisco Pedraza in place of the
  draft's ken + runer (and shuffles two vehicles): the engine's roster choice,
  visible in the per-driver matrix.
- UI shows ~4 fewer legs than these tables (exclude_unpaid default).

## 18-day multi-date sweep (2026-06-13..06-30, from-scratch basis)

Run via `scratch/sandbox_sweep.py` (06-12 excluded as the live next-day board;
every date restored byte-identical incl. the founder's partial builds on
06-15..06-21). Full table: `scratch/sandbox_sweep_report.txt`.

Per-bucket totals (jobs vs same-day 17h baseline; 13h+ = driver-days over 13h raw):

| bucket | strategy | in-house | jobs vs 17 | avg/max worst | 13h+ days |
|---|---|---|---|---|---|
| busy (2d) | S17 | 209 | — | 16.2/16.9h | 20 |
| | S15 | 204 | −5 | 14.9/14.9h | 20 |
| | SPL15 | 198 | −11 | 14.7/14.9h | 14 |
| | SPL13.5 | 191 | −18 | 13.2/13.4h | 5 |
| medium (10d) | S17 | 688 | — | 15.3/17.0h | 39 |
| | S15 | 677 | −11 | 14.2/15.0h | 30 |
| | SPL15 | 656 | −32 | 13.9/14.9h | 22 |
| | SPL13.5 | 654 | −34 | 12.8/13.4h | 10 |
| slow (6d) | S17 | 253 | — | 14.6/16.8h | 10 |
| | S15 | 250 | −3 | 13.0/14.7h | 6 |
| | SPL13.5 | 250 | −3 | 12.7/13.5h | 2 |

Sweep findings: (1) 15h costs ~1 job/day on medium, ~2.5/day on busy, ~free on
slow — and kills every 15h+ day incl. slow-day 16.8h surprises (06-16, 06-30).
(2) Splits are EXPENSIVE on medium days (−2/day extra vs S15 — shares fragment
windows when the day is not car-bound) but are the busy-day shape play.
(3) On slow days shares rarely form and 13.5 is free: SPL13.5 ties S15 on
coverage with 2 vs 6 long days. (4) Caveat: dates >1 week out are
currently-booked volume; days will grow and move buckets, but per-bucket
conclusions hold.

**Sweep verdict: set the global cap to 15h** (best coverage-per-humanity
everywhere, one simple rule, −19 jobs over 18 days vs today). Slow days can
tighten to 13.5 for free via typed Max hrs; splits remain a busy-day judgment
call (Day Setup toggle) trading ~3 jobs/day for the gentlest board.

## Founder decision: 15h default — IMPLEMENTED 2026-06-11 (uncommitted)

"Let's move it to fifteen hours, and I can always tweak that." Implemented with
typed-raise semantics so the tweak path works: `SPAN_HARD_HOURS_DEFAULT=15.0`,
NEW `SPAN_ABS_CEILING_HOURS=17.0`, typed/DB per-driver Max hrs may exceed the
default up to the absolute ceiling (a typed 16 binds at 16; the old min() would
have clamped it to 15). Rescue still bounded at the 15h policy default. Rescue
warning routing fixed per adversarial review (strict-typed drivers report THEIR
cap). 340 dispatching tests green; live 06-13 preview = 105 in-house, worst
14.9h. Full record: design doc PART 8. Commit/deploy = founder's go.

## Artifacts

- Full ASCII boards: `scratch/sandbox_0613_report.txt`
- Raw per-run data: `scratch/sandbox_0613_results.json`
- Sacred snapshot: `scratch/board_snapshots/2026-06-13.json` (+ full-file
  backup `content/db_backup_pre_0613_sandbox.sqlite3`)
- View any board live: `.venv/Scripts/python.exe scratch/sandbox_0613.py leave <RUN-ID>`
  then restore with `... sandbox_0613.py restore` (anything clicked in the UI
  on 06-13 while a sandbox board is applied gets ERASED by the restore).
