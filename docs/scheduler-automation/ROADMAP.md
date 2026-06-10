# Scheduler Automation — Roadmap

**Updated 2026-06-10.** The plain-language status of the schedule builder: what's done,
what's next, and why. Technical detail lives in `auto-assign-hour-balancing-design.md`
(same folder); this file is the map.

---

## ✅ Done and live in production

- **One-button day building**: Suggest Day Setup (roster + vehicle plan) → Apply →
  Auto-Assign. On the hardest benchmark Saturday (2026-05-16) it kept 110–112 jobs
  in-house vs 97 hand-built, same 15 drivers / 13 cars.
- **Shared cars (split shifts)**: when more drivers can work than there are cars, the
  morning driver hands the car off (3 PM default) to an evening driver. Double-booking a
  shared unit is physically impossible in every path, including manual swaps, with a
  60-minute warehouse buffer (return + wash/fuel + drive out) at every handoff.
- **Hour-balance guardrails**: hard 17-hour ceiling no rescue can exceed (a job that fits
  nobody under 17h farms, loudly, with a named reason); 13.5h-effective soft target with
  trim/relocation passes; midnight rule — a 00:00–02:59 pickup never lands on a day
  driver (explicit night windows still work; manual dispatcher moves always win).
- **Second-Shift Advisor**: after a build, proposes "add THIS driver with THIS unit" when
  residual jobs or untrimmable long days justify another body. One-click accept.
- **Feasibility calibrated to reality**: deplaning grace, intra-resort drive times,
  pre-farm swap recovery, gap compaction (the "give David the job in his hole" move).

**Settled by experiment (don't revisit without new data):**
- The handoff hour (12/1/2/3 PM) does NOT change coverage — busy-day coverage is bound by
  CARS, not driver-hours. The 3 PM default stands.
- An extra vehicle pays only on days when available drivers outnumber cars (+5 in-house,
  ~$500–750/day on 05-16; +0 on driver-bound days). **Every SHARED badge in Day Setup
  marks a day a spare unit would have earned its keep — count them to size the fleet.**

---

## ▶ Next (recommended order)

### 1. Demand-aware staffing  *(agreed 2026-06-10 — next up)*
Remove the daily brain-power drain of judging headcount by hand on 100+ job days.
- **Solo-first Day Setup**: when drivers outnumber cars, stop auto-proposing a split —
  leave the extras unchecked ("available — add via Advisor if the day needs them"). The
  Second-Shift Advisor, which reads the actual built board, proposes the split only when
  the day truly needs it. Demand-aware by construction, no guessing.
- **Fold-Out Advisor** (the mirror image): after a build, propose releasing thin drivers —
  "sereen has only 3 jobs and they fit on ken/george/rizwan; fold her out?" One-click
  accept relocates the jobs (all feasibility-checked) and frees her day + her car.
  Replaces the founder's manual post-build redistribution. Proposes only; never automatic
  until trust is earned.
- Includes (already coded, in this arc's first push): declining a proposed share by
  unchecking one driver now cleanly gives the other driver the car all day.
- Estimate: 1.5–2 sessions including benchmark validation.

### 2. Chain-aware builder
The greedy builder sometimes takes a single job over a better two-job round-trip chain
(the confirmed Aftab case: a 10:42 Publix run over an 11:00→12:15 airport chain). Needs
lookahead scoring; A/B on 05-09 / 05-16 / 06-01 / 06-02. Worth a few extra in-house jobs
and less deadhead on busy days. Estimate: the next big arc after #1.

### 3. Flexible-start staggering
Start late finishers later the next day (rest-aware). Designed; review found the
early-coverage guard must become vehicle-tier-aware before building (could strand an
early van pickup). Needs real prod hours data.

### 4. Real driver windows
Flip `USE_STUB_WINDOWS=False` once driver schedule data is cleaned (deactivate
placeholder id 6, fix OFF-marked-but-working drivers, pre-6AM starts), then re-benchmark.
Until then start/end windows come from observed history.

### 5. Live road-distance matrix
Re-introduce live drive times WITHOUT a network call during page loads (precomputed
matrix or Redis). Restores exact distances for far/unknown + intra-resort routes; today
those use the coarse category table (accuracy traded for the 2026-05-31 timeout fix).

### 6. Warehouse/base-location concept
Make the shared-car handoff buffer geography-aware: car-ready time = last clear + drive
to warehouse + wash/fuel; next pickup ≥ car-ready + drive out. Today it's a flat 60 min.
Low urgency — the flat hour matches founder practice.

### Business decision (no code): the 14th vehicle
Track SHARED badges for a few weeks. If they appear on most busy Saturdays, an extra
unit pays for itself (~$500–750/busy day at current farm-out spreads).

---

## Founder to-dos (10 minutes, admin screens)
- Set preferred vehicles: David → #008, roberto → #004, sereen → #003 (george done).
- Fix rizwan/ken weekly schedules (marked OFF on days they actually work).
- Keep retiring unused units (e.g. #015 done) so the car count the engine plans around
  stays honest.

## Known small gaps (accepted for now)
- A brand-new hire with zero history can't be force-added to a full day through Day
  Setup (workaround: Second-Shift Advisor accept). A "re-suggest with my checkboxes"
  option would close it.
- The Day Setup share proposal is headcount-driven (fixed by roadmap #1).
- The UI's "Skip unpaid" toggle (on by default) makes UI coverage numbers read ~3 lower
  than offline benchmarks on the same board — same engine, deliberate rule.
