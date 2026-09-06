#!/usr/bin/env python
"""15 — Build 3a P2: how far apart the three co-driver conventions actually are.

WHY THIS EXISTS
---------------
Build 3a's brief was to unify the co-driver car-share gate into one function
with one home "without changing any verdict — if true unification would change
a verdict anywhere, stop and bring me the discrepancy instead of picking a
winner."

It does change verdicts. The gate is evaluated under three different interval
conventions (dispatching/car_share.py documents all three side by side), and
they disagree on real shared unit-days. This script measures BY HOW MUCH, so
the founder's ruling rests on counts rather than on the worked examples in the
module docstring.

THE THREE CONVENTIONS, as shipped
  A  engine        [pickup − pad, engine clear + pad] vs the partner's raw
                   slot. Overlap only. HARD — a True sends the leg to the farm
                   pool. (car_share.sharers_conflict)
  B  manual warn   handoff_chain occupancy at P75. Overlap + interleave +
                   pickup-to-pickup pad. ADVISORY — never blocks.
                   (car_share.share_conflicts)
  C  mint          the same table at P50. Overlap + full one-sided separation
                   (no interleaving at all). HARD — kills a proposal.
                   (car_share.mint_share_ok)

WHAT IS SCORED
Every leg on every shared unit-day in the current regime is re-asked, as if it
were being proposed onto its own driver: "would each convention allow this leg
on this car, given everything the CO-DRIVER holds?" Because every one of these
unit-days actually operated, a rejection is a convention calling a real day
impossible — the closest thing to a precision reading that exists here (00
carries no cross-driver share ground truth, which is exactly why
assign_warnings ships these classes as "info" today).

METHOD
Raw sqlite only — no Django, no ORM, no writes. Reads the read-only snapshot
(GRAYSON_SNAPSHOT_DB may point at a frozen copy), derives the regime from the
data, and imports the SHIPPED rules from dispatching/car_share.py and
dispatching/handoff_chain.py so the script cannot drift from the product.

Convention A needs an engine clear time, which is Django's
estimate_job_end_time. Rather than boot Django, A is evaluated on the founder's
static planning model — booked pickup + the shipped 45-min arrival dwell +
the category drive table — which is what CHAIN_STATIC_TIMING makes the engine
use for chain feasibility anyway. That is an approximation, and it is labelled
as one everywhere it appears below.
"""
import csv
import datetime as dt
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)
from dispatching import car_share as cs           # noqa: E402  (pure, no Django)
from dispatching import handoff_chain as hc       # noqa: E402

PAD = 120          # SchedulerSettings.vehicle_share_pad_min default
DWELL_MIN = 45     # scheduler.STATIC_FLOOR_DWELL_MIN
DEFAULT_DRIVE = 35  # scheduler.DEFAULT_DRIVE_TIME

ASSUMPTIONS = (
    "Every scored unit-day ACTUALLY OPERATED, so a rejection is a convention "
    "calling a real day impossible. There is no cross-driver share ground "
    "truth in the data (00 §A4), so this is a disagreement census, not a "
    "precision measurement.",
    "Convention A's clear time is the founder's STATIC planning model (booked "
    "pickup + 45-min airport dwell + a flat drive), not Django's "
    "estimate_job_end_time. Flight-driven retimes and the per-category drive "
    "table are therefore not reflected; A's numbers are indicative.",
    "Each leg is scored against what the CO-DRIVER holds, with the leg's own "
    "driver's other legs excluded — the same 'would you allow this' question "
    "each convention answers in production.",
)


class Blk:
    """A leg as a convention sees it. `.pick/.start/.end` is the shape
    car_share.mint_share_ok consumes."""
    __slots__ = ("leg_id", "did", "pick", "start", "end", "clear")

    def __init__(self, leg_id, did, pick, kind, pct, clear):
        self.leg_id, self.did, self.pick, self.clear = leg_id, did, pick, clear
        self.start, self.end = cs.occupancy_block(pick, kind, pct)


def static_clear(pick, kind):
    """Convention A's clear time on the founder's static planning model."""
    dwell = DWELL_MIN if kind == "ARRIVAL" else 0
    return pick + dt.timedelta(minutes=dwell + DEFAULT_DRIVE)


def conv_a(leg, others, pad_min):
    """Engine rule: [pickup − pad, clear + pad] vs each partner's RAW slot."""
    lo = leg.pick - dt.timedelta(minutes=pad_min)
    hi = leg.clear + dt.timedelta(minutes=pad_min)
    return not any(cs.intervals_overlap(lo, hi, o.pick, o.clear) for o in others)


def conv_b(leg, others, pad_min, all_p75):
    """Manual-warn rule: any code fired on the focus leg = 'would warn'."""
    entries = [{"leg_id": e.leg_id, "did": e.did, "pick": e.pick,
                "start": e.start, "end": e.end} for e in all_p75]
    codes = {c["code"] for c in
             cs.share_conflicts(entries, pad_min, focus_leg_id=leg.leg_id)}
    return (not codes), codes


def main():
    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("15_share_gate_divergence.py",
               "Build 3a P2: where the three co-driver conventions disagree",
               h, ASSUMPTIONS)

    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    a, b = segs[-1][0], min(segs[-1][1], h.last_actuals_day)
    print(f"\nregime {a}..{b}")

    # ---- shared unit-days: a vehicle held by >1 driver on one date ----
    dva = C.q(con, """
        SELECT date, driver_id, vehicle_id FROM drivers_drivervehicleassignment
        WHERE vehicle_id IS NOT NULL AND date BETWEEN ? AND ?
    """, (a.isoformat(), b.isoformat()))
    by_date = {}
    for r in dva:
        by_date.setdefault(str(r["date"])[:10], []).append(
            (r["driver_id"], r["vehicle_id"]))
    shared = []                       # (day, vehicle_id, [driver_id, ...])
    for day, pairs in sorted(by_date.items()):
        for vid, holders in cs.holders_by_unit(pairs).items():
            if len(holders) > 1:
                shared.append((day, vid, holders))
    print(f"shared unit-days in the regime: {len(shared)}")

    # ---- the legs on those unit-days ----
    legs = C.q(con, C.live_legs_sql(
        "l.id, l.driver_id, l.pickup_date, l.pickup_time, "
        "l.pickup_location, l.dropoff_location",
        extra=" AND l.driver_id IS NOT NULL "
              " AND l.pickup_date BETWEEN ? AND ? "), (a.isoformat(), b.isoformat()))
    legs_by_drv_day = {}
    for r in legs:
        legs_by_drv_day.setdefault(
            (r["driver_id"], str(r["pickup_date"])[:10]), []).append(r)

    rows, verdicts = [], Counter()
    b_codes = Counter()
    scored_units = 0
    for day, vid, holders in shared:
        d = dt.date.fromisoformat(day)
        unit = []
        for did in holders:
            for r in legs_by_drv_day.get((did, day), []):
                if not r["pickup_time"]:
                    continue
                pick = C.booked_dtm(r["pickup_date"], r["pickup_time"])
                if pick is None:
                    continue
                kind = hc.occupancy_kind(C.loc_bucket(r["pickup_location"]),
                                         C.loc_bucket(r["dropoff_location"]))
                unit.append((r["id"], did, pick, kind))
        if len({u[1] for u in unit}) < 2:
            continue                    # only one holder actually drove
        scored_units += 1
        p75 = [Blk(i, dd, p, k, "p75", static_clear(p, k)) for i, dd, p, k in unit]
        p50 = [Blk(i, dd, p, k, "p50", static_clear(p, k)) for i, dd, p, k in unit]

        for idx, (leg_id, did, pick, kind) in enumerate(unit):
            others_a = [x for x in p75 if x.did != did]
            others_c = [x for x in p50 if x.did != did]
            ok_a = conv_a(p75[idx], others_a, PAD)
            ok_b, codes = conv_b(p75[idx], None, PAD, p75)
            ok_c = cs.mint_share_ok(p50[idx], others_c, PAD)
            for c in codes:
                b_codes[c] += 1
            verdicts[(ok_a, ok_b, ok_c)] += 1
            rows.append([day, vid, leg_id, did,
                         pick.strftime("%H:%M"), kind,
                         int(ok_a), int(ok_b), int(ok_c),
                         "|".join(sorted(codes))])
    con.close()

    n = len(rows)
    C.hdr("DISAGREEMENT CENSUS — every leg on a shared unit-day  [measured]")
    print(f"shared unit-days with two drivers actually driving : {scored_units}")
    print(f"legs scored                                        : {n}")
    if not n:
        print("nothing to score")
        return
    for conv, i in (("A engine (hard)", 0), ("B manual warn (advisory)", 1),
                    ("C mint (hard)", 2)):
        rej = sum(c for k, c in verdicts.items() if not k[i])
        print(f"  {conv:26s} rejects {rej:4d} / {n} legs  ({rej / n:6.1%}) "
              f"of a day that really ran")
    unanimous = sum(c for k, c in verdicts.items() if len(set(k)) == 1)
    print(f"  all three agree on                              {unanimous:4d} / {n} "
          f"legs ({unanimous / n:.1%})")

    C.sub("verdict triples (A, B, C) — True = allowed")
    for k in sorted(verdicts, key=lambda k: -verdicts[k]):
        tag = "unanimous" if len(set(k)) == 1 else "DISAGREE"
        print(f"  A={int(k[0])} B={int(k[1])} C={int(k[2])}  {verdicts[k]:4d}  {tag}")

    C.sub("which B code fired (a leg can fire more than one)")
    for code, cnt in b_codes.most_common():
        print(f"  {code:18s} {cnt}")

    C.hdr("WHAT THIS MEANS FOR THE UNIFICATION DECISION")
    print("Each row above where the triple is not unanimous is a leg on which")
    print("collapsing the three conventions into one would flip a verdict:")
    print("  * adopting A everywhere makes the mint engine and the warnings")
    print("    stricter or looser in the A direction;")
    print("  * adopting C everywhere adds an interleave rule the BUILDER does")
    print("    not have today, removing assignments it currently produces;")
    print("  * adopting B everywhere demotes two hard gates to advisory.")
    print("Build 3a therefore unified the HOME (dispatching/car_share.py), the")
    print("overlap predicate, the occupancy construction, the holders grouping")
    print("and the <=2 constant — all verdict-neutral — and left the choice of")
    print("convention to the founder.")

    p = C.write_csv("15_share_gate_divergence.csv",
                    ["date", "vehicle_id", "leg_id", "driver_id", "pickup",
                     "kind", "ok_engine", "ok_warn", "ok_mint", "warn_codes"],
                    rows)
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
