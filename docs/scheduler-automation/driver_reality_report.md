# Driver Reality Report — Observed Availability vs Configured

**READ-ONLY.** Generated 2026-05-30 from live prod history (`readonly_local`),
**Feb 1 – May 29, 2026**. Source: `scratch/phase3_reality.py`. **No driver records were
written.** This is a **confirm-ready artifact**: a human should eyeball each row and set the
*real* production window — it is **not** auto-applied. The Phase-3 guard uses the **STUB** columns
as provisional windows only (see caveats).

## How to read it
- **Cfg start-end** = configured weekly window (min start .. max end across available weekdays;
  per-weekday detail in `scheduler_phase3_driver_windows.md`). **flex** = any flexible day.
  **cfg max_h** = configured `max_hours` (— = none set anywhere).
- **Obs first-pickup / last-clear** = earliest pickup time-of-day and latest *clear* (finish) time
  observed across all worked days. **Obs max span** = longest first-pickup→last-clear on any one
  day (⚠ inflated by split days — see caveat). **Median span** = typical day length (more realistic).
- **#pre-start** = legs picked up *before* the configured start hour. **#clear>cfg-end** = legs
  *finishing after* the configured end hour (clear-by violations). **#worked-off** = days the driver
  worked despite being marked OFF for that weekday.
- **STUB start/end/max_h** = provisional window the guard uses now = ⌊earliest pickup⌋ ..
  ⌈latest clear⌉, max_h = ⌈observed max span⌉.

## Report

| Driver | id | Cfg | flex | cfg max_h | Obs 1st-pickup | Obs last-clear | Max span | Median | Days | Legs | #pre-start | #clear>end | #worked-off | STUB start | STUB end | STUB max_h |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| roberto | 32 | 6-23 | no | — | 00:53 | 23:38 | 22.3 | 12.7 | 93 | 617 | **22** | 0 | 0 | 0 | 23 | 23 |
| neuma | 26 | 3-16 | no | — | 02:27 | 20:19 | 22.4 | 10.6 | 93 | 592 | 1 | **38** | 2 | 2 | 21 | 23 |
| Yovanny Suarez | 46 | 6-18 | no | — | 06:00 | 19:27 | 13.3 | 10.2 | 89 | 545 | 0 | **21** | 6 | 6 | 20 | 14 |
| Junaid Baidr | 52 | 4-23 | no | — | 03:30 | 21:14 | 15.5 | 9.9 | 85 | 465 | 10 | **15** | 3 | 3 | 22 | 16 |
| runer | 33 | 0-23 | no | — | 03:00 | 23:51 | 17.4 | 11.3 | 85 | 506 | **22** | 7 | 3 | 3 | 23 | 18 |
| Angel Almanzar | 38 | 0-23 | no | — | 02:00 | 23:32 | 15.3 | 10.3 | 81 | 433 | 17 | **25** | 2 | 2 | 23 | 16 |
| alex | 49 | 6-23 | no | — | 04:00 | 23:30 | 16.9 | 12.8 | 63 | 414 | 8 | 0 | **63** | 4 | 23 | 17 |
| rizwan | 55 | 6-23 | no | — | 04:30 | 23:39 | 16.2 | 11.1 | 62 | 326 | 3 | 0 | 0 | 4 | 23 | 17 |
| ken | 58 | 6-23 | no | — | 00:15 | 23:56 | 19.9 | 11.9 | 60 | 362 | 8 | 0 | 0 | 0 | 23 | 20 |
| george | 56 | 6-23 | no | — | 03:30 | 23:57 | 17.2 | 12.5 | 60 | 374 | 7 | 0 | 3 | 3 | 23 | 18 |
| shipo | 34 | 6-23 | no | — | 07:15 | 23:20 | 15.8 | 8.9 | 60 | 263 | 0 | 0 | **60** | 7 | 23 | 16 |
| Seline | 57 | 6-23 | no | — | 03:00 | 22:49 | 16.3 | 11.8 | 58 | 369 | **26** | 0 | 3 | 3 | 23 | 17 |
| Michael Olmo | 48 | 0-17 | no | — | 02:30 | 19:28 | 16.5 | 12.2 | 50 | 374 | 0 | **7** | 2 | 2 | 20 | 17 |
| Steven Kleisath | 54 | 6-23 | no | — | 00:30 | 23:58 | 15.1 | 10.9 | 50 | 268 | 9 | 0 | 0 | 0 | 23 | 16 |
| Aftab | 59 | 6-23 | all | — | 01:12 | 23:53 | 19.4 | 11.6 | 43 | 230 | 8 | 0 | 0 | 1 | 23 | 20 |
| Julio Bonilla | 31 | 6-23 | no | — | 04:00 | 23:45 | 16.0 | 11.1 | 35 | 186 | 3 | 0 | **35** | 4 | 23 | 16 |
| Hasan | 35 | 6-23 | no | — | 00:38 | 23:38 | 15.9 | 10.5 | 28 | 138 | 12 | 0 | **28** | 0 | 23 | 16 |
| sereen | 53 | 6-23 | no | — | 03:45 | 21:53 | 16.9 | 11.4 | 28 | 155 | 12 | 0 | 0 | 3 | 22 | 17 |
| placeholder | 6 | 6-23 | no | — | 05:15 | 22:32 | 5.3 | 1.3 | 30 | 51 | 2 | 0 | **30** | 5 | 23 | 6 |
| Rayyan Vorajee | 1 | 6-23 | no | — | 00:05 | 23:53 | 21.4 | 1.8 | 14 | 23 | 3 | 0 | **14** | 0 | 23 | 22 |
| Abdalla | 9 | 6-23 | no | — | 00:37 | 23:18 | 8.7 | 1.7 | 12 | 17 | 3 | 0 | **12** | 0 | 23 | 9 |
| shelley | 62 | 6-23 | all | — | 01:41 | 23:52 | 20.1 | 10.5 | 10 | 45 | 1 | 0 | 0 | 1 | 23 | 21 |
| mesfin | 63 | 6-23 | all | — | 04:15 | 18:56 | 12.4 | 7.3 | 9 | 29 | 1 | 0 | 1 | 4 | 19 | 13 |
| Carlos Medina | 20 | 2-23 | no | — | 03:00 | 21:10 | 13.6 | 12.2 | 8 | 49 | 0 | 0 | **8** | 3 | 22 | 14 |
| Idrees | 61 | 6-23 | all | — | 05:15 | 22:44 | 14.7 | 13.3 | 7 | 47 | 3 | 0 | 0 | 5 | 23 | 15 |
| Raymond | 64 | 6-23 | all | — | 07:30 | 23:02 | 14.8 | 14.0 | 4 | 20 | 0 | 0 | 0 | 7 | 23 | 15 |
| HassanA | 65 | 6-23 | all | — | 08:48 | 23:36 | 14.2 | 11.5 | 3 | 19 | 0 | 0 | 0 | 8 | 23 | 15 |

## What the data says (for human correction)

1. **Several "active" drivers are mis-configured as OFF but work heavily** — `alex` (63 days),
   `shipo` (60), `Julio Bonilla` (35), `placeholder` (30), `Hasan` (28), `Rayyan`/`Abdalla` (occasional).
   Their weekly schedules are wrong and must be set to real availability (or the driver retired/inactive).
   `placeholder` (id 6) is clearly not a real driver — should be deactivated.
2. **`start_hour=6` is too late for many** — strong pre-6 AM pickup counts (Seline 26, roberto 22,
   runer 22, Angel 17, Hasan 12, sereen 12). Real early starts (airport/cruise) need lower start hours.
3. **The real-window drivers already "violate" their configured end a lot** — `neuma` (3-16, **38**
   clears past end), `Angel` (**25**), `Yovanny` (6-18, **21**), `Junaid` (4-23, **15**), `Michael Olmo`
   (0-17, **7**). Either the windows are too tight or they work past them — a human must decide the true
   clear-by per driver. **These are the drivers a CLEAR_BY guard will actually bind.**
4. **Observed max span (19–23h) is NOT a real shift length** — it's inflated by split days (an early job
   and a late job on the same date with a big idle gap). Median span (~10–13h for full-timers) is the
   realistic day length. **Stub `max_hours` from observed max span is therefore loose** and will rarely
   bind; set real per-driver caps from the median/operational reality, not the max.

## ⚠ Caveats (must carry into any result built on these stubs)

- **The stub windows are PROVISIONAL and OPTIMISTIC.** Observed history captures what a driver *did*,
  not their true hard limits — it widens windows and inflates max-hours. Any coverage/cost number
  produced under stubbed windows must be **re-measured once real windows are configured in production**.
- This report **suggests**; a human **confirms** in prod. The guard reads stubs from an isolated config
  block (`dispatching/feasibility_guards.py:STUB_DRIVER_WINDOWS`) — swapping in real configured windows
  is a one-line switch (`USE_STUB_WINDOWS = False`), not a rewrite.
