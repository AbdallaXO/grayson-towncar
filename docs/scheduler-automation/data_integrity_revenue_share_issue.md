# Data Integrity Issue — `Leg.revenue_share` is $0 / inconsistent for ~45% of legs

**Logged:** 2026-05-30 (surfaced during scheduler Phase 2.5a margin analysis). **Not fixed —
tracked for later.** Likely affects revenue/profit reporting **beyond** the scheduler.

## What

Across a 5-day sample (608 legs), **275 legs (45%) have `revenue_share = $0`**, and **273 of
those belong to reservations whose `total_price > 0`** — i.e., the per-leg revenue split is
empty even though the guest paid. Observed via read-only query against live prod (`scratch/
phase25a_revcheck.py`).

Two distinct failure modes:
1. **Round-trip / multi-leg split puts all revenue on one leg.** `Reservation.recalculate_leg_revenue_shares`
   (`reservations/models.py:481`) does a weighted split by `leg_base_price`; when one leg has a
   `leg_base_price` of 0/None the sibling can absorb 100% and this leg gets $0.
2. **Single-leg reservations never populated.** Examples (live): res 9902 ($168, 1 leg, share $0),
   res 9349 ($213, 1 leg, $0), res 8802 ($126, 1 leg, $0). A 1-leg reservation should have
   `revenue_share == total_price`; these are $0, so `recalculate_leg_revenue_shares` was apparently
   never run for them (it's only invoked on add/remove of legs).
3. **Split can exceed the total.** res 6708: legs sum to **$1,035** vs `total_price` **$740** — the
   denormalized shares are internally inconsistent for at least some reservations.

## Why it matters

- `revenue_share` (and the derived `profit_estimate = revenue_share − total_driver_pay`,
  `reservations/models.py:1233`) is used for per-leg revenue/profit reporting. Where it's $0/!=split,
  any per-leg revenue or margin number is wrong.
- It **invalidated the Phase-2 "farmed revenue" figures** (incl. the busy-Saturday "+$1,437"),
  which were computed on `revenue_share`. Phase 2.5 re-bases on `reservation.total_price` instead.
- Likely contaminates other dashboards that read `revenue_share`/`profit_estimate` at the leg level
  (vehicle-profit, driver-performance, accrual/revenue reports) — **needs a separate audit**.

## Suggested follow-up (not done now)

- Backfill: re-run `recalculate_leg_revenue_shares()` for all reservations (esp. 1-leg ones), and
  add a data check that `Σ legs.revenue_share == reservation.total_price`.
- Consider invoking the recalc on reservation price changes, not only leg add/remove.
- Audit downstream consumers of `revenue_share` / `profit_estimate`.

*No code or data changed. Read-only investigation only.*
