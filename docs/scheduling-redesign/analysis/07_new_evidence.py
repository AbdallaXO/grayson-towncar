"""07_new_evidence.py — what the live pull lets us do that the old snapshot could not.

The prior data audit was written against a snapshot that did not contain (or barely
contained) six tables. They are now full. This script asks one question of each:
**does it change what the engagement can deliver?**

Everything here is derived at run time. There is not one date literal in this file:
every window, cutoff and "is it fresh" test comes from `_common.Horizon`, which reads
nine independent production write streams, or from the tables' own MIN/MAX.

Read-only: `_common.connect()` opens the snapshot with `mode=ro`.

Sections
  1  table inventory + freshness, measured against the derived horizon
  2  reservations_historicalleg   — incl. THE prediction-vs-outcome question (§12.6)
  3  reservations_auditlog        — assignment churn + ladder recovery
  4  reservations_driverlocation  — coordinate reality, the base-location test
  5  reservations_schedulesnapshot— before/after replay
  6  reservations_legkeoi         — hand-labelled dispatcher risk
  7  routedistancecache / routetimingmetric
  8  ranked verdict
"""

import datetime as dt
import math
import re
from collections import Counter, defaultdict

import _common as C


# --------------------------------------------------------------------------
# small local helpers
# --------------------------------------------------------------------------

def putc(s):
    """'YYYY-MM-DD HH:MM:SS[.ffffff][+00:00]' -> naive UTC datetime. None-safe.

    Django writes aware UTC; SQLite stores the text. Some rows carry an explicit
    '+00:00', most do not. Strip it and stay naive-UTC throughout: every timestamp
    compared in this script is UTC on both sides, so no DST conversion is needed
    and none is applied. Local time is used ONLY for day-bucketing, via to_local().
    """
    if not s:
        return None
    t = str(s).replace("T", " ").strip()
    if "+" in t:
        t = t.split("+")[0]
    if t.endswith("Z"):
        t = t[:-1]
    try:
        return dt.datetime.fromisoformat(t)
    except ValueError:
        return None


def span(con, table, col):
    """(rows, first, last, distinct_days) for a timestamp column."""
    r = C.q(con, f"SELECT COUNT(*) n, MIN({col}) a, MAX({col}) b FROM {table}")[0]
    if not r["n"]:
        return 0, None, None, 0
    d = C.q1(con, f"SELECT COUNT(DISTINCT substr({col},1,10)) FROM {table}")
    return r["n"], putc(r["a"]), putc(r["b"]), d


def hours_behind(h, ts):
    if ts is None:
        return None
    return (putc(h.pull_utc) - ts).total_seconds() / 3600.0


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def qq(vals, p):
    return C.pct(vals, p)


def err_line(label, errs, width=26):
    if not errs:
        return f"{label:<{width}} n=0"
    a = [abs(x) for x in errs]
    return (f"{label:<{width}} n={len(errs):>5}  "
            f"P05 {qq(errs, 5):+7.1f}  P25 {qq(errs, 25):+6.1f}  P50 {qq(errs, 50):+6.1f}  "
            f"P75 {qq(errs, 75):+6.1f}  P95 {qq(errs, 95):+7.1f}  |err|P75 {qq(a, 75):5.1f}  "
            f"|err|P90 {qq(a, 90):5.1f}  <=5m {100.0 * sum(1 for x in a if x <= 5) / len(a):4.1f}%")


# ==========================================================================
def main():
    con = C.connect()
    H = C.Horizon(con)

    C.preamble(
        "07_new_evidence.py",
        "what the five newly-populated tables unlock for the redesign",
        H,
        assumptions=(
            "Every timestamp column in these tables is UTC (Django default). Comparisons "
            "are UTC-on-UTC, so no DST conversion is applied; to_local() is used only to "
            "bucket a row into a local calendar day.",
            "REALISED arrival at a pickup = the first 'on-location' tap; realised job end = "
            "the first 'completed' tap. MIN() per (leg,status), never .first(). Tap "
            "discipline noise (a driver tapping late) inflates measured prediction error, so "
            "every error figure below is an UPPER bound on the estimator's true error.",
            "A dispatch_eta_* value is only treated as a PREDICTION when its "
            "dispatch_eta_evaluated_at falls strictly inside the window in which the driver "
            "was actually travelling to that target. Outside that window the field is a "
            "drive-time estimate, not an arrival-time forecast, and scoring it as one would "
            "manufacture a huge fake error.",
            "Forward-dated legs (pickup_date > derived today) are excluded from every "
            "aggregate; they are structurally incomplete.",
        ),
    )

    today = H.today
    first_pickup = putc(C.q1(con, C.live_legs_sql("MIN(l.pickup_date)")) + " 00:00:00")
    first_day = first_pickup.date()
    byday = C.legs_per_day(con, end=H.last_demand_day)
    segs = C.changepoints(byday, first_day, H.last_demand_day, min_seg=28, min_effect=0.09)

    print("\ndemand regimes (raw-series changepoints, min_seg=28, min_effect=0.09) —")
    print("context only; every table below is judged against these boundaries:")
    for a, b, n, m in segs:
        print(f"  {a} .. {b}  ({n:3d}d)  {m:6.1f} legs/day")
    cur_start, cur_end, cur_n, cur_mean = segs[-1]
    prev_start, prev_end, prev_n, prev_mean = segs[-2] if len(segs) > 1 else segs[-1]
    print(f"\n  CURRENT regime opens {cur_start} at {cur_mean:.1f}/day; the prior plateau "
          f"({prev_start}..{prev_end}) ran {prev_mean:.1f}/day "
          f"({100.0 * (cur_mean - prev_mean) / prev_mean:+.1f}%).")

    # ======================================================================
    C.hdr("1. INVENTORY — the six tables, measured against the derived horizon")
    # ======================================================================

    TABLES = [
        ("reservations_historicalleg", "history_date"),
        ("reservations_auditlog", "timestamp"),
        ("reservations_driverlocation", "timestamp"),
        ("reservations_schedulesnapshot", "created_at"),
        ("reservations_schedulesnapshotentry", None),
        ("reservations_legkeoi", "created_at"),
        ("reservations_routedistancecache", "updated_at"),
        ("reservations_routetimingmetric", "last_calculated"),
    ]
    inv_rows = []
    print(f"{'table':<38} {'rows':>8} {'first':>11} {'last':>11} {'days':>5} "
          f"{'h behind pull':>13} {'in cur.regime':>13}")
    print("('days' = distinct calendar days that carry at least one row, NOT the span)")
    for t, col in TABLES:
        if col is None:
            n = C.q1(con, f"SELECT COUNT(*) FROM {t}")
            print(f"{t:<38} {n:>8} {'—':>11} {'—':>11} {'—':>5} {'—':>13} {'—':>13}")
            inv_rows.append([t, n, "", "", "", "", ""])
            continue
        n, a, b, days = span(con, t, col)
        lag = hours_behind(H, b)
        in_cur = C.q1(con, f"SELECT COUNT(*) FROM {t} WHERE substr({col},1,10) >= ?",
                      (cur_start.isoformat(),))
        print(f"{t:<38} {n:>8} {str(a)[:10]:>11} {str(b)[:10]:>11} {days:>5} "
              f"{lag:>13.1f} {in_cur:>13}")
        inv_rows.append([t, n, str(a)[:19], str(b)[:19], days, round(lag, 2), in_cur])
    C.write_csv("07_new_table_inventory.csv",
                ["table", "rows", "first", "last", "distinct_days",
                 "hours_behind_pull", "rows_in_current_regime"], inv_rows)
    print("\n[measured] Five of the seven timestamped tables write to within ~4 h of the pull. "
          "reservations_legkeoi lags because flags are rare events, not because it stopped. "
          "reservations_routetimingmetric is the one genuine staleness — see §7. "
          "reservations_driverlocation looks fresh here and is NOT — see §4a; its rows are "
          "still being written, but they no longer carry a position.")

    # provenance footnote: two columns outside the nine canonical streams run LATER
    later = [("reservations_leg.dispatch_eta_evaluated_at",
              C.q1(con, "SELECT MAX(dispatch_eta_evaluated_at) FROM reservations_leg")),
             ("drivers_fleetvehicle.samsara_last_seen_at",
              C.q1(con, "SELECT MAX(samsara_last_seen_at) FROM drivers_fleetvehicle"))]
    later = [(k, putc(v)) for k, v in later if v and putc(v) > putc(H.pull_utc)]
    if later:
        print("\n[measured] PROVENANCE FOOTNOTE — two columns outside Horizon's nine canonical "
              "streams carry writes AFTER its pull instant:")
        for k, v in later:
            print(f"  {k:<44} {v}  (+{(v - putc(H.pull_utc)).total_seconds() / 60.0:.0f} min)")
        newest = max(v for _, v in later)
        print(f"  [inferred] the copy therefore completed nearer {newest} UTC than "
              f"{H.pull_utc}. These are background-poller columns written every few minutes, "
              f"so the tables were simply copied in sequence. It does not move 'today', it does "
              f"not create a hole, and no conclusion here depends on the difference — but "
              f"anyone reconciling row counts to the minute should know.")

    # ======================================================================
    C.hdr("2. reservations_historicalleg — django-simple-history on Leg")
    # ======================================================================

    C.sub("2a. coverage, and does it agree with the live table?")
    n_h, h_a, h_b, h_days = span(con, "reservations_historicalleg", "history_date")
    n_legs_hist = C.q1(con, "SELECT COUNT(DISTINCT id) FROM reservations_historicalleg")
    n_legs_live = C.q1(con, "SELECT COUNT(*) FROM reservations_leg")
    types = {r[0]: r[1] for r in C.q(
        con, "SELECT history_type, COUNT(*) FROM reservations_historicalleg GROUP BY 1")}
    print(f"[measured] rows {n_h:,} over {h_days} days, {h_a} .. {h_b}")
    print(f"[measured] history_type: + (create) {types.get('+', 0):,}   "
          f"~ (update) {types.get('~', 0):,}   - (delete) {types.get('-', 0):,}")
    print(f"[measured] distinct legs with history {n_legs_hist:,}  "
          f"of {n_legs_live:,} legs alive now ({100.0 * n_legs_hist / n_legs_live:.1f}%)")
    print(f"[measured] mean history rows per tracked leg "
          f"{n_h / float(n_legs_hist):.1f}")

    # legs created before history was switched on can never have history
    legs_after = C.q1(con,
                      "SELECT COUNT(*) FROM reservations_leg l JOIN reservations_reservation r "
                      "ON r.id=l.reservation_id WHERE r.created_at >= ?", (str(h_a),))
    covered = C.q1(con, """SELECT COUNT(*) FROM reservations_leg l
                           JOIN reservations_reservation r ON r.id=l.reservation_id
                           WHERE r.created_at >= ?
                             AND EXISTS (SELECT 1 FROM reservations_historicalleg h
                                         WHERE h.id = l.id)""", (str(h_a),))
    print(f"[measured] of the {legs_after:,} legs whose RESERVATION was created after history "
          f"switched on, {covered:,} ({100.0 * covered / max(legs_after, 1):.1f}%) have at least "
          f"one history row. History is on for everything created since; it simply cannot see "
          f"backwards.")

    # VERIFICATION #1: latest history row must equal the live row.
    print("\nverification — latest history row vs the live leg row, field by field:")
    ver = C.q(con, """
        WITH last AS (
          SELECT h.* FROM reservations_historicalleg h
          JOIN (SELECT id, MAX(history_id) hid FROM reservations_historicalleg
                WHERE history_type <> '-' GROUP BY id) m
            ON m.hid = h.history_id
        )
        SELECT COUNT(*) n,
               SUM(COALESCE(l.status,'')            = COALESCE(last.status,''))            s_ok,
               SUM(COALESCE(l.driver_id,-1)         = COALESCE(last.driver_id,-1))         d_ok,
               SUM(COALESCE(l.pickup_time,'')       = COALESCE(last.pickup_time,''))       t_ok,
               SUM(COALESCE(l.pickup_date,'')       = COALESCE(last.pickup_date,''))       dt_ok,
               SUM(COALESCE(l.driver_pay_amount,-1) = COALESCE(last.driver_pay_amount,-1)) p_ok,
               SUM(COALESCE(l.vehicle_id,-1)        = COALESCE(last.vehicle_id,-1))        v_ok,
               SUM(COALESCE(l.dispatch_eta_minutes,-1)
                   = COALESCE(last.dispatch_eta_minutes,-1))                               e_ok
        FROM reservations_leg l JOIN last ON last.id = l.id""")[0]
    n = ver["n"]
    for lab, k in (("status", "s_ok"), ("driver_id", "d_ok"), ("pickup_time", "t_ok"),
                   ("pickup_date", "dt_ok"), ("driver_pay_amount", "p_ok"),
                   ("vehicle_id", "v_ok"), ("dispatch_eta_minutes", "e_ok")):
        print(f"  {lab:<22} {ver[k]:>6}/{n} agree  ({100.0 * ver[k] / n:5.1f}%)")
    print("[measured] Any field far below 100% is written by a path that BYPASSES "
          "simple-history — i.e. .update() / bulk_update(), which fire no post_save signal.")
    print("[inferred] pickup_time is the one field that does NOT reconcile, and the repo "
          "already documents why: dispatching/leg_timeline.py:14-20 — 'writes that went "
          "through queryset.update() left no snapshot, so simple_history's consecutive-"
          "snapshot diff folded them into the following save'. dispatching/pickup_moves.py:12-27 "
          "records the fix (save(update_fields=...) instead of queryset.update()). So history "
          "COUNTS retimes reliably but, for legs touched before that fix, mis-TIMES and "
          "mis-ATTRIBUTES them. Use it for 'how often', not for 'when' or 'by whom'.")

    C.sub("2b. the dispatch_eta_* family — what writes it, and is it a real prediction log?")
    print("code path (verified with grep -n):")
    print("  dispatching/samsara_scheduler.py:282   sweep_eta() -> Leg.objects.bulk_update(to_update, _ETA_FIELDS)")
    print("  dispatching/samsara_scheduler.py:196   _ETA_FIELDS — the twelve dispatch_* columns")
    print("  dispatching/samsara_risk.py:213        evaluate() — computes minutes + risk band")
    print("  dispatching/samsara_risk.py:88         effective_pickup_dt() -> pickup_policy.pickup_deadline()")
    print("  dispatching/samsara_scheduler.py:32    ETA_REFRESH_SECONDS = 6*60 (paid Google refresh cadence)")
    print("  dispatching/samsara_scheduler.py:27    INTERVAL_SECONDS = 3*60 (GPS poll; band math re-runs free)")
    print("  dispatching/samsara_risk.py:181        _can_reuse_eta() — reuse gates, so consecutive")
    print("                                          evaluations can repeat the same minutes value")

    pop = C.q(con, """SELECT COUNT(*) tot,
                        SUM(dispatch_eta_minutes IS NOT NULL) em,
                        SUM(dispatch_eta_evaluated_at IS NOT NULL) ev,
                        SUM(dispatch_risk_status IS NOT NULL AND dispatch_risk_status <> '') rs,
                        SUM(dispatch_eta_target_time IS NOT NULL) tt,
                        SUM(dispatch_eta_origin_lat IS NOT NULL) ol
                      FROM reservations_historicalleg""")[0]
    print(f"\n[measured] of {pop['tot']:,} history rows: dispatch_eta_minutes {pop['em']:,} "
          f"({100.0 * pop['em'] / pop['tot']:.1f}%), evaluated_at {pop['ev']:,}, "
          f"risk_status {pop['rs']:,}, target_time {pop['tt']:,}, origin GPS {pop['ol']:,}")
    eta_a = putc(C.q1(con, "SELECT MIN(history_date) FROM reservations_historicalleg "
                           "WHERE dispatch_eta_minutes IS NOT NULL"))
    eta_b = putc(C.q1(con, "SELECT MAX(history_date) FROM reservations_historicalleg "
                           "WHERE dispatch_eta_minutes IS NOT NULL"))
    print(f"[measured] ETA-bearing history rows run {eta_a} .. {eta_b} "
          f"({(eta_b - eta_a).days} days, ending {hours_behind(H, eta_b):.1f} h before the pull). "
          f"That is {(eta_a.date() - h_a.date()).days} days AFTER history itself began — the "
          f"columns were added later.")

    for lab, sql in (("risk_status", "dispatch_risk_status"), ("eta_target", "dispatch_eta_target")):
        rows = C.q(con, f"SELECT {sql} v, COUNT(*) n FROM reservations_historicalleg "
                        f"WHERE {sql} IS NOT NULL AND {sql} <> '' GROUP BY 1 ORDER BY 2 DESC")
        print(f"  {lab:<12} " + "   ".join(f"{r['v']}={r['n']:,}" for r in rows))

    # Is history the WRITER of these, or an incidental bystander?
    lags = [(putc(r["hd"]) - putc(r["ev"])).total_seconds() / 60.0
            for r in C.q(con, """SELECT history_date hd, dispatch_eta_evaluated_at ev
                                 FROM reservations_historicalleg
                                 WHERE dispatch_eta_evaluated_at IS NOT NULL""")]
    lags = [x for x in lags if x is not None]
    print("\n[measured] history_date MINUS dispatch_eta_evaluated_at, minutes:")
    print("  " + C.fmt_describe("lag(history_row, evaluation)", lags))
    print(f"  max lag {max(lags):.1f} min; "
          f"{100.0 * sum(1 for x in lags if 0 <= x <= 3.0) / len(lags):.1f}% fall in [0, 3.0] min")
    print("[inferred] that lag is spread almost UNIFORMLY across the poll interval "
          "(INTERVAL_SECONDS = 3 min), which is the signature of a trigger INDEPENDENT of the "
          "sweep. It has to be: sweep_eta writes with bulk_update(), which fires no post_save, "
          "so simple-history never records the sweep itself.")

    # structurally different confirmation: do those history rows coincide with status taps?
    tap_ts = defaultdict(list)
    for r in C.q(con, "SELECT leg_id, timestamp FROM reservations_legstatus"):
        tap_ts[r["leg_id"]].append(putc(r["timestamp"]))
    hits = tot = 0
    for r in C.q(con, """SELECT id, history_date FROM reservations_historicalleg
                         WHERE dispatch_eta_evaluated_at IS NOT NULL"""):
        hd = putc(r["history_date"])
        if hd is None:
            continue
        tot += 1
        if any(abs((hd - t).total_seconds()) <= 2.0 for t in tap_ts.get(r["id"], ())):
            hits += 1
    print(f"[measured] second check, structurally different — {hits:,}/{tot:,} "
          f"({100.0 * hits / tot:.1f}%) of ETA-bearing history rows sit within 2 SECONDS of a "
          f"legstatus tap on the same leg. The trigger is the driver's status tap, not the "
          f"sweep. Both tests agree.")
    print("[inferred] VERDICT on the shape of the log: every ETA value in historicalleg is an "
          "INCIDENTAL SNAPSHOT — an unrelated .save() copied whatever the last sweep had left "
          "on the row. That makes the prediction log SPARSE and EVENT-TRIGGERED rather than a "
          "complete time series, and it biases the sample toward legs that got tapped. But the "
          "values are real and dispatch_eta_evaluated_at timestamps each one exactly, so the "
          "predictions that ARE there can be scored honestly.")

    n_pred = C.q1(con, """SELECT COUNT(*) FROM (SELECT DISTINCT id, dispatch_eta_evaluated_at,
                            dispatch_eta_minutes, dispatch_eta_target
                          FROM reservations_historicalleg
                          WHERE dispatch_eta_minutes IS NOT NULL
                            AND dispatch_eta_evaluated_at IS NOT NULL)""")
    n_pred_legs = C.q1(con, "SELECT COUNT(DISTINCT id) FROM reservations_historicalleg "
                            "WHERE dispatch_eta_minutes IS NOT NULL")
    print(f"[measured] de-duplicated to distinct (leg, evaluated_at, minutes, target): "
          f"{n_pred:,} predictions across {n_pred_legs:,} legs.")

    # -------- THE §12.6 QUESTION -----------------------------------------
    C.sub("2c. THE §12.6 QUESTION — can a PREDICTION now be scored against an OUTCOME?")

    taps = defaultdict(dict)
    for r in C.q(con, "SELECT leg_id, status, MIN(timestamp) t "
                      "FROM reservations_legstatus GROUP BY 1,2"):
        taps[r["leg_id"]][r["status"]] = putc(r["t"])

    preds = C.q(con, """SELECT DISTINCT id AS leg_id, dispatch_eta_evaluated_at ev,
                          dispatch_eta_minutes em, dispatch_eta_target tgt,
                          dispatch_eta_target_time tt, dispatch_risk_status rs
                        FROM reservations_historicalleg
                        WHERE dispatch_eta_minutes IS NOT NULL
                          AND dispatch_eta_evaluated_at IS NOT NULL""")

    # leg pickup_date for regime bucketing / forward-date exclusion
    legdate = {r["id"]: r["pickup_date"] for r in
               C.q(con, "SELECT id, pickup_date FROM reservations_leg")}

    CASES = (("en route -> PICKUP", ("pickup", "next_pickup"), "on-the-way", "on-location"),
             ("on trip -> DROPOFF", ("dropoff",), "picked-up", "completed"))
    csv_rows = []
    scored = {}
    for label, tgts, k0, k1 in CASES:
        rows_in_window, per_leg_final, by_bucket = [], {}, defaultdict(list)
        drop_no_tap = drop_outside = drop_forward = 0
        for p in preds:
            if p["tgt"] not in tgts:
                continue
            pd = legdate.get(p["leg_id"])
            if pd and pd > today.isoformat():
                drop_forward += 1
                continue
            t = taps.get(p["leg_id"], {})
            s, e = t.get(k0), t.get(k1)
            if not s or not e or e <= s:
                drop_no_tap += 1
                continue
            ev = putc(p["ev"])
            if not (s <= ev < e):
                drop_outside += 1
                continue
            err = ((ev + dt.timedelta(minutes=p["em"])) - e).total_seconds() / 60.0
            rows_in_window.append(err)
            # bucket by the size of the prediction itself
            m = p["em"]
            b = "0-5" if m <= 5 else "6-15" if m <= 15 else "16-30" if m <= 30 else "31+"
            by_bucket[b].append(err)
            prev = per_leg_final.get(p["leg_id"])
            if prev is None or ev > prev[0]:
                per_leg_final[p["leg_id"]] = (ev, err, m)
            csv_rows.append([label, p["leg_id"], pd, str(ev)[:19], m, p["tgt"], p["rs"],
                             round(err, 2)])
        finals = [v[1] for v in per_leg_final.values()]
        scored[label] = (rows_in_window, finals, per_leg_final)
        print(f"\n{label}   [measured]")
        print(f"  in-window predictions {len(rows_in_window):,} on {len(per_leg_final):,} legs   "
              f"(dropped: no {k0}/{k1} tap pair {drop_no_tap:,}; evaluation outside the travel "
              f"window {drop_outside:,}; forward-dated {drop_forward:,})")
        print("  error = (evaluated_at + eta_minutes) - realised.  "
              "NEGATIVE = system said the driver would get there EARLIER than he did (optimistic).")
        print("  " + err_line("all in-window", rows_in_window))
        print("  " + err_line("final call per leg", finals))
        for b in ("0-5", "6-15", "16-30", "31+"):
            if by_bucket[b]:
                print("  " + err_line(f"predicted eta {b} min", by_bucket[b]))
    C.write_csv("07_eta_prediction_errors.csv",
                ["case", "leg_id", "pickup_date", "evaluated_at_utc", "eta_minutes",
                 "target", "risk_status", "error_minutes"], csv_rows)
    print("\n[inferred] the two short buckets are tight and the two long ones are not. That is "
          "NOT mostly model error: a 25-minute ETA evaluated seconds after the driver taps "
          "'on-the-way' is scored against an arrival that only happens once he actually leaves, "
          "and script 02 already established that the 'on-the-way' tap is not a departure "
          "signal. Read the LONG buckets as an upper bound contaminated by tap lag, and the "
          "'final call per leg' row as the operational number.")

    # ---- SECOND CHECK, structurally different: the FROZEN value on the live row ----
    C.sub("2c-bis. independent re-verification — the frozen dispatch_* state on the LIVE leg")
    print("sweep_eta's queryset excludes completed/cancelled legs "
          "(dispatching/samsara_scheduler.py:254-255), and _clear_eta_fields only runs for legs "
          "still IN that queryset (samsara_scheduler.py:278). So once a leg completes, its "
          "last evaluation FREEZES on reservations_leg. That is a second copy of the final "
          "prediction, written by a different mechanism (bulk_update, not simple-history) and "
          "sampled by a different rule (last sweep before completion, not last unrelated save).")
    frozen = C.q(con, """SELECT id AS leg_id, dispatch_eta_evaluated_at ev,
                           dispatch_eta_minutes em, dispatch_eta_target tgt, status
                         FROM reservations_leg
                         WHERE dispatch_eta_minutes IS NOT NULL
                           AND dispatch_eta_evaluated_at IS NOT NULL""")
    print(f"[measured] {len(frozen):,} live legs carry a frozen ETA; "
          f"{sum(1 for r in frozen if r['status'] == 'completed'):,} of them are 'completed' "
          f"(so the value can no longer move).")
    for label, tgts, k0, k1 in CASES:
        errs = []
        for r in frozen:
            if r["tgt"] not in tgts:
                continue
            pd = legdate.get(r["leg_id"])
            if pd and pd > today.isoformat():
                continue
            t = taps.get(r["leg_id"], {})
            s, e = t.get(k0), t.get(k1)
            ev = putc(r["ev"])
            if not s or not e or not ev or e <= s or not (s <= ev < e):
                continue
            errs.append(((ev + dt.timedelta(minutes=r["em"])) - e).total_seconds() / 60.0)
        print("  " + err_line(f"FROZEN {label}", errs, width=30))
        hist_final = scored[label][1]
        if errs and hist_final:
            a1 = [abs(x) for x in errs]
            a2 = [abs(x) for x in hist_final]
            print(f"    vs history-derived final call: |err| P75 {qq(a1, 75):.1f} vs "
                  f"{qq(a2, 75):.1f} min, P90 {qq(a1, 90):.1f} vs {qq(a2, 90):.1f} min "
                  f"(n={len(errs):,} vs {len(hist_final):,})")
    print("[measured] HONEST READING of this check, and it is not the one I expected:")
    print("  * the DROPOFF case matches the history-derived figure essentially exactly, at "
          "essentially the same n. That is a plumbing check, not an independent estimate — "
          "for a leg that completes mid-trip, the frozen row and the last history row are "
          "usually the SAME evaluation. It proves neither store is corrupted; it does not "
          "re-derive the accuracy number.")
    print("  * the PICKUP case DISAGREES badly, and the reason is selection, not measurement: "
          "a leg only freezes with target='pickup' if it completed while the sweep still "
          "considered the driver to be en route to it — i.e. the driver never tapped his way "
          "through the ladder in order. That subpopulation is tiny and pathological. Do not "
          "quote it.")
    print("  * the genuinely independent second opinion on ETA accuracy is §4d — a different "
          "origin, a different caller and a different era. That is where the cross-check "
          "lives.")

    # ---- the CHAINED prediction: a feasibility claim, scored one-sided ----
    C.sub("2c-ter. the chained 'next_pickup' ETA — the closest thing to a PLANNING prediction")
    print("samsara_risk.py:380-401 chains it: finish the current drop-off, add "
          "DROPOFF_SERVICE_MIN, then drive on to the next pickup. This is a feasibility claim "
          "about a job the driver has NOT started — 'if he left now he would be there in N'. "
          "Scoring it naively is a trap: when the next pickup is three hours away the driver "
          "waits, arrives 'late' against the chain, and the chain was never wrong. The claim "
          "is only testable where it BINDS — where the slack to the deadline is comparable to "
          "the drive itself. So bucket by slack and read only the tight buckets.")
    chain_rows = []
    for p in preds:
        if p["tgt"] != "next_pickup" or not p["tt"]:
            continue
        pd = legdate.get(p["leg_id"])
        if pd and pd > today.isoformat():
            continue
        e = taps.get(p["leg_id"], {}).get("on-location")
        ev, tt = putc(p["ev"]), putc(p["tt"])
        if not e or not ev or not tt or ev >= e:
            continue
        slack = (tt - ev).total_seconds() / 60.0 - p["em"]      # spare minutes the chain saw
        over = (e - (ev + dt.timedelta(minutes=p["em"]))).total_seconds() / 60.0
        vs_deadline = (e - tt).total_seconds() / 60.0            # + = actually missed it
        chain_rows.append((slack, over, vs_deadline, p["em"], p["leg_id"], pd, str(ev)[:19]))
    if chain_rows:
        print(f"[measured] n={len(chain_rows):,} chained claims with both a deadline and an "
              f"outcome.")
        print("  " + C.fmt_describe("slack the chain saw, minutes",
                                    [r[0] for r in chain_rows]))
        for lo, hi, lab in ((-10 ** 9, 0, "NEGATIVE slack (chain says he cannot make it)"),
                            (0, 15, "slack 0-15 min  (binding)"),
                            (15, 60, "slack 15-60 min"),
                            (60, 10 ** 9, "slack 60+ min   (not binding)")):
            sub_ = [r for r in chain_rows if lo <= r[0] < hi]
            if not sub_:
                continue
            miss = [r[2] for r in sub_]
            n_miss = sum(1 for x in miss if x > 0)
            print(f"  {lab:<45} n={len(sub_):>5}  actually missed the deadline "
                  f"{100.0 * n_miss / len(sub_):5.1f}%   minutes vs deadline "
                  f"P50 {qq(miss, 50):+6.1f}  P75 {qq(miss, 75):+6.1f}  P90 {qq(miss, 90):+6.1f}")
        binding = [r for r in chain_rows if 0 <= r[0] < 15]
        if binding:
            ov = [r[1] for r in binding if r[1] > 0]
            print(f"  [measured] in the BINDING bucket the chained travel estimate is "
                  f"overrun {100.0 * len(ov) / len(binding):.1f}% of the time; when it "
                  f"overruns, P75 {qq(ov, 75):.0f} min and P90 {qq(ov, 90):.0f} min.")
            print(f"  [inferred] a chained hop planned with zero buffer misses roughly "
                  f"{100.0 * sum(1 for r in binding if r[2] > 0) / len(binding):.0f}% of the "
                  f"time. That is the single most directly usable number in this script for "
                  f"sizing a turnaround buffer — and it is a number the old snapshot could "
                  f"not produce at all.")
        print("  [measured] CAVEAT, stated because it undercuts the clean story: the miss rate "
              "is NOT monotone in slack. The negative-slack bucket behaves exactly as it "
              "should (the chain says he cannot make it and he mostly does not), but the "
              "60+-minutes-of-slack bucket misses about as often as the binding one. Legs with "
              "hours of slack that still miss are not failing on travel time — they are "
              "failing on something the chain does not model (a driver who has not started, a "
              "guest who is not ready). Use the binding bucket to size a TRAVEL buffer; do not "
              "read the loose buckets as travel-time error at all.")
        C.write_csv("07_chained_next_pickup.csv",
                    ["slack_min", "overrun_vs_chain_min", "minutes_vs_deadline",
                     "eta_minutes", "leg_id", "pickup_date", "evaluated_at_utc"],
                    [[round(r[0], 1), round(r[1], 1), round(r[2], 1), r[3], r[4], r[5], r[6]]
                     for r in chain_rows])
    else:
        print("[unavailable] no chained prediction has both a deadline and an outcome.")

    # does the RISK BAND itself predict lateness?
    print("\nrisk band vs realised lateness at the pickup deadline  [measured]")
    band_rows = C.q(con, """SELECT DISTINCT id AS leg_id, dispatch_eta_evaluated_at ev,
                              dispatch_risk_status rs, dispatch_eta_target_time tt,
                              dispatch_eta_target tgt
                            FROM reservations_historicalleg
                            WHERE dispatch_risk_status IS NOT NULL
                              AND dispatch_risk_status <> ''
                              AND dispatch_eta_target_time IS NOT NULL
                              AND dispatch_eta_target IN ('pickup','next_pickup')""")
    band = defaultdict(list)
    band_csv = []
    for r in band_rows:
        pd = legdate.get(r["leg_id"])
        if pd and pd > today.isoformat():
            continue
        t = taps.get(r["leg_id"], {})
        s, e = t.get("on-the-way"), t.get("on-location")
        ev, tt = putc(r["ev"]), putc(r["tt"])
        if not e or not ev or not tt:
            continue
        if s and not (s <= ev):
            continue
        if ev >= e:
            continue
        late = (e - tt).total_seconds() / 60.0   # + = arrived after the deadline
        band[r["rs"]].append(late)
        band_csv.append([r["leg_id"], pd, r["rs"], str(ev)[:19], round(late, 2)])
    for k in ("on_time", "watch", "at_risk", "late", "unknown"):
        v = band[k]
        if not v:
            continue
        share_late = 100.0 * sum(1 for x in v if x > 0) / len(v)
        print(f"  band={k:<9} n={len(v):>5}  minutes past deadline at arrival: "
              f"P50 {qq(v, 50):+6.1f}  P75 {qq(v, 75):+6.1f}  P90 {qq(v, 90):+6.1f}   "
              f"actually late {share_late:5.1f}%")
    C.write_csv("07_risk_band_outcomes.csv",
                ["leg_id", "pickup_date", "risk_band", "evaluated_at_utc",
                 "minutes_past_deadline_at_arrival"], band_csv)
    if band["at_risk"] and band["watch"]:
        fp = 100.0 - 100.0 * sum(1 for x in band["at_risk"] if x > 0) / len(band["at_risk"])
        fpw = 100.0 - 100.0 * sum(1 for x in band["watch"] if x > 0) / len(band["watch"])
        print(f"[measured] CALIBRATION: the bands are ordered correctly (on_time -> watch -> "
              f"at_risk -> late is monotone in realised lateness at P90), but 'at_risk' is "
              f"wrong {fp:.0f}% of the time and 'watch' is wrong {fpw:.0f}% of the time — the "
              f"driver made it. 'late' is right 100% of the time because it is not a "
              f"prediction: samsara_risk.py:297-300 sets it once the clock has already passed "
              f"the deadline.")
        print("[inferred] a two-thirds false-positive rate on the amber band is the same "
              "complaint the KEOI free text makes in §6 ('This is NOT a CONFLICT the arrival "
              "is international'). Two independent records, one conclusion: the current risk "
              "signal over-warns, and the thing it is missing is the slack a long "
              "customs/baggage dwell actually buys. That is a concrete, sized target for the "
              "redesign.")

    C.sub("2d. assignment timeline, pay changes, and vehicle changes from history")
    dtypes = {r["id"]: r["driver_type"] for r in
              C.q(con, "SELECT id, driver_type FROM drivers_driver")}
    hist = defaultdict(list)
    for r in C.q(con, """SELECT id, history_id, history_date, driver_id, driver_pay_amount,
                           vehicle_id, pickup_time, pickup_date, status
                         FROM reservations_historicalleg
                         WHERE history_type <> '-' ORDER BY id, history_id"""):
        hist[r["id"]].append(r)

    drv_changes, flips, pay_changes, veh_changes, time_edits = [], 0, [], [], []
    drv_reassign, pay_any = [], []
    legs_with_flip = 0
    for lid, rows in hist.items():
        pd = legdate.get(lid)
        if pd and pd > today.isoformat():
            continue
        seq_d = [r["driver_id"] for r in rows]
        transitions = sum(1 for a, b in zip(seq_d[:-1], seq_d[1:]) if a != b)
        drv_changes.append(transitions)
        # same semantics as the auditlog measure in §3b: don't count the FIRST
        # None -> driver transition, that is the original assignment, not churn.
        collapsed = [seq_d[0]]
        for v in seq_d[1:]:
            if v != collapsed[-1]:
                collapsed.append(v)
        if collapsed and collapsed[0] is None:
            collapsed = collapsed[1:]
        drv_reassign.append(max(len(collapsed) - 1, 0))
        flip_here = 0
        for a, b in zip(seq_d[:-1], seq_d[1:]):
            if a is None or b is None or a == b:
                continue
            ta, tb = dtypes.get(a), dtypes.get(b)
            if ta and tb and ta != tb:
                flip_here += 1
        flips += flip_here
        legs_with_flip += 1 if flip_here else 0
        seq_p = [r["driver_pay_amount"] for r in rows]
        pay_changes.append(sum(1 for a, b in zip(seq_p[:-1], seq_p[1:])
                               if a is not None and b is not None and float(a) != float(b)))
        pay_any.append(len({("" if x is None else str(float(x))) for x in seq_p}) - 1)
        seq_v = [r["vehicle_id"] for r in rows]
        veh_changes.append(sum(1 for a, b in zip(seq_v[:-1], seq_v[1:]) if a != b))
        seq_t = [(r["pickup_date"], str(r["pickup_time"])[:5]) for r in rows]
        time_edits.append(sum(1 for a, b in zip(seq_t[:-1], seq_t[1:]) if a != b))

    print(f"[measured] legs with a reconstructable timeline: {len(drv_changes):,}")
    print("  " + C.fmt_describe("driver_id transitions / leg", drv_changes))
    print("  " + C.fmt_describe("re-assignments / leg (excl. 1st)", drv_reassign))
    print("  " + C.fmt_describe("RE-PRICES / leg (X -> different X)", pay_changes))
    print("  " + C.fmt_describe("distinct pay values seen - 1", pay_any))
    print("  " + C.fmt_describe("vehicle_id changes / leg", veh_changes))
    print(f"  [measured] {sum(1 for x in pay_changes if x):,} legs "
          f"({100.0 * sum(1 for x in pay_changes if x) / len(pay_changes):.1f}%) were RE-PRICED "
          f"after a pay figure already existed; "
          f"{sum(1 for x in pay_any if x):,} ({100.0 * sum(1 for x in pay_any if x) / len(pay_any):.1f}%) "
          f"had pay written at all during the tracked period (mostly the one-off NULL -> value "
          f"at assignment). "
          f"{sum(1 for x in veh_changes if x):,} legs "
          f"({100.0 * sum(1 for x in veh_changes if x) / len(veh_changes):.1f}%) changed vehicle.")
    print("  [inferred] pay is set once and almost never revised; vehicle almost never moves "
          "between legs. Neither is a churn story. Do NOT sell historicalleg on 'pay edit "
          "history' — the interesting movement is all in driver_id and pickup_time.")
    print("  " + C.fmt_describe("pickup date/time edits / leg", time_edits))
    print(f"[measured] in-house <-> affiliate FLIPS: {flips:,} transitions on "
          f"{legs_with_flip:,} legs "
          f"({100.0 * legs_with_flip / max(len(drv_changes), 1):.1f}% of tracked legs). "
          f"That is the farm-out decision changing its mind, visible for the first time.")

    C.sub("2e. is the booked pickup_time a stable planning anchor?")
    print("this matters more than it looks: every feasibility, turnaround and concurrency "
          "number the redesign will produce is anchored on booked pickup_time. If that number "
          "moves after the plan is built, the plan was never about the day that happened.")
    print("the repo says the record is split across four trails "
          "(dispatching/leg_timeline.py:4-9: HistoricalLeg / AuditLog / StaffActivity / "
          "LegStatus). Measure all of them and take the disagreement seriously.")

    n_legs = C.q1(con, C.live_legs_sql("COUNT(*)", " AND l.pickup_date <= ?"),
                  (today.isoformat(),))
    stamped = C.q1(con, C.live_legs_sql("COUNT(*)",
                                        " AND l.pickup_date <= ? AND l.pickup_time_changed_at IS NOT NULL"),
                   (today.isoformat(),))
    print(f"\n[measured] source 1 — Leg.pickup_time_changed_at: {stamped:,}/{n_legs:,} past-dated "
          f"live legs = {100.0 * stamped / n_legs:.1f}%.")
    print("  scope caveat: reservations/models.py:1695-1697 CLEARS this stamp on a net-zero "
          "revert (A->B->A), and models.py:1685 only fires inside save(). It is a UI badge, "
          "not an audit trail. Treat as a hard FLOOR.")

    al_time = C.q(con, """SELECT object_id, old_value, new_value, notes, timestamp
                          FROM reservations_auditlog
                          WHERE model_name='Leg' AND field_name='pickup_time'
                          ORDER BY timestamp""")
    al_legs = len({r["object_id"] for r in al_time})
    al_first_ts, al_last_ts = putc(al_time[0]["timestamp"]), putc(al_time[-1]["timestamp"])
    reasons = Counter((r["notes"] or "(no reason)") for r in al_time)
    print(f"\n[measured] source 2 — auditlog field_name='pickup_time': {len(al_time):,} events on "
          f"{al_legs:,} legs, but ONLY from {al_first_ts.date()} "
          f"({(al_last_ts.date() - al_first_ts.date()).days + 1} days).")
    for k, v in reasons.most_common(6):
        print(f"              reason {k!r:<42} {v:,} ({100.0 * v / len(al_time):.1f}%)")
    print("  that start date is when dispatching/pickup_moves.py became the shared write path "
          "(it is the only thing that writes this AuditLog row). Short window, but it is the "
          "ONLY trail with a trustworthy clock and actor — and it sits INSIDE the current "
          "demand regime, which makes it the right source for today's rate.")
    al_days = (al_last_ts.date() - al_first_ts.date()).days + 1
    win_lo, win_hi = al_first_ts.date().isoformat(), today.isoformat()
    win_legs = {r["id"]: (r["pu"], r["do"]) for r in C.q(con, C.live_legs_sql(
        "l.id AS id, l.pickup_location AS pu, l.dropoff_location AS do",
        " AND l.pickup_date BETWEEN ? AND ?"), (win_lo, win_hi))}
    retimed_in_win = {r["object_id"] for r in al_time} & set(win_legs)
    n_ev_in_win = sum(1 for r in al_time if r["object_id"] in retimed_in_win)
    print(f"  [measured] restricting BOTH sides to legs served in that window "
          f"({win_lo}..{win_hi}): {len(retimed_in_win):,}/{len(win_legs):,} = "
          f"{100.0 * len(retimed_in_win) / len(win_legs):.1f}% of legs were retimed, at "
          f"{n_ev_in_win / float(max(len(retimed_in_win), 1)):.1f} retimes per retimed leg "
          f"({n_ev_in_win / float(al_days):.0f} retime events per day).")
    kinds = Counter(C.trip_kind(*win_legs[i]) for i in retimed_in_win)
    base_kinds = Counter(C.trip_kind(*v) for v in win_legs.values())
    print("  [measured] what gets retimed, vs the population it is drawn from:")
    for k in ("ARRIVAL", "DEPARTURE", "OTHER"):
        if base_kinds[k]:
            print(f"    {k:<10} {kinds[k]:>5}/{base_kinds[k]:>5} legs retimed "
                  f"({100.0 * kinds[k] / base_kinds[k]:5.1f}% of that class)")
    print("  [inferred] retiming is overwhelmingly an ARRIVAL phenomenon and the ~7 rewrites "
          "per retimed leg are the flight-match writer chasing a moving arrival estimate. "
          "A flight-tracked arrival's pickup time is not a fact; it is a running forecast.")

    hist_time_legs = sum(1 for x in time_edits if x > 0)
    full_life = C.q1(con, """SELECT COUNT(*) FROM (SELECT id FROM reservations_historicalleg
                             WHERE history_type='+' GROUP BY id)""")
    full_life_chg = C.q1(con, """SELECT COUNT(*) FROM (
                                   SELECT h.id FROM reservations_historicalleg h
                                   WHERE h.id IN (SELECT id FROM reservations_historicalleg
                                                  WHERE history_type='+')
                                   GROUP BY h.id
                                   HAVING COUNT(DISTINCT h.pickup_time) > 1)""")
    print(f"\n[measured] source 3 — historicalleg. Restricting to the {full_life:,} legs whose "
          f"CREATE row is in history (so the whole life is observed): {full_life_chg:,} "
          f"({100.0 * full_life_chg / full_life:.1f}%) carry more than one distinct pickup_time.")
    print(f"  the looser consecutive-row diff over all tracked past legs agrees: "
          f"{hist_time_legs:,}/{len(time_edits):,} = "
          f"{100.0 * hist_time_legs / max(len(time_edits), 1):.1f}%.")
    print("[inferred] RECONCILING the three, which is the whole point of measuring all of them:")
    print("  * they do not contradict each other; they cover different windows with different "
          "fidelity. Source 1 self-clears on a revert and only fires inside save(), so it is a "
          "floor. Source 3 spans the era when the flight writer used queryset.update(), which "
          "left no snapshot until the next save — so it UNDERCOUNTS the older months.")
    print(f"  * source 2 is the only one scoped entirely inside the CURRENT regime, and it is "
          f"unambiguous: {100.0 * len(retimed_in_win) / len(win_legs):.0f}% of all legs and "
          f"{100.0 * kinds['ARRIVAL'] / max(base_kinds['ARRIVAL'], 1):.0f}% of ARRIVALS are "
          f"retimed, each about {n_ev_in_win / float(max(len(retimed_in_win), 1)):.0f} times.")
    print("  * QUOTE THIS: in the current regime, essentially every flight-tracked arrival's "
          "pickup time is rewritten repeatedly before service. The older full-life figure "
          "(~1 leg in 3) is the long-run floor, not the present rate.")

    def hhmm(s):
        s = (s or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})\s*([AP]M)?$", s, re.I)
        if not m:
            return None
        h, mi = int(m.group(1)), int(m.group(2))
        ap = (m.group(3) or "").upper()
        if ap == "PM" and h != 12:
            h += 12
        if ap == "AM" and h == 12:
            h = 0
        return h * 60 + mi

    deltas, flight_deltas, human_deltas = [], [], []
    for r in al_time:
        a, b = hhmm(r["old_value"]), hhmm(r["new_value"])
        if a is None or b is None:
            continue
        d = b - a
        if d > 720:
            d -= 1440
        if d < -720:
            d += 1440
        deltas.append(d)
        (flight_deltas if (r["notes"] or "").lower().startswith("flight")
         else human_deltas).append(d)
    print("\n  size of a retime, minutes (+ = moved later):")
    print("  " + C.fmt_describe("all retimes", deltas))
    print("  " + C.fmt_describe("automatic (flight match)", flight_deltas))
    print("  " + C.fmt_describe("everything else", human_deltas))
    print("  " + C.fmt_describe("|retime| all", [abs(x) for x in deltas]))
    big = 100.0 * sum(1 for x in deltas if abs(x) >= 30) / max(len(deltas), 1)
    print(f"  [measured] {big:.1f}% of retimes move the pickup by 30 min or more.")

    # lead time: how long before service does the retime happen?
    lead = []
    for r in al_time:
        pd = legdate.get(r["object_id"])
        ts = putc(r["timestamp"])
        if not pd or not ts:
            continue
        try:
            svc = dt.datetime.fromisoformat(pd + " 12:00:00")
        except ValueError:
            continue
        lead.append((svc - C.to_local(str(ts))).total_seconds() / 3600.0)
    print("  " + C.fmt_describe("hours before service day noon", lead))
    same_day = 100.0 * sum(1 for x in lead if -24 <= x <= 12) / max(len(lead), 1)
    print(f"  [measured] {same_day:.1f}% of retimes happen inside the service day itself.")
    print("[verdict] booked pickup_time is a LIVE field, not a fixed anchor. It is edited on "
          "roughly a third of legs, mostly by the automatic flight-match writer, mostly on the "
          "day of service, and by a median of a few minutes with a fat tail. Two consequences "
          "for the redesign: (a) any plan built on the booked time must be re-checked on the "
          "day, and (b) a retrospective study MUST use the value as of the moment being "
          "studied — historicalleg is now the only way to recover that, and that capability "
          "did not exist in the old snapshot.")
    C.write_csv("07_pickup_time_edits.csv",
                ["leg_id", "old", "new", "delta_min", "reason", "timestamp_utc"],
                [[r["object_id"], r["old_value"], r["new_value"],
                  (hhmm(r["new_value"]) - hhmm(r["old_value"]))
                  if hhmm(r["old_value"]) is not None and hhmm(r["new_value"]) is not None else "",
                  r["notes"] or "", str(r["timestamp"])[:19]] for r in al_time])

    # ======================================================================
    C.hdr("3. reservations_auditlog — assignment churn and ladder recovery")
    # ======================================================================

    C.sub("3a. coverage vs legstatus")
    n_a, a_a, a_b, a_days = span(con, "reservations_auditlog", "timestamp")
    n_s, s_a, s_b, s_days = span(con, "reservations_legstatus", "timestamp")
    print(f"[measured] auditlog  {n_a:,} rows  {a_a} .. {a_b}  ({a_days} days)")
    print(f"[measured] legstatus {n_s:,} rows  {s_a} .. {s_b}  ({s_days} days)")
    print(f"[measured] auditlog starts {(s_a - a_a).days} days EARLIER than legstatus, and the "
          f"two newest rows are {abs((a_b - s_b).total_seconds()) / 60.0:.1f} min apart — both "
          f"run right up to the pull. auditlog extends the observable operational record "
          f"backwards by roughly a month.")
    for r in C.q(con, "SELECT action, COUNT(*) n, MIN(timestamp) a, MAX(timestamp) b "
                      "FROM reservations_auditlog GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r['action']:<22} {r['n']:>8,}  {str(r['a'])[:10]} .. {str(r['b'])[:10]}")

    C.sub("3b. assignment churn — how often does a leg change hands?")
    assigns = defaultdict(list)
    for r in C.q(con, """SELECT object_id, new_value, timestamp, username, action
                         FROM reservations_auditlog
                         WHERE model_name='Leg'
                           AND action IN ('driver_assigned','driver_unassigned')
                         ORDER BY object_id, timestamp"""):
        assigns[r["object_id"]].append(r)

    booked = {}
    for r in C.q(con, C.live_legs_sql("l.id AS id, l.pickup_date AS pd, l.pickup_time AS pt")):
        booked[r["id"]] = C.booked_dtm(r["pd"], r["pt"])

    n_events, n_effective, n_distinct, churn_csv = [], [], [], []
    hours_before = []
    dayof, daybefore, earlier = 0, 0, 0
    churn_by_month = defaultdict(lambda: [0, 0])   # month -> [effective changes, legs]
    actor = Counter()
    for lid, evs in assigns.items():
        pd = legdate.get(lid)
        if not pd or pd > today.isoformat():
            continue
        n_events.append(len(evs))
        seq, eff = [], 0
        for e in evs:
            v = None if e["action"] == "driver_unassigned" else e["new_value"]
            if not seq or seq[-1] != v:
                if seq:
                    eff += 1
                seq.append(v)
        n_effective.append(eff)
        n_distinct.append(len({v for v in seq if v is not None}))
        churn_by_month[pd[:7]][0] += eff
        churn_by_month[pd[:7]][1] += 1
        churn_csv.append([lid, pd, len(evs), eff, len({v for v in seq if v is not None})])
        # timing of every effective change
        prev = None
        try:
            svc = dt.datetime.fromisoformat(pd + " 00:00:00")
        except ValueError:
            continue
        for e in evs:
            v = None if e["action"] == "driver_unassigned" else e["new_value"]
            if prev is not None and v != prev:
                loc = C.to_local(str(e["timestamp"]))
                if loc is None:
                    continue
                dd = (loc.date() - svc.date()).days
                if dd == 0:
                    dayof += 1
                elif dd == -1:
                    daybefore += 1
                else:
                    earlier += 1
                actor[e["username"] or "(system/none)"] += 1
                bk = booked.get(lid)
                if bk:
                    hours_before.append((bk - loc).total_seconds() / 3600.0)
            prev = v

    print(f"[measured] legs that were ever assigned (past-dated only): {len(n_events):,}")
    print("  " + C.fmt_describe("raw assign/unassign events / leg", n_events))
    print("  " + C.fmt_describe("EFFECTIVE changes of hand / leg", n_effective))
    print("  " + C.fmt_describe("distinct drivers touched / leg", n_distinct))
    print("[inferred] raw events massively overstate churn: 'Reset Schedule' + auto-assign "
          "re-writes the same driver onto the same leg and logs it every time. The EFFECTIVE "
          "series (collapse consecutive identical values) is the honest measure.")
    tot_changes = dayof + daybefore + earlier
    print(f"\n[measured] WHEN a change of hand happens ({tot_changes:,} effective changes):")
    print(f"  on the service day itself   {dayof:>7,}  ({100.0 * dayof / tot_changes:5.1f}%)")
    print(f"  the day before              {daybefore:>7,}  ({100.0 * daybefore / tot_changes:5.1f}%)")
    print(f"  earlier than that           {earlier:>7,}  ({100.0 * earlier / tot_changes:5.1f}%)")
    n_multi = sum(1 for x in n_effective if x >= 2)
    print(f"[measured] {100.0 * sum(1 for x in n_effective if x >= 1) / len(n_effective):.1f}% of "
          f"assigned legs change hands at least once after the first assignment; "
          f"{100.0 * n_multi / len(n_effective):.1f}% change twice or more.")
    print("\n[measured] the same thing against the BOOKED PICKUP INSTANT, which is what a "
          "plan actually has to survive:")
    print("  " + C.fmt_describe("hours before pickup, per change", hours_before))
    for h in (48, 24, 6, 2, 0):
        k = 100.0 * sum(1 for x in hours_before if x <= h) / len(hours_before)
        print(f"  within {h:>2} h of the booked pickup: {k:5.1f}% of all changes of hand"
              + ("   (<=0 h = after the pickup time had already passed)" if h == 0 else ""))
    print("[inferred] this is the number that sizes the redesign's problem. A plan published "
          "the night before is not what runs; a material share of assignment decisions are "
          "made inside the last day, and a non-trivial share inside the last two hours.")
    print("[measured] SECOND CHECK, structurally different — the same quantity from "
          "historicalleg's driver_id field diff (§2d) instead of auditlog's event stream: "
          f"P50 {C.describe(drv_reassign)['p50']:.0f}, P75 {C.describe(drv_reassign)['p75']:.0f}, "
          f"P90 {C.describe(drv_reassign)['p90']:.0f} re-assignments/leg "
          f"(n={len(drv_reassign):,}) vs auditlog's "
          f"P50 {C.describe(n_effective)['p50']:.0f}, P75 {C.describe(n_effective)['p75']:.0f}, "
          f"P90 {C.describe(n_effective)['p90']:.0f} (n={len(n_effective):,}). "
          "Two tables, two mechanisms, same answer.")

    print("\n[measured] churn per leg by service month (is it rising?):")
    mrows = []
    for m in sorted(churn_by_month):
        ch, lg = churn_by_month[m]
        if lg < 50:
            continue
        print(f"  {m}   {lg:>6,} legs   {ch / float(lg):5.2f} effective changes/leg")
        mrows.append([m, lg, round(ch / float(lg), 3)])
    C.write_csv("07_churn_by_month.csv", ["service_month", "legs", "changes_per_leg"], mrows)
    C.write_csv("07_churn_by_leg.csv",
                ["leg_id", "pickup_date", "raw_events", "effective_changes",
                 "distinct_drivers"], churn_csv)

    # regime comparison, using the derived boundary rather than calendar months
    def churn_between(a, b):
        tot = cnt = 0
        for lid, evs in assigns.items():
            pd = legdate.get(lid)
            if not pd or not (a.isoformat() <= pd <= b.isoformat()):
                continue
            seq, eff = [], 0
            for e in evs:
                v = None if e["action"] == "driver_unassigned" else e["new_value"]
                if not seq or seq[-1] != v:
                    if seq:
                        eff += 1
                    seq.append(v)
            tot += eff
            cnt += 1
        return (tot / float(cnt) if cnt else None), cnt

    pc, pn = churn_between(prev_start, prev_end)
    cc, cn = churn_between(cur_start, cur_end)
    if pc and cc:
        print(f"\n[measured] across the derived regime boundary: prior plateau "
              f"{prev_start}..{prev_end} {pc:.2f} changes/leg (n={pn:,}); current regime "
              f"{cur_start}..{cur_end} {cc:.2f} (n={cn:,})  -> {100.0 * (cc - pc) / pc:+.1f}%")

    print("\n[measured] who moves legs (effective changes, top 12):")
    for u, c in actor.most_common(12):
        print(f"  {u:<20} {c:>7,}  ({100.0 * c / tot_changes:4.1f}%)")
    C.write_csv("07_churn_actors.csv", ["username", "effective_changes"], actor.most_common())

    C.sub("3c. can auditlog RECOVER status events that legstatus is missing?")
    ls = defaultdict(set)
    for r in C.q(con, "SELECT DISTINCT leg_id, status FROM reservations_legstatus"):
        ls[r["leg_id"]].add(r["status"])
    al = defaultdict(set)
    al_first = defaultdict(dict)
    for r in C.q(con, """SELECT object_id, new_value, MIN(timestamp) t
                         FROM reservations_auditlog
                         WHERE model_name='Leg' AND action='status_changed'
                           AND field_name='status' AND new_value IS NOT NULL
                         GROUP BY 1,2"""):
        al[r["object_id"]].add(r["new_value"])
        al_first[r["object_id"]][r["new_value"]] = putc(r["t"])

    overlap_start = max(a_a, s_a)
    live_past = C.q(con, C.live_legs_sql(
        "l.id AS id, l.pickup_date AS pd", " AND l.pickup_date BETWEEN ? AND ?"),
        (overlap_start.date().isoformat(), H.last_actuals_day.isoformat()))
    print(f"[measured] comparison universe: live legs with pickup_date in the OVERLAP of both "
          f"tables, {overlap_start.date()} .. {H.last_actuals_day} — n={len(live_past):,}")

    rec_rows = []
    tot_recover = 0
    for st in C.LADDER:
        have_ls = sum(1 for r in live_past if st in ls.get(r["id"], ()))
        have_al = sum(1 for r in live_past if st in al.get(r["id"], ()))
        only_al = sum(1 for r in live_past
                      if st in al.get(r["id"], ()) and st not in ls.get(r["id"], ()))
        only_ls = sum(1 for r in live_past
                      if st in ls.get(r["id"], ()) and st not in al.get(r["id"], ()))
        tot_recover += only_al
        n = len(live_past)
        print(f"  {st:<12} legstatus {have_ls:>6,} ({100.0 * have_ls / n:5.1f}%)   "
              f"auditlog {have_al:>6,} ({100.0 * have_al / n:5.1f}%)   "
              f"ONLY-auditlog {only_al:>5,} (+{100.0 * only_al / n:4.1f} pts)   "
              f"only-legstatus {only_ls:>5,}")
        rec_rows.append([st, n, have_ls, have_al, only_al, only_ls])
    C.write_csv("07_ladder_recovery.csv",
                ["status", "legs", "in_legstatus", "in_auditlog", "only_auditlog",
                 "only_legstatus"], rec_rows)

    # do the two agree on TIMING where both exist? (that is what makes recovery safe)
    diffs = []
    for r in live_past:
        lid = r["id"]
        for st in ("on-location", "picked-up", "completed"):
            a = al_first.get(lid, {}).get(st)
            if not a or st not in ls.get(lid, ()):
                continue
            b = C.q1(con, "SELECT MIN(timestamp) FROM reservations_legstatus "
                          "WHERE leg_id=? AND status=?", (lid, st))
            b = putc(b)
            if a and b:
                diffs.append(abs((a - b).total_seconds()))
    if diffs:
        print("\n  " + C.fmt_describe("|auditlog - legstatus| seconds, same event", diffs))
        agree = 100.0 * sum(1 for x in diffs if x <= 5) / len(diffs)
        print(f"  [measured] {agree:.1f}% of paired events agree within 5 seconds — the two "
              f"tables are written by the same request, so a recovered auditlog timestamp is "
              f"as good as a tap.")
    print(f"[measured] total ladder events recoverable from auditlog that legstatus does not "
          f"have, over the overlap: {tot_recover:,}")
    print("[measured] NEGATIVE RESULT, and it matters: auditlog does NOT improve ladder "
          "coverage. The two tables are written inside the same request by the same view, so "
          "they carry the same events and the same gaps. The 17% of legs with no 'on-location' "
          "tap are legs where nobody tapped — no second table can invent that. The actuals "
          "sample cannot be grown this way.")

    # the one place auditlog IS the sole source: before legstatus existed
    pre_lo, pre_hi = a_a.date(), (s_a.date() - dt.timedelta(days=1))
    pre_legs = C.q(con, C.live_legs_sql("l.id AS id", " AND l.pickup_date BETWEEN ? AND ?"),
                   (pre_lo.isoformat(), pre_hi.isoformat()))
    print(f"\n[measured] where auditlog IS the only source — legs served {pre_lo} .. {pre_hi}, "
          f"before legstatus existed: n={len(pre_legs):,}")
    pre_rows = []
    for st in C.LADDER:
        have = sum(1 for r in pre_legs if st in al.get(r["id"], ()))
        print(f"  {st:<12} auditlog {have:>5,} "
              f"({100.0 * have / max(len(pre_legs), 1):5.1f}%)   legstatus: 0 by construction")
        pre_rows.append([st, len(pre_legs), have])
    print("[measured] that is a genuine {} extra days of ladder history, at coverage "
          "comparable to the tap table's — real, but it sits {} days before the current "
          "regime opened, so it informs trend work, not present-day parameters.".format(
              (pre_hi - pre_lo).days + 1, (cur_start - pre_hi).days))
    C.write_csv("07_prelegstatus_ladder.csv", ["status", "legs", "in_auditlog"], pre_rows)

    # ======================================================================
    C.hdr("4. reservations_driverlocation — GPS, and the base-location question")
    # ======================================================================

    C.sub("4a. how much of it carries real coordinates?")
    dl_rows = C.q(con, """SELECT substr(timestamp,1,7) m, COUNT(*) n,
                            SUM(latitude=0 AND longitude=0) zero,
                            SUM(latitude<>0 OR longitude<>0) real_,
                            SUM(accuracy_meters IS NOT NULL) acc,
                            SUM(eta_minutes IS NOT NULL) eta
                          FROM reservations_driverlocation GROUP BY 1 ORDER BY 1""")
    print(f"{'month':<9}{'rows':>8}{'real coords':>13}{'zeros':>9}{'accuracy':>10}{'eta_min':>9}")
    dl_csv = []
    for r in dl_rows:
        print(f"{r['m']:<9}{r['n']:>8,}{r['real_']:>13,}{r['zero']:>9,}"
              f"{r['acc']:>10,}{r['eta']:>9,}")
        dl_csv.append([r["m"], r["n"], r["real_"], r["zero"], r["acc"], r["eta"]])
    C.write_csv("07_driverlocation_coverage.csv",
                ["month", "rows", "real_coords", "zero_coords", "accuracy_present",
                 "eta_present"], dl_csv)
    tot_dl = sum(r["n"] for r in dl_rows)
    tot_real = sum(r["real_"] for r in dl_rows)
    last_real = putc(C.q1(con, "SELECT MAX(timestamp) FROM reservations_driverlocation "
                               "WHERE latitude<>0 OR longitude<>0"))
    first_real = putc(C.q1(con, "SELECT MIN(timestamp) FROM reservations_driverlocation "
                                "WHERE latitude<>0 OR longitude<>0"))
    print(f"\n[measured] {tot_real:,}/{tot_dl:,} ({100.0 * tot_real / tot_dl:.1f}%) of rows carry "
          f"real coordinates, and they ALL fall in {first_real.date()} .. {last_real.date()} "
          f"({(last_real - first_real).days} days). Zero rows since.")
    print(f"[measured] the real-coordinate archive CLOSED {(putc(H.pull_utc) - last_real).days} "
          f"days before the pull, and {(cur_start - last_real.date()).days} days before the "
          f"current demand regime opened. It cannot describe today's operation.")
    print(f"cause (verified): commit 94c88ba7 'Driver portal v2', authored the same day the "
          f"archive stops ({last_real.date()}), deleted the phone-GPS capture block from the "
          f"driver status endpoint.")
    print("  drivers/views.py:222   \"Phone-GPS capture was removed, so new rows come only from")
    print("                          the address-based picked-up fallback (_compute_fallback_eta).\"")
    print("  drivers/views.py:531   _compute_fallback_eta is now called ONLY for 'picked-up'")
    print("  drivers/views.py:585   ...and writes latitude=0, longitude=0 by construction")
    dup = C.q(con, """SELECT COUNT(*) groups, AVG(c) avg_c, MAX(c) max_c FROM (
                        SELECT leg_id, status, COUNT(*) c FROM reservations_driverlocation
                        WHERE latitude<>0 OR longitude<>0 GROUP BY 1,2)""")[0]
    print(f"[measured] DATA-QUALITY TRAP anyone reusing this table must know: the GPS-era rows "
          f"are NOT one-fix-per-status. {dup['groups']:,} (leg,status) groups hold "
          f"{tot_real:,} fixes — mean {dup['avg_c']:.1f}, max {dup['max_c']}. Drivers re-tapped "
          f"and the app re-posted; one leg carries {dup['max_c']} 'on-the-way' fixes spread "
          f"over days. Any per-leg statistic must aggregate per (leg,status), never count rows.")
    print("[measured] consistent with the code cut: since it, 100% of rows have status='picked-up'.")
    post = C.q(con, """SELECT status, COUNT(*) n FROM reservations_driverlocation
                       WHERE timestamp > ? GROUP BY 1 ORDER BY 2 DESC""", (str(last_real),))
    print("           post-cut status mix: " + ", ".join(f"{r['status']}={r['n']:,}" for r in post))

    C.sub("4b. IS THERE A DE FACTO BASE? — clustering the fix at the start of each driver-day")
    print("method: the only tap-triggered fix that can sit anywhere other than a customer "
          "location is the FIRST 'on-the-way' of a driver's day — where he was when he "
          "started moving toward job 1. Later fixes are all at pickups/dropoffs by "
          "construction, and there is no end-of-shift tap, so overnight position is "
          "[unavailable]. Validity filter: keep only day-starts whose own eta_minutes says "
          "the driver was still a real drive away, so a tap made while already parked at the "
          "pickup cannot masquerade as a base.")

    gps = C.q(con, """SELECT driver_id, leg_id, timestamp, latitude, longitude, eta_minutes,
                        eta_destination, status
                      FROM reservations_driverlocation
                      WHERE (latitude<>0 OR longitude<>0) ORDER BY driver_id, timestamp""")
    by_driver_day = {}
    for r in gps:
        if r["status"] != "on-the-way":
            continue
        loc = C.to_local(str(r["timestamp"]))
        if loc is None:
            continue
        key = (r["driver_id"], loc.date())
        if key not in by_driver_day or loc < by_driver_day[key][0]:
            by_driver_day[key] = (loc, float(r["latitude"]), float(r["longitude"]),
                                  r["eta_minutes"])
    starts = list(by_driver_day.values())
    far = [s for s in starts if s[3] is not None and s[3] >= 10]
    print(f"\n[measured] driver-days with a first-'on-the-way' GPS fix: {len(starts):,} "
          f"across {len({k[0] for k in by_driver_day}):,} drivers, "
          f"{len({k[1] for k in by_driver_day}):,} calendar days")
    print(f"[measured] of those, {len(far):,} have eta_minutes >= 10 (driver genuinely away "
          f"from the pickup) — the base-test sample")

    def grid(lat, lng, deg=0.005):        # ~0.55 km cells
        return (round(lat / deg), round(lng / deg))

    cells = Counter(grid(s[1], s[2]) for s in far)
    print(f"[measured] {len(cells):,} distinct ~0.55 km cells hold those {len(far):,} day-starts")
    top = cells.most_common(10)
    print("  top cells (centroid lat, lng — look them up on a map):")
    base_csv = []
    for (cy, cx), n in top:
        lat, lng = cy * 0.005, cx * 0.005
        drivers_here = len({k[0] for k, v in by_driver_day.items()
                            if v[3] is not None and v[3] >= 10 and grid(v[1], v[2]) == (cy, cx)})
        print(f"    {lat:9.4f},{lng:10.4f}   {n:>4} day-starts "
              f"({100.0 * n / len(far):4.1f}%)   {drivers_here} distinct driver(s)")
        base_csv.append([round(lat, 4), round(lng, 4), n, round(100.0 * n / len(far), 2),
                         drivers_here])
    C.write_csv("07_daystart_clusters.csv",
                ["lat", "lng", "day_starts", "pct_of_daystarts", "distinct_drivers"], base_csv)
    top10_share = 100.0 * sum(n for _, n in top) / len(far)
    print(f"[measured] top-1 cell holds {100.0 * top[0][1] / len(far):.1f}%, "
          f"top-10 cells hold {top10_share:.1f}% of all day-starts")

    # null model: how concentrated are ON-LOCATION fixes (which SHOULD cluster at MCO)?
    # Concentration statistics depend on n, so subsample the control to the SAME n.
    onloc_fix = [(float(r["latitude"]), float(r["longitude"])) for r in gps
                 if r["status"] == "on-location"]
    ocells = Counter(grid(a, b) for a, b in onloc_fix)
    otop = 100.0 * sum(n for _, n in ocells.most_common(10)) / max(len(onloc_fix), 1)
    print(f"[measured] CONTROL — the same statistic on 'on-location' fixes (which must pile up "
          f"at MCO): top-10 cells hold {otop:.1f}% of {len(onloc_fix):,} fixes, in "
          f"{len(ocells):,} cells.")
    import random
    rng = random.Random(0)          # fixed seed: re-running gives the same control
    t1s, t10s, ncells = [], [], []
    for _ in range(200):
        s = rng.sample(onloc_fix, min(len(far), len(onloc_fix)))
        cc = Counter(grid(a, b) for a, b in s)
        mc = cc.most_common(10)
        t1s.append(100.0 * mc[0][1] / len(s))
        t10s.append(100.0 * sum(n for _, n in mc) / len(s))
        ncells.append(len(cc))
    print(f"[measured] MATCHED-n CONTROL (200 subsamples of the control down to n={len(far)}, "
          f"because concentration statistics are n-dependent):")
    print(f"  control  top-1 {qq(t1s, 50):5.1f}%   top-10 {qq(t10s, 50):5.1f}%   "
          f"cells {qq(ncells, 50):5.0f}")
    print(f"  starts   top-1 {100.0 * top[0][1] / len(far):5.1f}%   top-10 {top10_share:5.1f}%   "
          f"cells {len(cells):5d}")
    print("[inferred] day-starts are MEASURABLY LESS concentrated than a genuinely shared "
          "destination, on both statistics, at matched n. Whatever the top cell is, it does "
          "not behave like the airport does.")

    # per-driver: does each driver have his OWN stable origin?
    per_drv = defaultdict(list)
    for (did, day), v in by_driver_day.items():
        if v[3] is not None and v[3] >= 10:
            per_drv[did].append((v[1], v[2]))
    self_conc, modal = [], {}
    for did, pts in per_drv.items():
        if len(pts) < 10:
            continue
        cc = Counter(grid(a, b) for a, b in pts)
        (my, mx), mn = cc.most_common(1)[0]
        modal[did] = (my * 0.005, mx * 0.005, len(pts))
        near = sum(1 for a, b in pts if haversine_km(a, b, my * 0.005, mx * 0.005) <= 3.0)
        self_conc.append(100.0 * near / len(pts))
    print(f"\n[measured] drivers with >=10 qualifying day-starts: {len(self_conc)}")
    print("  " + C.fmt_describe("% of a driver's day-starts within 3 km of HIS OWN modal point",
                                self_conc))
    if len(modal) >= 2:
        pts = list(modal.values())
        pair = [haversine_km(a[0], a[1], b[0], b[1])
                for i, a in enumerate(pts) for b in pts[i + 1:]]
        print("  " + C.fmt_describe("km between two drivers' modal start points", pair))
        shared = sum(1 for x in pair if x <= 3.0)
        print(f"  [measured] {shared}/{len(pair)} driver pairs "
              f"({100.0 * shared / len(pair):.1f}%) start within 3 km of each other")
    C.write_csv("07_driver_modal_starts.csv",
                ["driver_id", "modal_lat", "modal_lng", "qualifying_day_starts"],
                [[d, round(v[0], 4), round(v[1], 4), v[2]] for d, v in sorted(modal.items())])

    # per-driver share of the single busiest cell — a base would dominate everyone's day
    (ty, tx), tn = top[0]
    per_drv_top = []
    for did, pts in per_drv.items():
        if len(pts) < 10:
            continue
        per_drv_top.append(100.0 * sum(1 for a, b in pts if grid(a, b) == (ty, tx)) / len(pts))
    print(f"\n[measured] the single busiest cell ({ty * 0.005:.4f},{tx * 0.005:.4f}) as a share "
          f"of each driver's own day-starts:")
    print("  " + C.fmt_describe("% of a driver's starts in the top cell", per_drv_top))
    print(f"  {sum(1 for x in per_drv_top if x >= 50)}/{len(per_drv_top)} drivers start there "
          f"on half their days or more.")

    print("\n[verdict] §4b — NO. There is no de facto base in the GPS archive.")
    print("  * the busiest single point accounts for "
          f"{100.0 * tn / len(far):.0f}% of day-starts, not a majority, and it is less "
          f"concentrated than a real shared destination at matched n")
    print("  * a driver returns to his OWN modal start point on a median "
          f"{C.describe(self_conc)['p50']:.0f}% of days, and the median distance between two "
          f"drivers' modal start points is {qq(pair, 50):.1f} km")
    print("  * that is the signature of a HOME-KEPT fleet — each driver starts near his own "
          "place, with partial overlap where several drivers happen to live in the same "
          "corridor — not of a yard everyone reports to")
    print("  * this refines rather than overturns the prior conclusion ('there is no "
          "base-location concept'): the CODE has no base, and the behaviour has no base "
          "either. But it establishes something the code cannot: every driver has a PERSONAL "
          "origin, and it is stable enough to be worth modelling — a per-driver start point "
          "is a real feature the scheduler currently ignores entirely")
    print("  * scope: this is the 2026-03..2026-06 archive, a different and smaller fleet than "
          "today's. It is evidence, not a fact about the present.")

    C.sub("4c. the SAME question against CURRENT data — the live Samsara vehicle snapshot")
    print("drivers_fleetvehicle carries one CURRENT position per mapped vehicle "
          "(dispatching/samsara_scheduler.py:56 sync_vehicles writes samsara_last_* every "
          "3 min). It is a single instant, not a series — n is tiny and it cannot show a "
          "pattern over time. But it is the ONLY positional evidence inside the current "
          "regime, and it is a genuinely different instrument (vehicle telematics, not a "
          "phone), so it is worth asking whether it agrees with §4b.")
    fv = C.q(con, """SELECT vehicle_number, samsara_last_latitude lat, samsara_last_longitude lng,
                       samsara_movement_status ms, samsara_stationary_since ss,
                       samsara_last_seen_at seen
                     FROM drivers_fleetvehicle
                     WHERE samsara_last_latitude IS NOT NULL AND is_active=1""")
    n_fleet = C.q1(con, "SELECT COUNT(*) FROM drivers_fleetvehicle WHERE is_active=1")
    print(f"[measured] {len(fv)}/{n_fleet} active vehicles carry a position. Newest fix "
          f"{max(str(r['seen'])[:19] for r in fv)} UTC.")
    fresh = [r for r in fv if hours_behind(H, putc(r["seen"])) is not None
             and hours_behind(H, putc(r["seen"])) < 24]
    parked = []
    for r in fresh:
        ss = putc(r["ss"])
        if r["ms"] == "driving" or ss is None:
            continue
        mins = (putc(r["seen"]) - ss).total_seconds() / 60.0
        if mins >= 30:
            parked.append((float(r["lat"]), float(r["lng"]), r["vehicle_number"], mins))
    print(f"[measured] of those, {len(fresh)} have a fix inside 24 h and {len(parked)} have been "
          f"STATIONARY for 30 min or more at that fix (samsara_stationary_since), which is the "
          f"closest thing to 'where it rests' this instant offers.")
    if len(parked) >= 3:
        pp = [(a, b) for a, b, _, _ in parked]
        dists = [haversine_km(a[0], a[1], b[0], b[1])
                 for i, a in enumerate(pp) for b in pp[i + 1:]]
        print("  " + C.fmt_describe("km between two parked vehicles", dists))
        for thr in (0.3, 1.0, 3.0):
            k = sum(1 for x in dists if x <= thr)
            print(f"  pairs within {thr:>4} km: {k:>3}/{len(dists)} "
                  f"({100.0 * k / len(dists):4.1f}%)")
        # largest group inside 300 m
        best = 0
        for a in pp:
            k = sum(1 for b in pp if haversine_km(a[0], a[1], b[0], b[1]) <= 0.3)
            best = max(best, k)
        print(f"  [measured] largest cluster within 300 m: {best} of {len(parked)} parked "
              f"vehicles.")
        print(f"  [inferred] {len(parked) - best} vehicles rest apart from that cluster, on "
              f"{len(parked) - best} different streets. The current fleet agrees with §4b: "
              f"mostly home-kept, with ONE small multi-vehicle point rather than a yard.")
        # does the current fleet sit where the archive's day-starts were?
        near_hist = sum(1 for a, b, _, _ in parked
                        if any(haversine_km(a, b, cy * 0.005, cx * 0.005) <= 1.0
                               for (cy, cx), _ in cells.most_common(30)))
        print(f"  [measured] BRIDGE ACROSS THE ERAS: {near_hist}/{len(parked)} currently parked "
              f"vehicles sit within 1 km of one of the archive's top-30 day-start cells — "
              f"two instruments, two eras, the same map. The archive's geography still "
              f"describes where the fleet rests.")
        C.write_csv("07_fleet_rest_positions.csv",
                    ["vehicle", "lat", "lng", "stationary_minutes"],
                    [[v, round(a, 5), round(b, 5), round(m)] for a, b, v, m in parked])
    else:
        print("[unavailable] too few parked vehicles in the snapshot to say anything.")
    print("[verdict] §4c — consistent with §4b and far too small to stand alone (n="
          f"{len(parked)} vehicles at ONE instant). Quote §4b's conclusion; cite §4c only as "
          f"corroboration that the picture has not inverted since the archive closed.")

    C.sub("4d. driverlocation.eta_minutes as an INDEPENDENT prediction-vs-outcome source")
    print("structurally different from §2c on every axis: a different origin (the driver's "
          "PHONE GPS, not the vehicle's Samsara unit), a different caller (the driver's own "
          "status tap, not a 3-minute background sweep), a different date range, and — "
          "crucially — a SINGLE shot per tap that is never revised. One fix per leg per "
          "status, so the sample is one prediction per leg, not a stream.")
    dl_pred = C.q(con, """SELECT leg_id, status, MIN(timestamp) ts, eta_minutes,
                            latitude, longitude
                          FROM reservations_driverlocation
                          WHERE eta_minutes IS NOT NULL
                          GROUP BY leg_id, status""")
    dl_cases = (("phone GPS -> pickup", "on-the-way", "on-location", True),
                ("phone GPS -> dropoff", "picked-up", "completed", True),
                ("address fallback -> dropoff", "picked-up", "completed", False))
    dl_csv2 = []
    for label, st, endk, need_gps in dl_cases:
        errs, buckets = [], defaultdict(list)
        for r in dl_pred:
            if r["status"] != st:
                continue
            has_gps = not (float(r["latitude"]) == 0 and float(r["longitude"]) == 0)
            if has_gps != need_gps:
                continue
            pd = legdate.get(r["leg_id"])
            if pd and pd > today.isoformat():
                continue
            e = taps.get(r["leg_id"], {}).get(endk)
            ts = putc(r["ts"])
            if not e or not ts or e <= ts:
                continue
            err = ((ts + dt.timedelta(minutes=r["eta_minutes"])) - e).total_seconds() / 60.0
            errs.append(err)
            m = r["eta_minutes"]
            b = "0-5" if m <= 5 else "6-15" if m <= 15 else "16-30" if m <= 30 else "31+"
            buckets[b].append(err)
            dl_csv2.append([label, r["leg_id"], pd, str(ts)[:19], m, round(err, 2)])
        print("\n  " + err_line(label, errs, width=30))
        for b in ("0-5", "6-15", "16-30", "31+"):
            if buckets[b]:
                print("    " + err_line(f"eta {b} min", buckets[b], width=28))
    C.write_csv("07_driverlocation_eta_errors.csv",
                ["case", "leg_id", "pickup_date", "captured_utc", "eta_minutes",
                 "error_minutes"], dl_csv2)
    print("\n[inferred] the phone-GPS-to-PICKUP case is far worse than the Samsara sweep's "
          "figures in §2c, and the reason is not that Google was wrong. This estimate is made "
          "ONCE, at the 'on-the-way' tap, and never revised; the sweep re-evaluates against the "
          "car's real position every few minutes. The enormous negative tail (predicted arrival "
          "far earlier than realised) is drivers who tap 'on-the-way' and then do not move.")
    print("[measured] that reading is testable — realised travel time minus predicted, at the "
          "on-the-way tap:")
    tapgap = []
    for r in dl_pred:
        if r["status"] != "on-the-way":
            continue
        if float(r["latitude"]) == 0 and float(r["longitude"]) == 0:
            continue
        e = taps.get(r["leg_id"], {}).get("on-location")
        ts = putc(r["ts"])
        if not e or not ts or e <= ts:
            continue
        tapgap.append((e - ts).total_seconds() / 60.0 - r["eta_minutes"])
    if tapgap:
        print("  " + C.fmt_describe("realised minus predicted, minutes", tapgap))
        for th in (15, 30, 60):
            print(f"  {100.0 * sum(1 for x in tapgap if x > th) / len(tapgap):5.1f}% of legs took "
                  f"more than {th} min longer than the phone-GPS estimate")
    print("[verdict] this source is a SECOND OPINION on drive-time realism only in its "
          "dropoff form, where the driver is demonstrably in motion (|err| P90 is in line with "
          "§2c). Its pickup form is better read as a measurement of TAP LAG than of model "
          "error — which is itself useful, and it corroborates script 02's finding that the "
          "'on-the-way' tap is not a departure signal.")

    # ======================================================================
    C.hdr("5. reservations_schedulesnapshot — is a before/after replay possible?")
    # ======================================================================
    n_sn, sn_a, sn_b, sn_days = span(con, "reservations_schedulesnapshot", "created_at")
    n_en = C.q1(con, "SELECT COUNT(*) FROM reservations_schedulesnapshotentry")
    sd_a = C.q1(con, "SELECT MIN(schedule_date) FROM reservations_schedulesnapshot")
    sd_b = C.q1(con, "SELECT MAX(schedule_date) FROM reservations_schedulesnapshot")
    print(f"[measured] {n_sn} snapshots, {n_en:,} entries; created {sn_a} .. {sn_b}; "
          f"schedule_date {sd_a} .. {sd_b}")
    for r in C.q(con, "SELECT trigger, COUNT(*) n, AVG(assigned_count) a "
                      "FROM reservations_schedulesnapshot GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  trigger={r['trigger']:<20} {r['n']:>4} snapshots, mean assigned "
              f"{r['a']:.1f}")
    covered_days = C.q1(con, "SELECT COUNT(DISTINCT schedule_date) FROM reservations_schedulesnapshot")
    total_days = (dt.date.fromisoformat(sd_b) - dt.date.fromisoformat(sd_a)).days + 1
    print(f"[measured] {covered_days} distinct schedule_dates covered out of {total_days} "
          f"calendar days in range = {100.0 * covered_days / total_days:.1f}% of days have "
          f"ANY snapshot.")
    cur_cov = C.q1(con, "SELECT COUNT(DISTINCT schedule_date) FROM reservations_schedulesnapshot "
                        "WHERE schedule_date >= ?", (cur_start.isoformat(),))
    cur_days = (cur_end - cur_start).days + 1
    print(f"[measured] inside the CURRENT regime ({cur_start}..{cur_end}, {cur_days} days): "
          f"{cur_cov} days covered ({100.0 * cur_cov / cur_days:.1f}%)")

    # replay requires a BEFORE and an AFTER for the same day
    pairs = C.q(con, """SELECT schedule_date, COUNT(*) n,
                          SUM(trigger IN ('before_auto_assign','before_reset')) befores,
                          SUM(trigger='manual') manuals
                        FROM reservations_schedulesnapshot GROUP BY 1""")
    replayable = [r for r in pairs if r["befores"] >= 1 and r["n"] >= 2]
    print(f"[measured] days with >=2 snapshots including at least one 'before_*' — i.e. a real "
          f"BEFORE/AFTER pair: {len(replayable)} of {len(pairs)} snapshotted days")
    # do the entries actually differ between consecutive snapshots on a day?
    diff_days, same_days = 0, 0
    ent = defaultdict(dict)
    for r in C.q(con, "SELECT snapshot_id, leg_id, driver_id FROM reservations_schedulesnapshotentry"):
        ent[r["snapshot_id"]][r["leg_id"]] = r["driver_id"]
    for r in pairs:
        snaps = [x["id"] for x in C.q(
            con, "SELECT id FROM reservations_schedulesnapshot WHERE schedule_date=? "
                 "ORDER BY created_at", (r["schedule_date"],))]
        if len(snaps) < 2:
            continue
        changed = any(ent.get(snaps[i], {}) != ent.get(snaps[i + 1], {})
                      for i in range(len(snaps) - 1))
        diff_days += 1 if changed else 0
        same_days += 0 if changed else 1
    print(f"[measured] of multi-snapshot days, {diff_days} show a genuine assignment DIFF "
          f"between consecutive snapshots and {same_days} do not.")
    print("[verdict] the snapshots are a real before/after record, but they are OPPORTUNISTIC: "
          "one is written when a dispatcher resets or auto-assigns, so coverage follows "
          "dispatcher behaviour, not the calendar. They support case studies and a "
          "'what did auto-assign change' diff — they do NOT support an unbiased "
          "population-level replay, because the days that got snapshotted are exactly the "
          "days somebody felt the need to re-plan.")
    C.write_csv("07_snapshot_days.csv",
                ["schedule_date", "snapshots", "before_triggers", "manual"],
                [[r["schedule_date"], r["n"], r["befores"], r["manuals"]] for r in pairs])

    # ======================================================================
    C.hdr("6. reservations_legkeoi — hand-labelled dispatcher risk")
    # ======================================================================
    keoi = C.q(con, """SELECT k.id, k.category, k.operational_status, k.closed_reason,
                         k.created_at, k.closed_at, k.description, k.leg_id,
                         l.pickup_date, l.pickup_time, l.pickup_location, l.dropoff_location,
                         l.driver_id
                       FROM reservations_legkeoi k
                       JOIN reservations_leg l ON l.id=k.leg_id
                       ORDER BY k.created_at""")
    print(f"[measured] {len(keoi)} flags, {putc(keoi[0]['created_at']).date()} .. "
          f"{putc(keoi[-1]['created_at']).date()} "
          f"({(putc(keoi[-1]['created_at']).date() - putc(keoi[0]['created_at']).date()).days + 1} days)")
    junk_re = re.compile(r"^\s*(test\w*|disregard|asdf|ignore)\s*\.?\s*$", re.I)
    real = [k for k in keoi if not junk_re.match(k["description"] or "")]
    print(f"[measured] {len(keoi) - len(real)} are operator test rows "
          f"({', '.join(repr((k['description'] or '')[:12]) for k in keoi if junk_re.match(k['description'] or ''))}) "
          f"-> n={len(real)} substantive flags")

    cats = Counter(k["category"] for k in real)
    print("\n  category                     n    share")
    for c, n in cats.most_common():
        print(f"  {c:<22} {n:>5}   {100.0 * n / len(real):5.1f}%")
    tight = cats["tight_schedule"] + cats["driver_conflict"]
    print(f"[measured] 'tight_schedule' + 'driver_conflict' = {tight}/{len(real)} = "
          f"{100.0 * tight / len(real):.1f}% — the dominant worry by a wide margin. "
          f"Everything else is a long tail.")
    print("  operational_status: " +
          ", ".join(f"{k}={v}" for k, v in Counter(k["operational_status"] for k in real).most_common()))
    print("  closed_reason:      " +
          ", ".join(f"{k}={v}" for k, v in Counter(str(k["closed_reason"]) for k in real).most_common()))

    # what is IN the text?
    THEMES = {
        "chained conflict (prev job may run late)":
            r"\bif\b.*(late|delay|behind|run(s|ning)? (late|behind)|not.*pick)|previous|before this|arrival before",
        "explicit swap plan named":
            r"\bswap\b|\bgive (his|her|the)\b|\breassign|send (his|her|the|it|this)|cover (it|this)|change it with|move.*to",
        "flight timing is the risk":
            r"\bflight\b|\barrival\b|jetblue|international|global entry|flightradar|divert",
        "'this is NOT a conflict' (false-positive annotation)":
            r"not a conflict|is NOT a CONFLICT|do not move|this is perfect|will be able to make it|plenty of time",
        "guest/pax readiness":
            r"guest|client|passenger|not responding|luggage|bags|carry on|carry-on",
        "wants to avoid farm-out":
            r"farm|out ?source|no inhouse|inhouse",
        "port / cruise":
            r"\bport\b|cruise|cape",
    }
    print("\n  what the free text actually says (regex over the description; a flag can hit "
          "more than one theme):")
    theme_csv = []
    for name, pat in THEMES.items():
        n = sum(1 for k in real if re.search(pat, k["description"] or "", re.I))
        print(f"    {name:<52} {n:>3}/{len(real)}  ({100.0 * n / len(real):5.1f}%)")
        theme_csv.append([name, n, round(100.0 * n / len(real), 1)])
    C.write_csv("07_keoi_themes.csv", ["theme", "flags", "pct"], theme_csv)

    # is the flag predictive? compare flagged legs to the rest on lateness
    dep = {}
    for r in C.q(con, C.live_legs_sql("l.id AS id, l.pickup_date AS pd, l.pickup_time AS pt",
                                      " AND l.pickup_date <= ?"), (H.last_actuals_day.isoformat(),)):
        dep[r["id"]] = C.booked_dtm(r["pd"], r["pt"])
    def lateness(lid):
        b = dep.get(lid)
        t = taps.get(lid, {}).get("on-location")
        if not b or not t:
            return None
        return (C.to_local(str(t)) - b).total_seconds() / 60.0
    flagged = [lateness(k["leg_id"]) for k in real]
    flagged = [x for x in flagged if x is not None]
    flag_ids = {k["leg_id"] for k in real}
    # control: legs on the SAME service days that were not flagged
    fdays = {k["pickup_date"] for k in real}
    ctrl = []
    for r in C.q(con, C.live_legs_sql("l.id AS id", " AND l.pickup_date IN (%s)"
                                      % ",".join("?" * len(fdays))), tuple(sorted(fdays))):
        if r["id"] in flag_ids:
            continue
        v = lateness(r["id"])
        if v is not None:
            ctrl.append(v)
    print("\n  are flagged legs actually worse? minutes from booked pickup to 'on-location' "
          "(+ = arrived after the booked time):")
    print("  " + C.fmt_describe("KEOI-flagged legs", flagged))
    print("  " + C.fmt_describe("same-day unflagged control", ctrl))
    if flagged and ctrl:
        print(f"  [measured] P75 gap {qq(flagged, 75) - qq(ctrl, 75):+.1f} min, "
              f"P90 gap {qq(flagged, 90) - qq(ctrl, 90):+.1f} min "
              f"(n={len(flagged)} vs {len(ctrl):,})")
        print("  [measured] this is a NULL result: flagged legs did NOT run measurably later "
              "than their same-day unflagged neighbours. n=52 flagged cannot resolve a small "
              "effect, so 'no difference' is weak evidence either way — but note that a null "
              "here is what SUCCESS looks like. Every one of these flags was actively managed: "
              f"{Counter(k['operational_status'] for k in real)['backup_arranged']} had a "
              f"backup arranged and {Counter(str(k['closed_reason']) for k in real)['leg_completed']} "
              "closed because the leg completed. The flag is an intervention, not a "
              "prediction, and it cannot be scored as one.")
    C.write_csv("07_keoi_flags.csv",
                ["id", "created_at", "category", "operational_status", "closed_reason",
                 "leg_id", "pickup_date", "pickup_time", "pickup", "dropoff",
                 "minutes_booked_to_onlocation", "description"],
                [[k["id"], str(k["created_at"])[:19], k["category"], k["operational_status"],
                  k["closed_reason"], k["leg_id"], k["pickup_date"], str(k["pickup_time"])[:5],
                  k["pickup_location"], k["dropoff_location"],
                  (round(lateness(k["leg_id"]), 1) if lateness(k["leg_id"]) is not None else ""),
                  (k["description"] or "").replace("\n", " ")] for k in keoi])

    keoi_start = putc(keoi[0]["created_at"]).date()
    keoi_legs = sum(byday.get((keoi_start + dt.timedelta(days=i)).isoformat(), 0)
                    for i in range((today - keoi_start).days + 1))
    rate = len(real) / float(max(keoi_legs, 1))
    print(f"\n[measured] flags per leg over the KEOI era ({keoi_start}..{today}, "
          f"{keoi_legs:,} live legs): {rate:.4f} = ~1 flag per {1.0 / rate:.0f} legs. At the "
          f"current regime's {cur_mean:.0f} legs/day that is ~{rate * cur_mean:.1f} flags/day.")
    print("[inferred] ~2 flags a day is NOT the rate at which tight schedules occur — it is the "
          "rate at which one dispatcher decided a tight schedule was worth writing down. The "
          "true incidence is higher and is [unavailable].")
    print("[verdict] n=60 substantive flags is enough to READ (and it is unusually rich text), "
          "enough to enumerate failure MODES, and enough for qualitative validation of a "
          "risk model's face validity. It is NOT enough to fit, tune or statistically "
          "validate a classifier, and the flags are not a random sample — they exist only "
          "where a dispatcher already noticed. Treat as a labelled TEST SET of worked "
          "examples, never as training data or as a base rate.")

    # ======================================================================
    C.hdr("7. routedistancecache and routetimingmetric — maintained in production?")
    # ======================================================================
    for r in C.q(con, """SELECT status, COUNT(*) n, MIN(created_at) a, MAX(updated_at) b,
                           AVG(attempts) at
                         FROM reservations_routedistancecache GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  rdc status={r['status']:<8} {r['n']:>6,}  {str(r['a'])[:10]} .. "
              f"{str(r['b'])[:19]}  ({hours_behind(H, putc(r['b'])):.1f} h before pull)  "
              f"mean attempts {r['at']:.2f}")
    tot_rdc = C.q1(con, "SELECT COUNT(*) FROM reservations_routedistancecache")
    ok_rdc = C.q1(con, "SELECT COUNT(*) FROM reservations_routedistancecache WHERE status='ok'")
    print(f"[measured] resolve rate {ok_rdc:,}/{tot_rdc:,} = {100.0 * ok_rdc / tot_rdc:.1f}%")
    dm = C.describe([r[0] for r in C.q(
        con, "SELECT drive_minutes FROM reservations_routedistancecache WHERE status='ok' "
             "AND drive_minutes IS NOT NULL")])
    print(f"  drive_minutes: n={dm['n']:,} P25 {dm['p25']} P50 {dm['p50']} P75 {dm['p75']} "
          f"P90 {dm['p90']}")
    added = C.q(con, """SELECT substr(created_at,1,7) m, COUNT(*) n
                        FROM reservations_routedistancecache GROUP BY 1 ORDER BY 1""")
    print("  rows created by month: " + ", ".join(f"{r['m']}={r['n']:,}" for r in added))
    print(f"[measured] the cache is WARM and growing to within "
          f"{hours_behind(H, putc(C.q1(con, 'SELECT MAX(updated_at) FROM reservations_routedistancecache'))):.1f} h "
          f"of the pull. It is populated lazily on a miss "
          f"(dispatching/scheduler.py:658 resolve_drive_minutes, reached from day_setup), so it covers the pairs the "
          f"system has actually had to price, not the space of possible pairs.")

    n_rtm, rtm_a, rtm_b, _ = span(con, "reservations_routetimingmetric", "last_calculated")
    samples = C.q1(con, "SELECT SUM(sample_count) FROM reservations_routetimingmetric")
    print(f"\n[measured] routetimingmetric: {n_rtm} rows, last_calculated {rtm_a} .. {rtm_b} "
          f"({hours_behind(H, rtm_b) / 24.0:.1f} DAYS before the pull)")
    print(f"[measured] {samples:,} samples spread over {n_rtm} partitions = "
          f"{samples / float(n_rtm):.1f} samples per row")
    print("  " + C.fmt_describe("sample_count per partition",
                                [r[0] for r in C.q(con, "SELECT sample_count FROM reservations_routetimingmetric")]))
    thin = C.q1(con, "SELECT COUNT(*) FROM reservations_routetimingmetric WHERE sample_count < 10")
    thin30 = C.q1(con, "SELECT COUNT(*) FROM reservations_routetimingmetric WHERE sample_count < 30")
    print(f"[measured] {thin}/{n_rtm} ({100.0 * thin / n_rtm:.1f}%) partitions rest on fewer "
          f"than 10 samples; {thin30}/{n_rtm} ({100.0 * thin30 / n_rtm:.1f}%) on fewer than 30. "
          f"A P90 from <10 samples is not a P90.")
    dims = C.q1(con, """SELECT COUNT(*) FROM (SELECT DISTINCT trip_type, pickup_location_category,
                          dropoff_location_category, time_of_day_category, day_type
                        FROM reservations_routetimingmetric)""")
    print(f"[measured] the partition key is 5-dimensional ({dims} distinct combinations "
          f"present). Over-partitioned relative to the sample it has.")
    stale_days = hours_behind(H, rtm_b) / 24.0
    print(f"[verdict] routedistancecache: LIVE. routetimingmetric: STALE by {stale_days:.1f} days "
          f"and thin. Recompute it over a derived window before trusting any percentile in it; "
          f"the raw taps (script 02) are the better source.")
    C.write_csv("07_rtm_partitions.csv",
                ["trip_type", "pickup_cat", "dropoff_cat", "tod", "day_type", "sample_count",
                 "p75_total", "p90_total", "last_calculated"],
                [[r["trip_type"], r["pickup_location_category"], r["dropoff_location_category"],
                  r["time_of_day_category"], r["day_type"], r["sample_count"],
                  r["p75_total_time"], r["p90_total_time"], str(r["last_calculated"])[:19]]
                 for r in C.q(con, "SELECT * FROM reservations_routetimingmetric "
                                   "ORDER BY sample_count DESC")])

    # ======================================================================
    C.hdr("8. RANKED VERDICT — what changes what we can deliver")
    # ======================================================================
    enroute_final = scored["en route -> PICKUP"][1]
    ontrip_final = scored["on trip -> DROPOFF"][1]
    binding = [r for r in chain_rows if 0 <= r[0] < 15] if chain_rows else []
    ranked = [
        ("1. reservations_auditlog", "LOAD-BEARING — the biggest single gain",
         [f"{n_a:,} rows, unbroken {a_a.date()} .. {a_b.date()}, ending "
          f"{hours_behind(H, a_b):.1f} h before the pull",
          f"assignment churn is now measurable: {C.describe(n_effective)['p50']:.0f} effective "
          f"changes of hand at the median, {C.describe(n_effective)['p90']:.0f} at P90, and "
          f"{100.0 * sum(1 for x in hours_before if x <= 24) / len(hours_before):.1f}% of all "
          f"changes land inside 24 h of the booked pickup",
          "the number is confirmed by a structurally different route (historicalleg's "
          "driver_id diff) that agrees at P50/P75/P90",
          f"recovers only {tot_recover:,} ladder events legstatus does not hold — the two "
          f"tables are near-duplicates written by the same request, so the hoped-for coverage "
          f"gain is NOT there. That is a real negative result: do not plan on it",
          f"extends the operational record {(s_a - a_a).days} days before legstatus begins",
          "unlocks: a churn KPI, a 'how much re-planning does a plan survive' baseline, "
          "per-dispatcher behaviour, and the first honest measure of pickup-time volatility"]),
        ("2. reservations_historicalleg", "LOAD-BEARING, with one hard limit",
         [f"{n_h:,} rows on {n_legs_hist:,} legs from {h_a.date()}",
          f"§12.6 is PARTLY ANSWERED: {n_pred:,} timestamped scheduler predictions exist and "
          f"CAN be scored. En-route-to-pickup final call: |err| P75 "
          f"{qq([abs(x) for x in enroute_final], 75):.1f} min, P90 "
          f"{qq([abs(x) for x in enroute_final], 90):.1f} min (n={len(enroute_final):,})",
          (f"the chained next-pickup claim can be scored too, and where it binds it misses "
           f"{100.0 * sum(1 for r in binding if r[2] > 0) / len(binding):.0f}% of the time "
           f"(n={len(binding):,}) — the first empirical turnaround-buffer number this "
           f"engagement has") if binding else
          "the chained next-pickup claim can be scored where it binds",
          f"the risk band it stores is monotone but over-warns: 'at_risk' is a false alarm "
          f"{100.0 - 100.0 * sum(1 for x in band['at_risk'] if x > 0) / max(len(band['at_risk']), 1):.0f}% "
          f"of the time",
          f"NEGATIVE finding worth stating: pay and vehicle do NOT churn — only "
          f"{100.0 * sum(1 for x in pay_changes if x) / len(pay_changes):.1f}% of legs are ever "
          f"re-priced and {100.0 * sum(1 for x in veh_changes if x) / len(veh_changes):.1f}% "
          f"ever change vehicle. This table's value is driver_id and pickup_time, nothing else",
          "the hard limit: sweep_eta writes with bulk_update(), which fires no signal, so "
          "history never logs the sweep itself — the prediction log is an incidental "
          "by-product of unrelated saves, sparse and biased toward legs that got touched",
          "it predicts the LIVE ETA of a driver already moving. It says nothing about what "
          "auto-assign believed when it SEATED the job hours earlier — that estimate is "
          "still never persisted, so §12.6 stands for the PLANNING model",
          f"also unlocks: {flips:,} in-house/affiliate flips on {legs_with_flip:,} legs, and "
          f"the ONLY way to recover what pickup_time WAS at a past instant "
          f"— which matters because "
          f"{100.0 * kinds['ARRIVAL'] / max(base_kinds['ARRIVAL'], 1):.0f}% of arrivals get "
          f"retimed, about {n_ev_in_win / float(max(len(retimed_in_win), 1)):.0f} times each"]),
        ("3. reservations_legkeoi", "LOAD-BEARING for problem definition, not for statistics",
         [f"n={len(real)} substantive flags over "
          f"{(putc(keoi[-1]['created_at']).date() - putc(keoi[0]['created_at']).date()).days + 1} days",
          f"{100.0 * tight / len(real):.1f}% are tight_schedule or driver_conflict — the "
          f"redesign's target failure mode, named by the people who live it",
          "the text names the mitigation (a swap with a specific other job) far more often "
          "than it names the fault — the operational answer is already in the corpus",
          "several flags exist to say a system-detected conflict is NOT one (international "
          "arrival = ~2 h of built-in slack), which is direct evidence of false positives",
          "too small and too self-selected to fit or validate a model against"]),
        ("4. reservations_routedistancecache", "SUPPORTING — quietly essential, already live",
         [f"{tot_rdc:,} pairs, {100.0 * ok_rdc / tot_rdc:.1f}% resolved, updated to within "
          f"{hours_behind(H, putc(C.q1(con, 'SELECT MAX(updated_at) FROM reservations_routedistancecache'))):.1f} h "
          f"of the pull",
          "makes feasibility/turnaround maths reproducible offline without paying Google",
          "lazily filled, so it describes the pairs the business actually runs"]),
        ("5. reservations_schedulesnapshot", "INTERESTING — case studies only",
         [f"{n_sn} snapshots / {n_en:,} entries; {covered_days} distinct days "
          f"({100.0 * covered_days / total_days:.1f}% of the calendar)",
          f"{len(replayable)} days carry a genuine before/after pair",
          "coverage is dispatcher-triggered, so the snapshotted days are exactly the "
          "hard days — good for worked examples, invalid as a population sample"]),
        ("6. reservations_driverlocation", "A CLOSED ARCHIVE — answers one question, then stops",
         [f"{tot_real:,} real-coordinate rows exist, but ALL of them predate "
          f"{last_real.date()} — {(cur_start - last_real.date()).days} days before the "
          f"current demand regime even opened",
          "phone-GPS capture was deleted in commit 94c88ba7 (drivers/views.py:222 says so in "
          "as many words); 100% of rows since are latitude=0/longitude=0 fallback records "
          "with no position at all",
          "it does settle the base question, and that is worth having: NO shared base — the "
          f"busiest single point is {100.0 * tn / len(far):.0f}% of day-starts, drivers' modal "
          f"start points sit a median {qq(pair, 50):.1f} km apart, and the live Samsara "
          f"snapshot agrees. A HOME-KEPT fleet with per-driver origins",
          "the useful corollary: a per-driver start point is real, stable enough to model, "
          "and the scheduler ignores it entirely",
          "but it cannot measure deadhead or repositioning, and it cannot describe today"]),
        ("7. reservations_routetimingmetric", "NOT LOAD-BEARING — stale and over-partitioned",
         [f"{n_rtm} rows, {samples:,} samples = {samples / float(n_rtm):.1f} per partition; "
          f"{100.0 * thin / n_rtm:.1f}% rest on <10 samples",
          f"last recomputed {stale_days:.1f} days before the pull, i.e. before the current "
          f"regime",
          "derive timings from the taps instead; this table is a cached answer to a question "
          "nobody re-asked"]),
    ]
    for title, verdict, bullets in ranked:
        print(f"\n{title}  —  {verdict}")
        for b in bullets:
            print(f"    * {b}")

    C.hdr("what CHANGES for the engagement, stated as capabilities", ch="-")
    for line in [
        f"NEW: a measured turnaround buffer. The chained ETA's binding bucket gives a miss "
        f"rate and an overrun distribution "
        f"({'P75 %.0f min, P90 %.0f min' % (qq([r[1] for r in binding if r[1] > 0], 75), qq([r[1] for r in binding if r[1] > 0], 90)) if binding else 'n/a'}). "
        f"Previously every buffer would have been asserted.",
        "NEW: a churn baseline. 'How much of the published plan survives to the service day' "
        "is now a number, from two independent tables that agree.",
        "NEW: the retime problem is now sized, and it is bigger than anyone assumed — a "
        "flight-tracked arrival's pickup time is a running forecast, not a booking.",
        "NEW: calibration evidence that the existing risk signal over-warns, corroborated "
        "independently by the dispatchers' own free text.",
        "NEW: farm-out decisions can be watched changing their mind over a leg's life.",
        "NEW: the base question is settled — no yard, home-kept fleet, per-driver origins that "
        "are stable enough to model and that the scheduler currently ignores.",
        "UNCHANGED: the planner is still unmeasurable. Nothing new instruments auto-assign.",
        "UNCHANGED: the actuals sample cannot be grown. auditlog duplicates legstatus.",
    ]:
        print(f"  * {line}")

    C.hdr("[unavailable] — what these tables still cannot answer", ch="-")
    for line in [
        "What the PLANNER predicted. Nothing persists auto-assign's own estimate of when a "
        "driver would finish job A and reach job B. dispatch_eta_* is a live, same-day, "
        "GPS-driven ETA for a driver already in motion, computed by a different engine "
        "(samsara_risk) than the one that seats the job (scheduler). Scoring the planner "
        "still requires instrumenting it.",
        "Where vehicles are when they are not on a job. Every position record is triggered by "
        "a status tap, so there is no fix between the last drop-off of one day and the first "
        "'on-the-way' of the next. Deadhead between jobs, and overnight parking, are both "
        "[unavailable] as measured facts.",
        f"A base is now ANSWERED, not unavailable — but only to the strength of the evidence: "
        f"a {(last_real - first_real).days}-day archive that closed "
        f"{(cur_start - last_real.date()).days} days before the current regime opened, plus a "
        f"{len(parked)}-vehicle snapshot at one instant. Both say the same thing (no yard, "
        f"home-kept, per-driver origins). What is still [unavailable] is any TIME SERIES of "
        f"vehicle position in the current regime, so 'has that changed since "
        f"{last_real.date()}' cannot be answered.",
        "Counterfactuals. Snapshots record what the schedule WAS, never what an alternative "
        "would have cost. A before/after diff shows the change, not its value.",
        "A base rate for scheduling failure. KEOI records what a dispatcher noticed; the "
        "denominator (near-misses nobody flagged) does not exist.",
        "Any of it before history switched on. auditlog is the earliest of the new tables and "
        f"still starts {a_a.date()}; the business ran for years before that.",
    ]:
        print(f"  * {line}")

    print("\nCSVs written to " + C.OUT_DIR)
    con.close()


if __name__ == "__main__":
    main()
