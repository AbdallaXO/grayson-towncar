"""
Keep what the Samsara sweep already knows.

The sweep computes, every 180 seconds, where each in-house car is relative to
where it has to be next — then overwrites the answer on the next tick. This
module turns that into a series (``DispatchEtaSample``), and reads the series
back in the shape ``analysis/07_new_evidence.py`` scores.

POSTURE. A ledger, like ``advisor_events``: nothing here is read in a request
path, the writer never raises into the sweep, and it makes no Samsara, Google or
AeroAPI call of its own — every value it stores was already computed and paid
for by the tick it rides.

WHAT IS WRITTEN, AND WHY NOT EVERYTHING. Measured before it was chosen —
``analysis/28_eta_history_gate.py``, 28 real days replayed at the real cadence
with the sweep's own target selection (``out/28_write_rules.csv``,
``out/28_notap_coverage.csv``):

    rule                    rows/day   MiB/yr   scorable kept   ambiguous legs
    everything                 6,868      557          100.0%     21/25  84.0%
    under way only             2,509      204           95.9%      8/25  32.0%
    within 60 min only         1,650      134           38.6%     20/25  80.0%
    UNDER WAY OR WITHIN 60 M   3,429      278           97.0%     21/25  84.0%

"Scorable" is 07's own window — a sample between the two taps it would be graded
against. Only 1,762 of the 6,868 rows a literal per-tick insert writes are
scorable at all; the rest are a parked car hours from its next job.

The last column is why the cheapest rule lost. "Under way" halves the volume and
keeps 96% of what 07 can score — and then loses two thirds of the case §3.4
actually wants GPS for. An ambiguous leg is one where the milestone passed with
no pickup tap, and GPS's job is to say whether the car ever left; a leg with no
tap is, by construction, not "under way" by status. The union rule costs 900
more rows a day and loses neither.

Stated honestly: even keeping every tick only covers 21 of those 25 legs — four
are never the sweep's target at all, because the driver's car is not mapped to
Samsara or another leg held the badge. n=25 over 28 days is thin, but the
mechanism is structural rather than statistical, and the direction is 32% against 84%.

GROWTH, so nobody is surprised later: ~1.25 M rows and ~278 MiB a year at
today's fleet, which makes this the largest table in the database inside a year.
Row volume tracks DRIVERS, not trips (about 1.2 rows per driver-tick), so it
grows with headcount rather than with bookings. ``RETENTION_DAYS`` is the dial
and ships at 0 — keep everything — because deleting samples deletes the evidence
behind a published number, and that is a decision to take deliberately.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Config (module constants, house style; 0/False disables) ────────────────

#: The write rule, in words: keep a sample when the driver has actually started
#: (on-the-way / on-location / picked-up) OR when the moment being measured is
#: within NEAR_TARGET_MIN. See the table above for what each half is worth.
WRITE_RULE = "under_way_or_near"
NEAR_TARGET_MIN = 60
#: Statuses that mean the driver is under way. Mirrored by value rather than
#: imported so this file states its own rule; the test asserts the two agree.
STARTED_STATUSES = ("on-the-way", "on-location", "picked-up")

#: 0 = keep everything (the shipped default). Days of samples to retain if the
#: table is ever capped — see the growth note above.
RETENTION_DAYS = 0

ETA_SAMPLES_ENABLED = True


def _minutes(a, b):
    """(a - b) in minutes, or None if either side is missing."""
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 60.0


def _same_coord(a, b):
    """Same anchor position, across the type boundary.

    ``Leg.dispatch_eta_origin_lat/lng`` are DecimalFields, so a value read back
    from the row is a Decimal while the one the sweep just computed is a float.
    ``28.42 == Decimal("28.42")`` is False — comparing them raw would have made
    ``eta_carried`` permanently False and quietly useless, with nothing to show
    for it. Compared as floats, at a tolerance far below any real GPS motion."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def wanted(status, minutes_to_target):
    """Does this evaluation earn a row? The rule, in one place."""
    if (status or "") in STARTED_STATUSES:
        return True
    return (minutes_to_target is not None
            and minutes_to_target <= NEAR_TARGET_MIN)


def build_sample(leg, fields, now):
    """One DispatchEtaSample from a leg and the ``fields`` dict ``evaluate()``
    just produced — or None when the write rule says this tick is not worth a
    row.

    MUST be called BEFORE ``_apply_eta_fields`` overwrites the leg: the previous
    tick's ETA and origin are still on the row here, and comparing them is the
    only way to know whether this tick's value carries new information.

    Nothing is re-derived from a second source. ``minutes_to_target`` and
    ``slack_minutes`` are the sweep's own arithmetic
    (``samsara_risk.evaluate``: ``slack = minutes_to_target - drive_min``)
    against the same ``now`` the sweep stamps as ``evaluated_at``, so a sample
    and the leg row it produced cannot disagree."""
    from dispatching.models import DispatchEtaSample

    target_time = fields.get("dispatch_eta_target_time")
    eta_minutes = fields.get("dispatch_eta_minutes")
    mt = _minutes(target_time, now)
    if not wanted(getattr(leg, "status", ""), mt):
        return None
    slack = None if (mt is None or eta_minutes is None) else mt - eta_minutes

    o_lat = fields.get("dispatch_eta_origin_lat")
    o_lng = fields.get("dispatch_eta_origin_lng")
    # Same number, same anchor, same target as last time => no new information
    # about the road. Deliberately NOT called "reused": whether a paid call was
    # made is not knowable from the data, and for scoring it does not matter.
    carried = bool(
        eta_minutes is not None
        and eta_minutes == getattr(leg, "dispatch_eta_minutes", None)
        and _same_coord(o_lat, getattr(leg, "dispatch_eta_origin_lat", None))
        and _same_coord(o_lng, getattr(leg, "dispatch_eta_origin_lng", None))
        and (fields.get("dispatch_eta_target") or "")
        == (getattr(leg, "dispatch_eta_target", "") or ""))

    return DispatchEtaSample(
        leg_id_ref=leg.id,
        driver_id_ref=getattr(leg, "driver_id", None),
        sampled_at=now,
        eta_target=(fields.get("dispatch_eta_target") or "")[:12],
        eta_minutes=eta_minutes,
        eta_target_time=target_time,
        risk_status=(fields.get("dispatch_risk_status") or "")[:12],
        minutes_to_target=None if mt is None else round(mt, 2),
        slack_minutes=None if slack is None else round(slack, 2),
        is_moving=fields.get("dispatch_is_moving"),
        stationary_minutes=fields.get("dispatch_stationary_minutes"),
        origin_lat=None if o_lat is None else float(o_lat),
        origin_lng=None if o_lng is None else float(o_lng),
        vehicle_label=(fields.get("dispatch_vehicle_label") or "")[:32],
        eta_carried=carried,
    )


def record_samples(samples):
    """Persist one tick's samples. One INSERT, conflicts ignored (the unique
    constraint makes a retried or double-running loop harmless), and never
    raises — the sweep's ETA badges must not depend on the log."""
    from dispatching.models import DispatchEtaSample

    if not ETA_SAMPLES_ENABLED or not samples:
        return 0
    try:
        DispatchEtaSample.objects.bulk_create(samples, ignore_conflicts=True)
        return len(samples)
    except Exception:
        logger.exception("eta sample write failed (%d rows)", len(samples))
        return 0


def prune(now=None):
    """Drop samples older than RETENTION_DAYS. A no-op at the shipped 0."""
    from datetime import timedelta
    from django.utils import timezone
    from dispatching.models import DispatchEtaSample

    if not RETENTION_DAYS:
        return 0
    now = now or timezone.now()
    try:
        n, _ = DispatchEtaSample.objects.filter(
            sampled_at__lt=now - timedelta(days=RETENTION_DAYS)).delete()
        return n
    except Exception:
        logger.exception("eta sample prune failed")
        return 0


# ══════════════════════════════════════════════════════════════════════════
# READING IT BACK — 07's table, from the sample shape
# ══════════════════════════════════════════════════════════════════════════

#: 07's two cases, verbatim (07_new_evidence.py:365-366): the label, the target
#: kinds it covers, and the tap pair the prediction is scored between.
PREDICTION_CASES = (
    ("en route -> PICKUP", ("pickup", "next_pickup"), "on-the-way", "on-location"),
    ("on trip -> DROPOFF", ("dropoff",), "picked-up", "completed"),
)


def prediction_errors(samples, taps, leg_dates, today):
    """07's ETA-error table, computed from DispatchEtaSample-shaped rows.

    ``samples``   iterable with ``.leg_id_ref``, ``.sampled_at``,
                  ``.eta_minutes``, ``.eta_target``, ``.risk_status``
    ``taps``      {leg_id: {status: first timestamp}}
    ``leg_dates`` {leg_id: pickup_date} — read LIVE from the leg, as 07 does,
                  not stored on the sample; 07 uses it for the forward-date drop
                  and for the CSV column, and 21 legs already disagree with
                  their own history rows.
    ``today``     the horizon date, for that forward-date drop.

    Returns [(case, leg_id, pickup_date, sampled_at, eta_minutes, target,
              risk_status, error_minutes)], error = (sampled_at + eta) - realised;
    NEGATIVE means the system said he would arrive earlier than he did.

    This is the shipped half of Phase 1.3's gate: ``analysis/28 --verify-fill``
    feeds it rows drawn from the old incidental log and requires it to reproduce
    the committed out/07_eta_prediction_errors.csv exactly. If it cannot, the
    live number and the replayed one are not the same measurement."""
    from datetime import timedelta

    by_case = {}
    for label, kinds, k0, k1 in PREDICTION_CASES:
        for k in kinds:
            by_case[k] = (label, k0, k1)

    out = []
    seen = set()
    for s in samples:
        entry = by_case.get(s.eta_target)
        if entry is None or s.eta_minutes is None or s.sampled_at is None:
            continue
        label, k0, k1 = entry
        leg_id = s.leg_id_ref
        pd = leg_dates.get(leg_id)
        if pd is not None and str(pd) > str(today):
            continue                                    # forward-dated
        t = taps.get(leg_id) or {}
        start, end = t.get(k0), t.get(k1)
        if not start or not end or end <= start:
            continue                                    # no usable tap pair
        if not (start <= s.sampled_at < end):
            continue                                    # outside the window
        key = (label, leg_id, s.sampled_at)
        if key in seen:
            continue                     # one row per (leg, evaluation instant)
        seen.add(key)
        err = ((s.sampled_at + timedelta(minutes=s.eta_minutes))
               - end).total_seconds() / 60.0
        out.append((label, leg_id, pd, s.sampled_at, s.eta_minutes,
                    s.eta_target, s.risk_status, round(err, 2)))
    return out
