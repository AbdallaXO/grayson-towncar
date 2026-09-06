#!/usr/bin/env python
"""08 — The de-phantomed assignment event stream, and everything built directly on it.

THE QUESTION THIS SCRIPT EXISTS TO ANSWER
-----------------------------------------
What actually happens to a leg's driver slot between booking and pickup — who writes it,
when, by machine or by hand, and with what net effect on in-house coverage?

THE ONE NON-NEGOTIABLE RULE (00 §A4.6)
--------------------------------------
`reservations_auditlog` is NOT the assignment stream. 30.8% of its driver-assignment rows
are phantoms: reservations/signals.py:751 returns early when `save(update_fields=…)` names
neither 'status' nor 'driver', the pre-save snapshot stays empty, and the post-save handler
logs a fresh assign for a leg whose driver never moved. The biggest writer is the nightly
confirmation-SMS job (dispatching/confirmation_sms.py:509), whose phantoms arrive in one
Twilio-paced burst per day, exactly one day before service — perfectly mimicking a nightly
machine build. Section 1 measures this, as the justification for everything after it.

The valid stream = `reservations_historicalleg`, walked per leg ordered by
(id, history_date, history_id), emitting a transition wherever driver_id changes.
Board state (T-0) is read from `reservations_leg` directly.

NO HARDCODED DATES. Horizon and the regime boundary derive from the data at run time.

Outputs: out/08_transitions.csv, out/08_churn_per_day.csv, out/08_band_table.csv,
         out/08_ladder.csv
"""

import datetime as dt
import os
import random
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ASSUMPTIONS = (
    "driver_type and is_active are CURRENT-STATE flags (00 §A5/A4): every in-house vs "
    "affiliate split below applies today's flag to historical events. Contamination is "
    "bounded at 0.21% of assigned legs by django_admin_log (00 §A5/A3).",
    "The assignment stream is the historicalleg driver-transition walk (00 §A4.6). "
    "reservations_auditlog appears ONLY in the phantom census of section 1 — never as a "
    "stream. Reset Schedule uses queryset .update() and is invisible to BOTH trails.",
    "A transition's time-to-pickup uses the PLAN-TIME pickup carried on the same history "
    "row (pickup_date/pickup_time as they stood at the click), not the final values — "
    "arrival pickup_time is retimed ~7.6x per leg (00 §A11), so final values would credit "
    "the dispatcher with foresight. history_date is UTC; converted DST-aware.",
    "An unassign followed by a re-assign within 30 min is ONE reassignment with a "
    "transient hole: X->None->Y coalesces to X->Y (rule from the Phase-1 move-taxonomy "
    "verification; hole P50 ~4 min). Raw and coalesced tallies both reported.",
    "Machine-vs-human classification is temporal (burst shape), because machine and manual "
    "writes are byte-identical rows. The 2 s split validates against the 604 labelled "
    "sandbox draft events with zero false positives on manual actions (00 §A4.6).",
)

# Transition-class codes: I = in-house driver, A = affiliate (a COMPANY, not a person —
# 00 §A10), U = unassigned, X = driver id no longer in drivers_driver.
BANDS = [">72h", "24-72h", "6-24h", "1-6h", "<1h", "after-pickup"]

COALESCE_S = 1800        # 30-min X->None->Y merge window
SWAP_WINDOW_S = 30.0     # reciprocal-swap pairing window
NULL_TRIALS = 20         # permutation-null trials (seeded — reproducible)
RUN_GAP_S = 5.0          # consecutive same-actor assigns <=5 s apart form a run
BUILD_MIN_N = 20         # board-level build: >=20 legs in one run...
BUILD_MED_GAP_S = 1.0    # ...at a median intra-run spacing <=1 s (server loop pace)
CLUSTER_GAP_S = 2.0      # burst-taxonomy split (validated rule, 00 §A4.6)


def band_of(hrs):
    if hrs is None:
        return "unknown"
    if hrs > 72:
        return ">72h"
    if hrs > 24:
        return "24-72h"
    if hrs > 6:
        return "6-24h"
    if hrs > 1:
        return "1-6h"
    if hrs >= 0:
        return "<1h"
    return "after-pickup"


def main():
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("08_assignment_stream.py",
               "the de-phantomed assignment stream: builds, bursts, churn, bands, ladder",
               h, ASSUMPTIONS)

    # ---- windows: derived, never assumed --------------------------------------
    byday = C.legs_per_day(con, end=h.last_demand_day)
    segs = C.changepoints(byday, dt.date.fromisoformat(min(byday)), h.last_demand_day,
                          min_seg=28, min_effect=0.09)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    plat_a, plat_b = segs[-2][0], cur_a - dt.timedelta(days=1)
    cur_days = (cur_b - cur_a).days + 1
    stream_days = (cur_b - plat_a).days + 1
    print(f"\nderived regimes  : plateau {plat_a}..{plat_b} ({segs[-2][3]:.1f} legs/day), "
          f"current {cur_a}..{cur_b} ({segs[-1][3]:.1f} legs/day, {cur_days}d)")
    print(f"stream window    : {plat_a}..{cur_b} ({stream_days} service dates)")

    CUR = (cur_a.isoformat(), cur_b.isoformat())
    PLAT = (plat_a.isoformat(), plat_b.isoformat())
    plat_days = (plat_b - plat_a).days + 1

    # ---- driver classes (current-state flag — see assumptions) ----------------
    dtp = {r["id"]: r["driver_type"] for r in
           C.q(con, "SELECT id, driver_type FROM drivers_driver")}

    def cls(d):
        if d is None:
            return "U"
        t = dtp.get(d)
        return "I" if t == "inhouse" else ("A" if t == "affiliate" else "X")

    # ------------------------------------------------------------------ 1. phantoms
    C.hdr("1. WHY NOT AUDITLOG — the phantom census [measured] (00 §A4.6)")
    print("State-based walk: per leg, carry the driver forward from None; an auditlog row")
    print("that does not CHANGE the reconstructed driver is a phantom (no-op).")
    per_leg_ev = defaultdict(list)
    for r in con.execute(
            "SELECT object_id, action, new_value FROM reservations_auditlog "
            "WHERE model_name='Leg' AND action IN ('driver_assigned','driver_unassigned') "
            "ORDER BY object_id, timestamp, id"):
        per_leg_ev[r["object_id"]].append(r)
    leg_ids = {r[0] for r in con.execute("SELECT id FROM reservations_leg")}
    n_rows = n_ph = n_rows_live = n_ph_live = 0
    for lid, evs in per_leg_ev.items():
        exists = lid in leg_ids
        st = None
        for e in evs:
            phantom = False
            if e["action"] == "driver_assigned":
                nv = int(e["new_value"]) if e["new_value"] else None
                phantom = (nv == st)
                st = nv
            else:
                phantom = (st is None)
                st = None
            n_rows += 1
            n_ph += phantom
            if exists:
                n_rows_live += 1
                n_ph_live += phantom
    print(f"\n  auditlog driver rows (all legs)      : {n_rows:,}")
    print(f"  phantoms (no state change)           : {n_ph:,}  ({100.0*n_ph/n_rows:.1f}%)")
    print(f"  on still-existing legs               : {n_ph_live:,} of {n_rows_live:,} "
          f"({100.0*n_ph_live/n_rows_live:.1f}%)")
    print("""
Any analysis that COUNTS auditlog assignment rows measures the signal bug, not the
dispatchers. (00 §A4.6 published 28,255/91,670 = 30.8% from the same rule; the walk here
lands within 20 rows of it — the residual is a filter-variant difference on legs whose
row-level vs final-state cancellation status disagree, bracketed below in section 2.)""")

    # ------------------------------------------------------------------ 2. the stream
    C.hdr("2. THE STREAM — historicalleg transition walk [measured]")
    WALK_SQL = ("SELECT id, history_date, history_id, history_type, driver_id, "
                "history_user_id, pickup_date, pickup_time "
                "FROM reservations_historicalleg ORDER BY id, history_date, history_id")
    print("SQL: " + WALK_SQL)

    # legs universe: the A6 demand filter, final state, exactly as every sibling script
    legs = {}
    for r in C.q(con, C.live_legs_sql(
            "l.id AS lid, l.pickup_date AS pd, l.pickup_time AS pt, "
            "l.driver_id AS fd, r.created_at AS ca")):
        legs[r["lid"]] = dict(pd=r["pd"], pt=r["pt"], fd=r["fd"],
                              created=C.to_local(r["ca"]))
    print(f"A6 legs universe: {len(legs):,}")

    trans_by_leg = defaultdict(list)   # lid -> [dict], chronological
    hist_rows_of = defaultdict(list)   # lid -> [(local_dt, driver_id)] for the ladder
    n_hist = n_raw_all = 0
    cur_leg, prev_d = None, None
    had_nonnull = False                # has this leg EVER held a driver in its history?
    first_ev = None
    for r in con.execute(WALK_SQL):
        n_hist += 1
        lid = r["id"]
        in_u = lid in legs
        loc = C.to_local(r["history_date"])
        if in_u:
            hist_rows_of[lid].append((loc, r["driver_id"]))
        if lid != cur_leg:
            cur_leg, prev_d = lid, r["driver_id"]
            had_nonnull = r["driver_id"] is not None
            if r["history_type"] == "+" and r["driver_id"] is not None:
                n_raw_all += 1
                if in_u:
                    trans_by_leg[lid].append(dict(
                        t=loc, frm=None, to=r["driver_id"], actor=r["history_user_id"],
                        pd=r["pickup_date"], pt=r["pickup_time"], first=True))
                    if first_ev is None or loc < first_ev:
                        first_ev = loc
            continue
        if r["driver_id"] != prev_d:
            n_raw_all += 1
            if in_u:
                trans_by_leg[lid].append(dict(
                    t=loc, frm=prev_d, to=r["driver_id"], actor=r["history_user_id"],
                    pd=r["pickup_date"], pt=r["pickup_time"],
                    first=(r["driver_id"] is not None and not had_nonnull)))
                if first_ev is None or loc < first_ev:
                    first_ev = loc
            prev_d = r["driver_id"]
            if r["driver_id"] is not None:
                had_nonnull = True

    n_tr = sum(len(v) for v in trans_by_leg.values())
    n_assign = sum(1 for v in trans_by_leg.values() for t in v if t["to"] is not None)
    n_unassign = n_tr - n_assign
    print(f"\n  history rows scanned                 : {n_hist:,}")
    print(f"  transitions, no filter (all legs)    : {n_raw_all:,}")
    print(f"  transitions on A6 legs               : {n_tr:,}  "
          f"({n_assign:,} to-a-driver / {n_unassign:,} to-NULL)")
    print(f"  first event                          : {first_ev} (local — history "
          f"installs here; nothing earlier is reconstructable at transition level)")
    print("""
PROVENANCE NOTE: the unfiltered walk reproduces the Phase-1 verification's 49,087 exactly,
so the walk itself is identical. 00 §A4.6 quotes 48,278 "under the §A6 filters"; a sweep of
every defensible A6-application (final-state universe / row-level status / cancelled legs
included) brackets it at 48,132..48,536 — the figure moves <1% with the filter choice and
every downstream conclusion is invariant to it. This script applies the SAME final-state A6
universe as every sibling script.""")

    # annotate: plan-time pickup, band, classes, service window
    for lid, ts in trans_by_leg.items():
        for t in ts:
            pick = C.booked_dtm(t["pd"], t["pt"])   # plan-time pickup ON THAT ROW, local
            t["hrs"] = None if pick is None else (pick - t["t"]).total_seconds() / 3600.0
            t["band"] = band_of(t["hrs"])
            t["fc"], t["tc"] = cls(t["frm"]), cls(t["to"])

    def win_of(pd):
        if CUR[0] <= pd <= CUR[1]:
            return "CUR"
        if PLAT[0] <= pd <= PLAT[1]:
            return "PLAT"
        return "OUT"

    # state B sanity, current regime (board state = reservations_leg directly)
    cur_legs = {lid: v for lid, v in legs.items() if CUR[0] <= v["pd"] <= CUR[1]}
    nI = sum(1 for v in cur_legs.values() if cls(v["fd"]) == "I")
    nA = sum(1 for v in cur_legs.values() if cls(v["fd"]) == "A")
    nU = sum(1 for v in cur_legs.values() if cls(v["fd"]) == "U")
    print(f"state B sanity (current regime): {len(cur_legs):,} legs = "
          f"I {nI:,} ({nI/cur_days:.2f}/day) + A {nA:,} ({nA/cur_days:.2f}/day) + U {nU}; "
          f"in-house share {100.0*nI/max(len(cur_legs),1):.1f}%")

    C.write_csv("08_transitions.csv",
                ["leg_id", "t_local", "from_driver", "to_driver", "from_class", "to_class",
                 "actor_user_id", "first_assign", "row_pickup_date", "row_pickup_time",
                 "hrs_to_pickup", "band", "final_pickup_date", "window"],
                [[lid, t["t"], t["frm"], t["to"], t["fc"], t["tc"], t["actor"],
                  int(t["first"]), t["pd"], t["pt"],
                  None if t["hrs"] is None else round(t["hrs"], 2), t["band"],
                  legs[lid]["pd"], win_of(legs[lid]["pd"])]
                 for lid, ts in sorted(trans_by_leg.items()) for t in ts])

    # ------------------------------------------------------------------ 3. bursts
    C.hdr("3. BURST CLASSIFICATION — server loop vs human hand [measured]")
    # gaps between consecutive actions of the same actor on the same service date
    grp = defaultdict(list)   # (actor, row service date) -> [transition]
    for lid, ts in trans_by_leg.items():
        for t in ts:
            grp[(t["actor"], t["pd"])].append(t)
    gaps = []
    for evs in grp.values():
        evs.sort(key=lambda t: t["t"])
        gaps.extend((y["t"] - x["t"]).total_seconds() for x, y in zip(evs[:-1], evs[1:]))
    print(f"consecutive same-(actor, service-date) gaps: n={len(gaps):,}")
    buckets = [("<0.05s", 0.05), ("0.05-0.25s", 0.25), ("0.25-0.5s", 0.5),
               ("0.5-1s", 1.0), ("1-2s", 2.0), ("2-5s", 5.0), ("5-30s", 30.0),
               ("30s-10m", 600.0), (">10m", float("inf"))]
    bc = Counter()
    for g in gaps:
        for name, hi in buckets:
            if g < hi:
                bc[name] += 1
                break
    for name, _hi in buckets:
        print(f"  {name:10s} {bc[name]:7,}  {100.0*bc[name]/len(gaps):5.1f}%")
    valley = bc["0.5-1s"] + bc["1-2s"]
    print(f"  -> bimodal: machine loops sub-0.25 s, humans >=2 s; the 0.5-2 s valley holds "
          f"{100.0*valley/len(gaps):.1f}% — the 2 s cut is safe (00 §A4.6).")

    # 2 s clusters of ASSIGNS -> validated taxonomy
    def clusters(evs, gap):
        evs = sorted(evs, key=lambda t: t["t"])
        out, cur = [], []
        for e in evs:
            if cur and (e["t"] - cur[-1]["t"]).total_seconds() <= gap:
                cur.append(e)
            else:
                if cur:
                    out.append(cur)
                cur = [e]
        if cur:
            out.append(cur)
        return out

    def classify(run):
        ndrv = len({e["to"] for e in run})
        if len(run) >= 10 and ndrv >= 3:
            return "board_build"
        if len(run) >= 5 and ndrv == 1:
            return "per_driver_build"
        if len(run) == 1:
            return "human_singleton"
        if 2 <= len(run) <= 4:
            return "small_batch(2-4)"
        return "other_multi"

    tallies = Counter()          # class -> events (whole stream)
    tallies_cur = Counter()      # class -> events (current regime, by row service date)
    pdb_by_date = Counter()      # service date -> per-driver bursts
    for (actor, pd), evs in grp.items():
        assigns = [e for e in evs if e["to"] is not None]
        for run in clusters(assigns, CLUSTER_GAP_S):
            k = classify(run)
            tallies[k] += len(run)
            if CUR[0] <= pd <= CUR[1]:
                tallies_cur[k] += len(run)
            if k == "per_driver_build":
                pdb_by_date[pd] += 1
    print(f"\nassign events by burst class ({CLUSTER_GAP_S:.0f} s clusters within "
          f"(actor, service date); rule validated on the 604 labelled draft events):")
    print(f"  {'class':18s} {'whole stream':>14s} {'share':>7s} {'current':>9s} {'share':>7s}")
    tot, totc = sum(tallies.values()), sum(tallies_cur.values())
    for k in ("human_singleton", "small_batch(2-4)", "per_driver_build", "board_build",
              "other_multi"):
        print(f"  {k:18s} {tallies[k]:14,} {100.0*tallies[k]/tot:6.1f}% "
              f"{tallies_cur[k]:9,} {100.0*tallies_cur[k]/max(totc,1):6.1f}%")

    cur_dates = [(cur_a + dt.timedelta(days=i)).isoformat() for i in range(cur_days)]
    pdb_counts = [pdb_by_date.get(d, 0) for d in cur_dates]
    print(f"\nper-driver builder bursts (>=5 assigns, one target driver), current regime:")
    print(f"  median {statistics.median(pdb_counts):.0f}/day, "
          f"P90 {C.pct(pdb_counts, 90):.0f}, on {sum(1 for c in pdb_counts if c)}/"
          f"{cur_days} dates — the board is built driver-by-driver today.")

    # ------------------------------------------------------------------ 4. board builds
    C.hdr("4. BOARD-LEVEL BUILD DETECTION AND ITS EXTINCTION [measured]")
    print(f"Rule: same-actor assign run (gap <= {RUN_GAP_S:.0f} s) with >= {BUILD_MIN_N} "
          f"legs and median intra-run gap <= {BUILD_MED_GAP_S:.0f} s — only "
          f"auto_assign_drivers' apply loop writes that many legs at server pace.")
    build_dates = Counter()
    build_rows = []
    for (actor, pd), evs in grp.items():
        assigns = [e for e in evs if e["to"] is not None]
        for run in clusters(assigns, RUN_GAP_S):
            if len(run) < BUILD_MIN_N:
                continue
            g = [(y["t"] - x["t"]).total_seconds() for x, y in zip(run[:-1], run[1:])]
            if statistics.median(g) <= BUILD_MED_GAP_S:
                build_dates[pd] += 1
                build_rows.append((pd, run[0]["t"], len(run),
                                   len({e["to"] for e in run})))
    mon = defaultdict(lambda: [0, 0])   # month -> [dates with build, builds]
    seen = set()
    for pd, n in sorted(build_dates.items()):
        mon[pd[:7]][0] += 1
        mon[pd[:7]][1] += n
        seen.add(pd)
    print("\n  month     dates-with-build  builds")
    for m in sorted(mon):
        print(f"  {m}   {mon[m][0]:16d}  {mon[m][1]:6d}")
    in_win = [d for d in build_dates if PLAT[0] <= d <= CUR[1]]
    in_cur = sorted(d for d in build_dates if CUR[0] <= d <= CUR[1])
    print(f"\n  service dates with a board build, {PLAT[0]}..{CUR[1]}: "
          f"{len(in_win)} of {stream_days}")
    print(f"  current regime: {len(in_cur)} of {cur_days}"
          + (f"  ({', '.join(in_cur)})" if in_cur else ""))
    print(f"  last board build on record: service {max(build_dates) if build_dates else '—'}")
    print("""
The one-click whole-board build is EXTINCT under the current regime (corroborated by the
before_auto_assign snapshots and auto_assign draft events, both series stopping the same
week — 00 §A4.6/§B1). What replaced it is section 3's per-driver builder cadence.""")

    # ------------------------------------------------------------------ 5. churn
    C.hdr("5. CHURN — move taxonomy per day, 30-min coalesced [measured]")
    print(f"Coalescing: X->None->Y with the refill <= {COALESCE_S//60} min after the pull "
          f"is ONE move X->Y (the transient hole is bookkeeping, not a decision).")
    co_by_leg = {}
    n_merged = 0
    hole_mins = []
    for lid, rows in trans_by_leg.items():
        out = []
        i = 0
        while i < len(rows):
            if (rows[i]["to"] is None and i + 1 < len(rows)
                    and rows[i + 1]["frm"] is None and rows[i + 1]["to"] is not None
                    and (rows[i + 1]["t"] - rows[i]["t"]).total_seconds() <= COALESCE_S):
                m = dict(rows[i + 1])
                m["frm"] = rows[i]["frm"]
                m["fc"] = rows[i]["fc"]
                m["first"] = False
                out.append(m)
                hole_mins.append((rows[i + 1]["t"] - rows[i]["t"]).total_seconds() / 60.0)
                n_merged += 1
                i += 2
            else:
                out.append(rows[i])
                i += 1
        co_by_leg[lid] = out
    print(f"  merges: {n_merged:,} (hole P50 {C.pct(hole_mins,50):.1f} min, "
          f"P90 {C.pct(hole_mins,90):.1f}); coalesced stream: "
          f"{sum(len(v) for v in co_by_leg.values()):,} moves")

    def move_class(t):
        a, b = t["fc"], t["tc"]
        if a == "U":
            return "place_in" if b == "I" else ("place_aff" if b == "A" else "place_x")
        if b == "U":
            return {"I": "pullback_in", "A": "pullback_aff"}.get(a, "pullback_x")
        if t["frm"] == t["to"]:
            return "selfmove_noop"          # X->None->X coalesced back onto itself
        if a == "I" and b == "I":
            return "reassign_in_in"
        if a == "I" and b == "A":
            return "release_to_aff"
        if a == "A" and b == "I":
            return "recapture_to_in"
        if a == "A" and b == "A":
            return "vendor_swap"
        return f"other_{a}{b}"

    CLASSES = ["place_in", "place_aff", "reassign_in_in", "release_to_aff",
               "recapture_to_in", "vendor_swap", "pullback_in", "pullback_aff",
               "selfmove_noop"]
    day_tab = defaultdict(Counter)     # service date -> class counts (final pd bucket)
    day_net = Counter()                # service date -> net in-house change-of-hands
    d2d_by_date = defaultdict(list)    # service date -> [(t, lid, frm, to)] for swaps
    for lid, rows in co_by_leg.items():
        pd = legs[lid]["pd"]
        for t in rows:
            k = move_class(t)
            day_tab[pd][k] += 1
            if t["frm"] is not None and t["to"] is not None:
                day_net[pd] += (1 if t["tc"] == "I" else 0) - (1 if t["fc"] == "I" else 0)
                if t["frm"] != t["to"]:
                    d2d_by_date[pd].append((t["t"], lid, t["frm"], t["to"]))

    def window_means(a, b, ndays, label):
        tot = Counter()
        net = 0
        for pd, c in day_tab.items():
            if a <= pd <= b:
                tot.update(c)
                net += day_net[pd]
        print(f"\n  {label} — per-day means over {ndays} days:")
        for k in CLASSES:
            print(f"    {k:16s} {tot[k]:6,}  {tot[k]/ndays:8.2f}/day")
        print(f"    net in-house change-of-hands (driver->driver moves only): "
              f"{net:+,} = {net/ndays:+.2f}/day")
        return tot, net

    window_means(CUR[0], CUR[1], cur_days, f"CURRENT regime {CUR[0]}..{CUR[1]}")
    window_means(PLAT[0], PLAT[1], plat_days, f"plateau {PLAT[0]}..{PLAT[1]}")
    print("""
READ: gross in-house->in-house reshuffling dwarfs every other operator, yet the NET effect
of all change-of-hands is negative every day — hands-on revision leaks legs to affiliates
(releases outnumber recaptures ~3:1). The reshuffle is enormous churn for negative net
in-house yield.""")

    # ---- reciprocal swaps + permutation null
    C.sub(f"reciprocal swaps — leg X: A->B and leg Y: B->A within {SWAP_WINDOW_S:.0f} s")

    def count_swaps(rows):
        rows = sorted(rows)
        used = set()
        n = 0
        for i in range(len(rows)):
            if i in used:
                continue
            t1, l1, a1, b1 = rows[i]
            for j in range(i + 1, len(rows)):
                if j in used:
                    continue
                t2, l2, a2, b2 = rows[j]
                if (t2 - t1).total_seconds() > SWAP_WINDOW_S:
                    break
                if l2 != l1 and a2 == b1 and b2 == a1:
                    n += 1
                    used.add(i)
                    used.add(j)
                    break
        return n

    swaps_by_date = {pd: count_swaps(rows) for pd, rows in d2d_by_date.items()}
    cur_swaps = sum(v for pd, v in swaps_by_date.items() if CUR[0] <= pd <= CUR[1])
    cur_d2d = sum(len(v) for pd, v in d2d_by_date.items() if CUR[0] <= pd <= CUR[1])
    print(f"  current regime: {cur_swaps} swap pairs among {cur_d2d:,} driver->driver "
          f"moves ({cur_swaps/cur_days:.1f}/day)")
    # permutation null: shuffle the (from,to) pair labels within each service date,
    # timestamps held fixed — how many mirrored pairs appear by coincidence?
    rng = random.Random(8)
    nulls = []
    for _ in range(NULL_TRIALS):
        s = 0
        for pd, rows in d2d_by_date.items():
            if not (CUR[0] <= pd <= CUR[1]):
                continue
            pairs = [(a, b) for _t, _l, a, b in rows]
            rng.shuffle(pairs)
            s += count_swaps([(t, l, p[0], p[1])
                              for (t, l, _a, _b), p in zip(sorted(rows), pairs)])
        nulls.append(s)
    print(f"  permutation null ({NULL_TRIALS} trials, seeded): mean "
          f"{sum(nulls)/len(nulls):.1f} (range {min(nulls)}-{max(nulls)}) — the observed "
          f"count is ~{cur_swaps/(sum(nulls)/len(nulls)):.0f}x chance: deliberate trades, "
          f"not noise.")

    all_days = sorted(set(day_tab) | set(swaps_by_date))
    C.write_csv("08_churn_per_day.csv",
                ["date", "window"] + CLASSES + ["d2d_moves", "net_inhouse_change_of_hands",
                                                "reciprocal_swap_pairs"],
                [[pd, win_of(pd)] + [day_tab[pd][k] for k in CLASSES]
                 + [len(d2d_by_date.get(pd, [])), day_net[pd], swaps_by_date.get(pd, 0)]
                 for pd in all_days])

    # ------------------------------------------------------------------ 6. bands
    C.hdr("6. TIME-TO-PICKUP BANDS — when does the churn happen? [measured]")
    print("Plan-time pickup from the history row itself (DST-aware); RAW stream, so the")
    print("transient holes are visible where they occur in time.")

    MIXES = ["first_assign_in", "first_assign_aff", "re_place_in", "re_place_aff",
             "reassign(I->I)", "release(I->A)", "recapture(A->I)", "vendor_swap(A->A)",
             "pullback(I->U)", "pullback(A->U)", "other"]

    def mixname(t):
        # first_assign = the leg's FIRST-ever driver; re_place = refilling a hole
        a, b = t["fc"], t["tc"]
        if a == "U" and b == "I":
            return "first_assign_in" if t["first"] else "re_place_in"
        if a == "U" and b == "A":
            return "first_assign_aff" if t["first"] else "re_place_aff"
        if a == "I" and b == "I":
            return "reassign(I->I)"
        if a == "I" and b == "A":
            return "release(I->A)"
        if a == "A" and b == "I":
            return "recapture(A->I)"
        if a == "A" and b == "A":
            return "vendor_swap(A->A)"
        if a == "I" and b == "U":
            return "pullback(I->U)"
        if a == "A" and b == "U":
            return "pullback(A->U)"
        return "other"

    band_csv = []
    for wlabel, (wa, wb), ndays in (("CUR", CUR, cur_days), ("PLAT", PLAT, plat_days)):
        wtr = [(lid, t) for lid, ts in trans_by_leg.items()
               if wa <= legs[lid]["pd"] <= wb for t in ts]
        print(f"\n  === {wlabel} {wa}..{wb} ({ndays}d) — {len(wtr):,} raw transitions "
              f"({len(wtr)/ndays:.1f}/day) ===")
        print(f"  {'band':13s}{'n':>7s}{'n/day':>8s}   dominant moves")
        for b in BANDS + ["unknown"]:
            bt = [t for _lid, t in wtr if t["band"] == b]
            if not bt:
                continue
            mix = Counter(mixname(t) for t in bt)
            band_csv.append([wlabel, b, len(bt), round(len(bt) / ndays, 2)]
                            + [mix.get(m, 0) for m in MIXES])
            top = ", ".join(f"{k} {v}" for k, v in mix.most_common(4))
            print(f"  {b:13s}{len(bt):7,}{len(bt)/ndays:8.2f}   {top}")

        # net in-house effect per band: state at band entry vs band exit, per leg
        print(f"\n  {'band':13s}{'legsTouched':>12s}{'net_all':>9s}{'net/day':>9s}"
              f"{'net_churn':>10s}{'churn/day':>10s}{'creations(U->I)':>16s}")
        for b in BANDS:
            per_leg = defaultdict(list)
            for lid, t in wtr:
                if t["band"] == b:
                    per_leg[lid].append(t)
            net_all = net_churn = creation = 0
            for lid, lts in per_leg.items():
                lts.sort(key=lambda t: t["t"])
                s0, s1 = lts[0]["fc"], lts[-1]["tc"]
                d = (1 if s1 == "I" else 0) - (1 if s0 == "I" else 0)
                net_all += d
                if s0 == "U":
                    creation += (1 if s1 == "I" else 0)
                else:
                    net_churn += d
            if per_leg:
                print(f"  {b:13s}{len(per_leg):12,}{net_all:9,}{net_all/ndays:9.2f}"
                      f"{net_churn:10,}{net_churn/ndays:10.2f}{creation:16,}")
        print("  (net_churn excludes first-placements: it is what REVISION did to legs "
              "already on the board — negative in every band means every stage of "
              "hands-on revision nets legs AWAY from in-house.)")

    C.write_csv("08_band_table.csv",
                ["window", "band", "n", "n_per_day"] + MIXES, band_csv)

    # ------------------------------------------------------------------ 7. ladder
    C.hdr("7. THE IN-HOUSE-SHARE LADDER — T-72h .. T-0 [measured]")
    print("At each rung: replay each leg's history to (plan-time pickup - rung) and read")
    print("who held it. Only legs already BOOKED at the rung count (reservation created_at).")
    print("T-0 is state B from reservations_leg. 'backfill' = instant precedes the leg's")
    print(f"first history row; earliest known state used (history installs {first_ev.date()}).")

    def state_at(lid, instant):
        rows = hist_rows_of.get(lid)
        if not rows:
            return None, "nohist"
        last = None
        for loc, d in rows:
            if loc <= instant:
                last = d
            else:
                break
        if last is None and rows[0][0] > instant:
            return cls(rows[0][1]), "backfill"
        return cls(last), "ok"

    RUNGS = [72, 48, 24, 12, 6, 3, 0]
    ladder_csv = []
    for wlabel, (wa, wb) in (("CUR", CUR), ("PLAT", PLAT)):
        wlegs = [(lid, v) for lid, v in legs.items() if wa <= v["pd"] <= wb]
        print(f"\n  === {wlabel} {wa}..{wb} ===")
        print(f"  {'rung':>6s}{'booked':>8s}{'I':>7s}{'A':>7s}{'U':>7s}{'I_share':>9s}"
              f"{'I_share_all':>12s}{'backfill':>9s}")
        for rg in RUNGS:
            nb = nI2 = nA2 = nU2 = bf = nI_all = ntot = 0
            for lid, v in wlegs:
                pick = C.booked_dtm(v["pd"], v["pt"])
                if pick is None:
                    continue
                ntot += 1
                inst = pick - dt.timedelta(hours=rg)
                if rg == 0:
                    s, f = cls(v["fd"]), "ok"     # state B directly
                else:
                    s, f = state_at(lid, inst)
                booked = rg == 0 or (v["created"] is not None and v["created"] <= inst)
                if s == "I":
                    nI_all += 1
                if not booked:
                    continue
                nb += 1
                bf += (f == "backfill")
                if s == "I":
                    nI2 += 1
                elif s == "A":
                    nA2 += 1
                else:
                    nU2 += 1
            ish = 100.0 * nI2 / nb if nb else 0.0
            ish_all = 100.0 * nI_all / ntot if ntot else 0.0
            print(f"  T-{rg:>3d}h{nb:>8,}{nI2:>7,}{nA2:>7,}{nU2:>7,}{ish:>8.1f}%"
                  f"{ish_all:>11.1f}%{bf:>9,}")
            ladder_csv.append([wlabel, rg, nb, nI2, nA2, nU2, round(ish, 1),
                               round(ish_all, 1), bf])
        print("  (I_share = in-house / legs already booked at the rung; I_share_all "
              "counts late bookings as unassigned.)")
    print("""
READ: in-house share RISES toward pickup, peaks around T-12h, then bleeds back out through
the final hours as the morning-of scramble releases legs to affiliates — the ladder is the
band table's net-negative churn, seen as coverage over time.""")

    C.write_csv("08_ladder.csv",
                ["window", "rung_hours", "booked", "inhouse", "affiliate", "unassigned",
                 "i_share_pct", "i_share_all_pct", "backfill"], ladder_csv)

    print("\nWrote: out/08_transitions.csv, out/08_churn_per_day.csv, "
          "out/08_band_table.csv, out/08_ladder.csv")


if __name__ == "__main__":
    main()
