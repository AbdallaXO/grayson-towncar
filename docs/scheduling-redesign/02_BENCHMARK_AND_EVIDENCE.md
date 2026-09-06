# 02 — Benchmark and Evidence

**The reproducible evidence base: what the four committed scripts prove, the headline numbers,
and how this becomes the permanent "is it doing good" instrument.**

| | |
|---|---|
| Produced | 2026-08-23 |
| Scripts | [`analysis/08_assignment_stream.py`](analysis/08_assignment_stream.py) · [`analysis/09_benchmark_state_b.py`](analysis/09_benchmark_state_b.py) · [`analysis/10_standby_and_mints.py`](analysis/10_standby_and_mints.py) · [`analysis/11_handoff_chain.py`](analysis/11_handoff_chain.py) — house conventions: no date literals (every window derives at run time), DB opened read-only, `_common.py` reuse, A6 filters, pairing caps stated |
| Verification | Adversarial pass, 2026-08-23: each script run cold by an independent verifier, every headline re-derived by a structurally different method. Two clean PASSes (10, 11); two PASS-WITH-NOTES (08, 09) where the verifier proved the **scripts** right and the earlier ad-hoc session figures imprecise. |
| Re-run | `python docs/scheduling-redesign/analysis/NN_name.py` from the repo root, any time, against any fresh `content/db.sqlite3` pull. CSVs land in `analysis/out/`. |

Labels: **[measured]** / **[inferred]** / **[modeled]** per 00's convention.

---

## 1. The benchmark — what "better" is measured against

**There is no machine build to beat.** Board-level auto-assign last ran 2026-08-10 (1 of 28
current-regime dates; 18 of 155 ever) [measured, 08]. The incumbent is **state B: the operated
board** — built driver-by-driver with the per-driver Schedule Builder, scarce tiers first, then
hand-finished (D9). Read directly from `reservations_leg`, never replayed from event logs.

**State B, current regime (derives to 2026-07-24..2026-08-20, 28 days)** [measured, 09]:

| Criterion | Value |
|---|---|
| Legs | 3,023 (108.0/day) |
| In-house coverage | 2,458 legs = **81.3%** (87.79/day); farmed 20.18/day; 0 unassigned |
| Drivers | 15.46 distinct in-house/day; 433 driver-days |
| Hard-infeasible turn pairs | **11.4–11.6/day** (shipped constants, 8 h cap); tight 9.6/day |
| Hours violations | **4.00 driver-days/day > 13.5 h**; 2.18/day > 15 h; max span 23.64 h |
| Shared vehicle-days (handoffs) | 34 of 389 (8.7%), on 21 of 28 dates, never 3+ drivers/car [11] |

Any future schedule proposal is scored on this same per-date scorecard
(`out/09_state_b_scorecard.csv`): coverage, conflicts, hours, fairness. **When the founder later
judges whether the optimizer "is doing good" (D11), this is the instrument.**

## 2. The assignment record — what can and cannot be trusted

[measured, 08] The only valid assignment stream is the `historicalleg` transition walk (48,132
A6-filtered transitions from 2026-03-03). `reservations_auditlog` assignment rows are **30.8%
no-op phantoms** (the `signals.py:751` skip-guard; the nightly confirmation-SMS job perfectly
mimics a machine build); Reset Schedule bypasses both trails. The machine-vs-human burst
signature on the valid stream is cleanly bimodal.

**Where coverage is made and lost** [measured, 08]: coverage is created at *first placement*
(share peaks ~T-12 h at 82.9%); reassignment churn is **net-negative in every time-to-pickup
band** — there is no band where shuffling adds in-house coverage. Releases outrun recaptures
(7.1–7.3 vs 2.36/day); ~500 genuine reciprocal swaps happen in 28 days (18× a permutation null)
— feasibility repair against a moving flight clock, not coverage hunting. **Consequence: the
optimizer intervenes at day-before build time, never as day-of rescue.**

## 3. The lever — cap enforcement + standby mints (the adopted rule)

[modeled on measured boards, 10] Standby = **available that day**: active driver, zero legs, zero
DVA row, no approved time-off, 510-min rest both sides — no activity-history filter (D-2026-08-23,
new hires visible from day one). Pool: **8–14/day, P50 10** [measured]. Same-day pull-ins already
happen 0.75/day [measured] — the floor of reachability; willingness is unrecorded.

Fixed-strict replay (co-driver car-share constraints enforced, OOS cars excluded, waterfall
capacity, ≤2 drivers/vehicle-day), vs the 81.3% baseline:

| Setting (gap/buf) | Cap-only | With mints | Coverage | ≈ $/yr |
|---|---|---|---|---|
| Conservative (180/45) | −1.75/day | **+1.93/day** | 83.1% | ~$50k |
| **Central (120/30)** | −1.25/day | **+3.00/day** | **84.1%** | **~$78k** |
| Generous (90/5) | +0.54/day | +4.54/day | 85.5% | ~$118k |

Zero days over 13.5 h by construction; zero new rest breaches; existing breach-pairs **68→32
healed**. Call-outs ~3.2/day, 5–10 on the heaviest days — **no daily cap** (founder). Mint
policy: the soft ≥2-job preference costs **nothing**; a hard floor forfeits −2.18/day (~$56k) —
ships soft (D6). **Saturation: 84.1%** even with unlimited bodies — cars run out, not people
(480 no-free-car failures vs ~23 no-body); **~88% of the unreachable residual picks up
08:00–12:59**, the car-bound morning bank. Past this ceiling the levers are +1 rostered
morning shifts (≈ +2.75 legs/day ≈ $71k/yr each [measured sensitivity]) and vehicles — the D8
follow-on, deliberately out of scope.

## 4. The handoff chain — validated against practice

[measured/modeled, 11] The founder chain (drop → El Car Wash by MCO → fuel 8 min → base at
6785 Narcoossee → incoming driver waiting ≥1 h) closes against all 32 measured handoffs: car
ready ~55–67 min after an MCO drop; the median real handoff carries **~51 min of headroom** over
the central chain; 75% clear it. The 72-min observed floor matches the skip-wash fast path
(≈34 min) — AMBER territory requiring an explicit dispatcher plan. The shipped
`VEHICLE_SHARE_PAD_MIN = 60` sits at ~P9 of reality. Per-zone matrix in `out/11_chain_matrix.csv`;
the model itself is [`03_STANDBY_AND_HANDOFF_MODEL.md`](03_STANDBY_AND_HANDOFF_MODEL.md).

## 5. Honest limits (inherited by everything above)

Summer-only data (season/growth confounded); 28-day regime (every percentile carries that n);
standby willingness unrecorded (pull-in rate is the only floor); all replay gains are
counterfactuals against fixed demand — central estimates, not promises; ~46% of specific farm
refills drift >30 min by service time (plan-time aggregate survives: +2.71/day); arrival clocks
move (97.5% retimed) and every plan must expect day-of repair; deadhead is permanently
unobservable (D3).

---

*The build these numbers justify is specified in
[`04_PLANNER_AND_BUILD_PLAN.md`](04_PLANNER_AND_BUILD_PLAN.md).*
