"""
Recovery Advisor PRESENTATION layer — turns the engine's cards and plans into
something a dispatcher can read at 4 a.m. without knowing scheduler internals.

POSTURE: STRICTLY READ-ONLY AND ADDITIVE. This module never detects, never
ranks, never decides, and never writes. It reads an already-assembled
``BoardState``, an already-detected ``Disruption`` and an already-validated
``CandidatePlan`` and returns a JSON-safe ``display`` dict. The engine's own
fields (``headline``, ``narrative``, ``why``, ``risks``, ``basis``, ``moves``,
``apply``) are left on the card BYTE-FOR-BYTE — they still drive the apply
payload, the task-resolution notes and the "Show the math" expander. Ranking
order is whatever the engine handed us; ``rank`` is passed through untouched.

WHY A SEPARATE MODULE: the engine's strings are correct and auditable, and they
must stay that way (tests pin them; the apply path quotes ``plan.title`` into
task notes). Rewriting them in place would trade auditability for readability.
So we keep both: engine text verbatim under "Show the math", plain language on
the surface. Every builder here is wrapped by ``safe_display`` at the call site
— a display failure degrades the card to the old rendering rather than losing
a dispatcher their recovery options.

THE THREE THINGS THIS DRAWS
  1. ``card["display"]["conflict"]`` — the problem, once, as a timeline: the
     driver's committed run, the drive-back + reset block that follows it, the
     run nobody is free for in red, and a bracket measuring the shortfall. All
     four come from the same numbers the engine already computed (the slot's
     ``chain_clear_dt``, the disruption's ``impact_dt`` and ``details['slack']``)
     — the bracket is *defined* as ``impact_dt − slack``, so the picture can
     never disagree with the engine's own arithmetic.
  2. ``plan["display"]["after"]`` — the board AFTER the move, driver by driver:
     the old driver's lane tagged "conflict cleared", the moved run landing in
     the receiving driver's lane tagged "moved from X", and — crucially — that
     driver's REAL next job on the same lane, so the spare time between them is
     anchored to something a dispatcher can verify. The gap between them is
     measured with ``board_validation.turn_slack_minutes``, the same one formula
     the assignment engine and the board pill use: roomy renders as a green
     dashed region drawn to scale, tight renders as a red sliver with the
     minutes labeled.
  3. ``plan["display"]["math"]`` — the engine's own words, verbatim.

GEOMETRY IS COMPUTED SERVER-SIDE (percentages, not datetimes) because the rail
renders client-side from JSON while the conflict-task page renders server-side
from the same dict. One implementation of the arithmetic, two renderers, no
drift.

NOTHING IS INVENTED. Every time, name, route, minute count and next job on
screen traces to a leg row, a schedule slot or an engine verdict. Where a fact
does not exist (an unassigned leg has no old driver; a farmed leg's receiver has
no in-house schedule), the corresponding element is OMITTED rather than guessed.
"""
from __future__ import annotations

from datetime import date as _date, datetime, time as dt_time, timedelta

# Rounding for a timeline's outer edges: pad the content, then snap outward to
# a clean half-hour so the axis reads 3:30 / 4 AM / 5 AM rather than 3:47.
_AXIS_PAD_MIN = 15
_AXIS_SNAP_MIN = 30
# Below this many minutes a block still gets drawn wide enough to see (a 3-min
# sliver is the POINT of the tight-turn callout — it must not vanish).
_MIN_BLOCK_PCT = 0.6


# ════════════════════════════════════════════════════════════════════════════
# SAFETY WRAPPER
# ════════════════════════════════════════════════════════════════════════════

def safe_display(fn, *args, **kwargs):
    """Run a display builder, swallowing anything it throws.

    The advisor's job is to keep a dispatcher moving. A presentation bug must
    never cost them the card, the plans or the Apply button — the surfaces all
    fall back to the engine's raw text when ``display`` is missing.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
# PLAIN LANGUAGE
# ════════════════════════════════════════════════════════════════════════════

# Airport codes a dispatcher would otherwise have to expand in their head.
# Values are how a person says it out loud, not the legal facility name
# (confirmation_sms._AIRPORT_PATTERNS is the guest-facing long form; this is the
# radio-traffic short form).
_AIRPORT_PLAIN = {
    "MCO": "Orlando Airport",
    "SFB": "Sanford",
    "MLB": "Melbourne",
    "LAL": "Lakeland",
    "TPA": "Tampa Airport",
    "MIA": "Miami Airport",
    "FLL": "Fort Lauderdale Airport",
    "PBI": "West Palm Beach Airport",
    "RSW": "Fort Myers Airport",
    "JAX": "Jacksonville Airport",
    "SRQ": "Sarasota Airport",
    "DAB": "Daytona Beach Airport",
    "PIE": "St. Pete Airport",
}

# Whole-string spellings that show up as location names on real legs.
_PLACE_PLAIN = {
    "ORLANDO INTERNATIONAL AIRPORT": "Orlando Airport",
    "ORLANDO INTERNATIONAL": "Orlando Airport",
    "SANFORD INTERNATIONAL AIRPORT": "Sanford",
    "ORLANDO SANFORD INTERNATIONAL": "Sanford",
    "PORT CANAVERAL": "Port Canaveral",
}


# Chain prefixes and generic property nouns a dispatcher drops when speaking.
# "Disney's Port Orleans Resort" is a booking system's name for it; "Port
# Orleans" is the name on the radio, and the radio name is what has to fit on a
# 32-pixel timeline block.
_PLACE_PREFIXES = ("walt disney world", "disney's", "disney", "universal's",
                   "universal orlando", "universal", "the")
_PLACE_SUFFIXES = ("resort & spa", "resort and spa", "resort", "hotel", "inn",
                   "suites", "lodge", "villas", "motel", "spa")


def _trim_property_name(head):
    """Drop the chain prefix and the generic property noun.

    The prefix comes off only when TWO OR MORE words survive it. "Disney's Port
    Orleans Resort" is a booking system's name for Port Orleans, but "Disney
    Springs" is the name of the place — strip the chain word there and the
    destination becomes "Springs", which is not anywhere.
    """
    words = head.split()
    low = " ".join(words).lower()
    for pre in _PLACE_PREFIXES:
        n = len(pre.split())
        if low.startswith(pre + " ") and len(words) - n >= 2:
            words = words[n:]
            low = " ".join(words).lower()
            break
    for suf in _PLACE_SUFFIXES:
        n = len(suf.split())
        if low.endswith(" " + suf) and len(words) - n >= 1:
            words = words[:-n]
            break
    out = " ".join(words).strip(" -–—&")
    return out if len(out) > 1 else head


def plain_place(text):
    """A location a dispatcher would say out loud.

    Strips the postal tail (everything a Google-completed address appends after
    the venue name), expands airport codes, and trims a property name down to
    the part people actually say. A street address or an unrecognised venue
    passes through as written, minus the address tail — guessing at a name we
    do not know is worse than showing the one on the booking.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    head = raw.split(",")[0].strip()
    if not head:
        head = raw
    key = head.upper()
    if key in _PLACE_PLAIN:
        return _PLACE_PLAIN[key]
    if key in _AIRPORT_PLAIN:
        return _AIRPORT_PLAIN[key]
    # A parenthesised code is the airport, however long the official name is:
    # Google hands back "Orlando International Airport (MCO), Jeff Fuqua Blvd,
    # …", which is the shape most rows on this board actually carry.
    for code, plain in _AIRPORT_PLAIN.items():
        if key.endswith(f"({code})"):
            return plain
    # "MCO Terminal B" — a code that OPENS the label and is followed by airport
    # vocabulary. Matching a bare code anywhere in a short label turned "Cafe
    # Mia" into "Miami Airport" and "Lal Bagh" into "Lakeland": three letters
    # are a common word fragment, so the code has to be load-bearing.
    tokens = key.split()
    if len(tokens) > 1 and tokens[0] in _AIRPORT_PLAIN and any(
            t in ("TERMINAL", "TERMINALS", "AIRPORT", "INTERNATIONAL", "ARRIVALS",
                  "DEPARTURES", "GATE", "CURBSIDE") for t in tokens[1:]):
        return _AIRPORT_PLAIN[tokens[0]]
    trimmed = _trim_property_name(head)
    if trimmed.upper() in _PLACE_PLAIN:
        return _PLACE_PLAIN[trimmed.upper()]
    # A code embedded in a venue name — "Brightline MCO" is a real station, not
    # the airport, so the name stays and only the code is expanded. Matching is
    # CASE-SENSITIVE on purpose: airport codes are written in caps, which is
    # what separates "Brightline MCO" from "Cafe Mia".
    words = trimmed.split()
    if len(words) > 1 and any(w in _AIRPORT_PLAIN for w in words):
        return " ".join(_AIRPORT_PLAIN.get(w, w) for w in words)
    return trimmed


def plain_route(obj):
    """"Port Orleans → Sanford" for a Leg or a ScheduleSlot."""
    pu = plain_place(getattr(obj, "pickup_location", ""))
    do = plain_place(getattr(obj, "dropoff_location", ""))
    if pu and do:
        return f"{pu} → {do}"
    return pu or do or "this run"


def clock(value):
    """"4:00 AM" from a datetime or a time."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, dt_time):
        return value.strftime("%I:%M %p").lstrip("0")
    return ""


def duration(minutes):
    """Compact form for timeline labels and chips, where space is the
    constraint: "3 min", "45 min", "1 h 50 m"."""
    try:
        m = int(round(abs(float(minutes))))
    except (TypeError, ValueError):
        return ""
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    return f"{h} h" if rem == 0 else f"{h} h {rem} m"


def duration_words(minutes):
    """Long form for prose: "13 minutes", "1 hour", "1 hour 50 minutes".

    Sentences on this panel are meant to be read the way a dispatcher says them
    out loud — "George will be 13 minutes late" — so prose spells the unit out
    and leaves the abbreviations to the drawings.
    """
    try:
        m = int(round(abs(float(minutes))))
    except (TypeError, ValueError):
        return ""
    if m < 60:
        return f"{m} minute{'' if m == 1 else 's'}"
    # Past a couple of days, hours stop being a unit anyone reads: a stale card
    # on an old leg said "580 hours 50 minutes ago".
    if m >= 2880:
        days = m // 1440
        return f"{days} day{'' if days == 1 else 's'}"
    h, rem = divmod(m, 60)
    head = f"{h} hour{'' if h == 1 else 's'}"
    return head if rem == 0 else f"{head} {rem} minute{'' if rem == 1 else 's'}"


def day_word(target_date, today):
    """"today" / "tomorrow" / "on Thu Aug 7" — for shift-availability warnings."""
    if not isinstance(target_date, _date) or not isinstance(today, _date):
        return "that day"
    delta = (target_date - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return f"on {target_date.strftime('%a %b %-d')}"


# ════════════════════════════════════════════════════════════════════════════
# GEOMETRY — one axis, percentages out
# ════════════════════════════════════════════════════════════════════════════

def _snap_down(dt, step):
    minute = (dt.minute // step) * step
    return dt.replace(minute=minute, second=0, microsecond=0)


def _snap_up(dt, step):
    base = dt.replace(second=0, microsecond=0)
    rem = base.minute % step
    if rem == 0 and base == dt:
        return base
    return base + timedelta(minutes=(step - rem) % step or step)


class Axis:
    """A time window rendered as 0–100%.

    Every block, marker, bracket and band on one timeline is placed through the
    same Axis, so a picture is internally consistent by construction: if two
    things overlap on screen they overlap in real life.
    """

    def __init__(self, start, end):
        if end <= start:
            end = start + timedelta(minutes=60)
        self.start, self.end = start, end
        self.total = max(1.0, (end - start).total_seconds() / 60.0)

    @classmethod
    def around(cls, moments):
        """Axis covering every supplied datetime, padded and snapped outward."""
        pts = [m for m in moments if isinstance(m, datetime)]
        if not pts:
            return None
        lo, hi = min(pts), max(pts)
        lo = _snap_down(lo - timedelta(minutes=_AXIS_PAD_MIN), _AXIS_SNAP_MIN)
        hi = _snap_up(hi + timedelta(minutes=_AXIS_PAD_MIN), _AXIS_SNAP_MIN)
        return cls(lo, hi)

    def pct(self, dt):
        return round(max(0.0, min(100.0,
                    (dt - self.start).total_seconds() / 60.0 / self.total * 100.0)), 3)

    def span(self, a, b, *, min_width=0.0):
        """(left_pct, width_pct) for the interval [a, b], clamped to the axis."""
        left = self.pct(a)
        right = self.pct(b)
        if right < left:
            left, right = right, left
        return left, round(max(right - left, min_width), 3)

    def ticks(self):
        """Hour marks inside the window (2-hourly once the window is long), plus
        the two edges — the axis a dispatcher reads times off."""
        out = []
        hours = self.total / 60.0
        step = 1 if hours <= 8 else (2 if hours <= 16 else 3)
        t = self.start.replace(minute=0, second=0, microsecond=0)
        if t < self.start:
            t += timedelta(hours=1)
        while t.hour % step:
            t += timedelta(hours=1)
        while t <= self.end:
            if self.start < t < self.end:
                out.append({"label": _hour_label(t), "left_pct": self.pct(t),
                            "hour": True})
            t += timedelta(hours=step)
        edges = [{"label": clock(self.start).replace(":00", ":00"),
                  "left_pct": 0.0, "edge": "start"},
                 {"label": clock(self.end), "left_pct": 100.0, "edge": "end"}]
        return edges[:1] + out + edges[1:]


def _hour_label(dt):
    """"4 AM" — hour ticks lose the ":00" a dispatcher does not need."""
    return dt.strftime("%I %p").lstrip("0")


# ════════════════════════════════════════════════════════════════════════════
# ENGINE READS (read-only; every one of these is already on the board)
# ════════════════════════════════════════════════════════════════════════════

def _tight_min():
    from dispatching import pickup_policy
    return pickup_policy.TURN_TIGHT_SLACK_MIN


def _is_sentinel(value):
    """check_feasibility's ±999 "there is nothing on that side" markers — never
    a real minute count, never drawn or printed as one."""
    try:
        return abs(int(value)) >= 999
    except (TypeError, ValueError):
        return False


def _planning_schedules(board):
    """The PLANNING-clock schedules — recorded pickups re-anchored via
    max(static, actual). Every after-the-move picture is drawn on these because
    they are exactly what ``check_feasibility`` and ``validate_post_move_board``
    saw when they blessed the plan; drawing the detection clock instead would
    show a board the engine never validated."""
    from dispatching.conflict_advisor import _planning
    try:
        return _planning(board)[0] or {}
    except Exception:
        return board.schedules or {}


def _slot_index(board, schedules=None):
    """{leg_id: ScheduleSlot} across every in-house driver on the board.

    Includes the guard-3 overnight tail: an overlap chained across midnight has
    its earlier leg on the PREVIOUS date, so that slot lives in ``prev_tail``
    and not in today's schedules. Without it the cross-midnight card loses its
    committed-run block — the very half of the picture that explains the
    problem.
    """
    idx = {}
    for sched in (schedules if schedules is not None
                  else (board.schedules or {})).values():
        for s in sched.slots:
            idx[s.leg_id] = s
    for pairs in (getattr(board, "prev_tail", None) or {}).values():
        for slot, _picked in pairs:
            idx.setdefault(slot.leg_id, slot)
    return idx


def _pickup_dt(board, leg_or_slot, slot=None):
    """The absolute moment this job starts.

    A slot's ``chain_clear_dt`` is already absolute (guard 3 chains the
    00:00–02:00 tail to the previous evening), so when one is present we take
    the date from it rather than assuming the board's target date — that is what
    keeps an overnight pair from drawing as a 24-hour-wide block.
    """
    t = getattr(leg_or_slot, "pickup_time", None)
    if t is None:
        return None
    ref = getattr(slot, "chain_clear_dt", None) if slot is not None else None
    if ref is None:
        ref = getattr(leg_or_slot, "chain_clear_dt", None)
    day = board.target_date
    if isinstance(ref, datetime):
        cand = datetime.combine(ref.date(), t)
        if cand > ref:                      # clear can never precede pickup
            cand -= timedelta(days=1)
        return cand
    return datetime.combine(day, t)


def _clear_dt(board, leg, slot=None, leg_id=None, detection=False):
    """When the driver is done with this job and free to drive on.

    TWO CLOCKS, exactly as the engine keeps them:
      * DETECTION (``detection=True``) — what is true now. Once a pickup has
        actually been recorded the dwell is no longer an estimate, and the
        detector re-anchored its slack on that tap. Drawing the static clear
        instead would put the run block on a different clock from the bracket
        measuring it: the block would end early and the difference would
        surface as a phantom "drive back + reset" nobody is driving.
      * PLANNING (the default) — validating future placements, NEVER
        optimistic. The planning schedules already carry max(static, actual) on
        the slot, so the after-the-move picture takes the slot as given. An
        optimistic clear here would draw a board the validator never blessed.
    """
    lid = leg_id if leg_id is not None else getattr(leg, "id", None)
    picked = (board.picked_up_by_leg or {}).get(lid) if detection else None
    if picked is not None:
        from dispatching.scheduler import chain_clear_dt_from_actual
        src = leg
        if src is None and slot is not None:
            from dispatching.board_validation import _slot_leg_shim
            src = _slot_leg_shim(slot)
        if src is not None:
            try:
                # Store taps sharpen this further still — and the shim carries no
                # history, so the resolved state has to be handed in.
                return chain_clear_dt_from_actual(
                    src, picked,
                    store_state=(getattr(board, "store_by_leg", None) or {}).get(lid))
            except Exception:
                pass
    if slot is not None and getattr(slot, "chain_clear_dt", None):
        return slot.chain_clear_dt
    from dispatching.scheduler import chain_clear_dt
    try:
        return chain_clear_dt(leg, board.target_date)
    except Exception:
        return None


def _trip_type(leg, slot):
    """arrival | return | cruise | other — the board's own job classification,
    so a run is the same colour here as it is on the timeline a dispatcher
    already reads all day."""
    t = getattr(slot, "trip_type", None) if slot is not None else None
    if not t and leg is not None:
        try:
            t = leg.get_trip_type()
        except Exception:
            t = None
    t = (t or "other").lower()
    return t if t in ("arrival", "return", "cruise") else "other"


def _job(board, leg_id, slots=None, detection=False):
    """One drawable job: {leg_id, start, end, route, time, trip} or None.

    ``detection`` picks the clock — see _clear_dt. The conflict picture is
    detection (what is true now); every after-the-move picture is planning."""
    leg = (board.legs_by_id or {}).get(leg_id)
    slot = (slots or {}).get(leg_id)
    src = leg if leg is not None else slot
    if src is None:
        return None
    start = _pickup_dt(board, src, slot)
    end = _clear_dt(board, leg, slot, leg_id=leg_id, detection=detection)
    if start is None or end is None or end < start:
        return None
    return {"leg_id": leg_id, "start": start, "end": end,
            "route": plain_route(src), "time": clock(start),
            "trip": _trip_type(leg, slot)}


def _driver_jobs(board, driver_id, exclude=(), schedules=None):
    """Every job on one in-house driver's day, in time order, as drawable dicts.

    Returns [] for a driver we hold no schedule for — an affiliate. That is a
    fact, not a failure: affiliate days are not on our board, so the lane is
    drawn with the run alone rather than with invented free time around it.
    """
    sched = (schedules if schedules is not None
             else (board.schedules or {})).get(driver_id)
    if sched is None:
        return []
    out = []
    for slot in sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id)):
        if slot.leg_id in exclude:
            continue
        j = _job(board, slot.leg_id, {slot.leg_id: slot})
        if j is not None:
            out.append(j)
    return out


def _driver_name(board, driver_id):
    from dispatching.conflict_advisor import _driver_name as engine_name
    if driver_id is None:
        return ""
    try:
        return engine_name(board, driver_id)
    except Exception:
        return f"driver {driver_id}"


def _first_name(name):
    return (name or "").strip().split(" ")[0] or (name or "")


def _initial(name):
    n = (name or "").strip()
    return n[0].upper() if n else "?"


def _window_is_guess(board, driver_id):
    """True when this driver's availability is an observed-history stub rather
    than a configured shift — the engine's own ``window_sources`` verdict."""
    return ((board.window_sources or {}).get(driver_id) == "stub"
            and (board.windows or {}).get(driver_id) is not None)


# ════════════════════════════════════════════════════════════════════════════
# CARD — the problem, in plain language and one picture
# ════════════════════════════════════════════════════════════════════════════

# What the engine's ``basis`` actually tells a dispatcher: WHICH SIGNAL found
# this. The vocabulary itself (clock_only, gps_fresh, …) stays on the card for
# the expander; this is the plain reading of it.
#
# These describe what was MEASURED — never what was cleared. `basis` records the
# detecting signal and nothing else: detection never inspects the assigned car,
# the driver's credentials, or the booking. A chip reading "vehicle, licensing
# and everything else check out" would make a safety claim on the engine's
# behalf, and would be flatly wrong on a leg that does have a vehicle problem.
_SCOPE_BY_BASIS = {
    "clock_only": ("Going by the clock",
                   "worked out from the schedule, not from live tracking"),
    "recorded_pickup": ("Going by the recorded pickup",
                        "measured from the time the driver actually tapped"),
    "gps_fresh": ("Confirmed by live tracking",
                  "the vehicle's own position right now says so"),
    "gps_stale_parked": ("Going by the clock",
                         "the vehicle hasn't moved and hasn't checked in"),
    "flight": ("Going by the flight",
               "worked out from where the plane actually is"),
}


def _scope(d):
    """The scope chip only belongs on cards that ARE a timing problem. On a
    hygiene card ("nobody tapped the button") or an abstain card ("we don't
    know which night this lands") it would assert a diagnosis the engine
    explicitly declined to make."""
    if d.hygiene or d.abstain:
        return None
    label = _SCOPE_BY_BASIS.get(d.basis)
    if not label:
        return None
    return {"text": label[0], "detail": label[1]}


def _overlap_facts(board, d, slots):
    """The four numbers the double-booked picture is built from, or None.

    ``ready`` is derived as ``impact − slack`` on purpose: that is the engine's
    own definition of the shortfall, so the bracket on screen is the engine's
    arithmetic rather than a second opinion about it.
    """
    if len(d.leg_ids) < 2 or d.impact_dt is None:
        return None
    slack = d.details.get("slack")
    if slack is None:
        return None
    committed = _job(board, d.leg_ids[0], slots, detection=True)
    blocked = _job(board, d.leg_ids[1], slots, detection=True)
    if committed is None or blocked is None:
        return None
    return {"committed": committed, "blocked": blocked,
            "due": d.impact_dt,
            "ready": d.impact_dt - timedelta(minutes=slack),
            "slack": int(slack),
            # DOUBLE-BOOKED means what a dispatcher means by it: the second
            # pickup falls while the driver is still carrying the first guest.
            # A driver whose 11:00 run drops off at 12:15 and who then can't get
            # back for his own 12:30 is NOT double-booked — he has one job at
            # 12:30 and he is going to be late for it. Calling that
            # "double-booked" describes a board that does not exist.
            "same_time": d.impact_dt < committed["end"],
            # PRIME DIRECTIVE. A warning-band overlap has POSITIVE slack: the
            # engine says the turn works, reality just thinned it. Drawing that
            # as a shortfall — red block, "no driver free", "N min short" —
            # would flag a turn the engine calls legal, which is the one thing
            # this panel must never do.
            "broken": slack < 0}


def _cascade_facts(board, d, slots):
    """Late-cascade / overrun share the double-booked shape: one job running
    long or late, and the FIRST job downstream it breaks.

    The first, not the worst. Later breaks in the chain were measured against
    the carried-forward clear of the ones before them, so pairing the anchor
    with the worst break would draw a reset block straight over the job that
    actually breaks first — and name a different pickup than the card's own
    headline and countdown, which both key off ``impact_dt``.
    """
    breaks = [b for b in (d.details.get("breaks") or []) if b[1] is not None]
    if not breaks:
        return None
    anchor = _job(board, d.anchor_leg_id, slots, detection=True)
    if anchor is None:
        return None
    jobs = [(b, _job(board, b[0], slots, detection=True)) for b in breaks]
    jobs = [(b, j) for b, j in jobs if j is not None]
    if not jobs:
        return None
    # Prefer the break the card is already counting down to; otherwise the
    # earliest one on the clock.
    first = next((p for p in jobs if p[1]["start"] == d.impact_dt), None)
    if first is None:
        first = min(jobs, key=lambda p: (p[1]["start"], p[0][0]))
    (leg_id, slack), blocked = first
    due = _due_dt(board, leg_id, blocked["start"])
    return {"committed": anchor, "blocked": blocked,
            "due": due,
            "ready": due - timedelta(minutes=slack),
            "slack": int(slack), "broken": True}


def _due_dt(board, leg_id, fallback):
    """The moment the driver is truly due at this pickup — the same
    flight-relaxed deadline the detector measured its slack against. A delayed
    plane moves the deadline out; using the booked time instead would shift the
    whole picture earlier than the arithmetic behind it."""
    leg = (board.legs_by_id or {}).get(leg_id)
    if leg is None:
        return fallback
    try:
        from dispatching.conflict_advisor import _effective_pickup_dt
        return _effective_pickup_dt(leg, board.target_date) or fallback
    except Exception:
        return fallback


def _flight_facts(board, d, slots):
    """A late plane that breaks the turn OUT draws the same shape as a
    double-booking: the run that overruns, the drive back, and the pickup that
    can no longer be made. Only when the engine says the turn is actually
    broken (``slack_out`` negative) — a flight that merely moved has no gap to
    draw, and drawing one would invent a problem."""
    out = d.details.get("slack_out")
    if out is None or out >= 0 or len(d.leg_ids) < 2:
        return None
    committed = _job(board, d.leg_ids[0], slots, detection=True)
    blocked = _job(board, d.leg_ids[1], slots, detection=True)
    if committed is None or blocked is None:
        return None
    due = _due_dt(board, d.leg_ids[1], blocked["start"])
    return {"committed": committed, "blocked": blocked,
            "due": due,
            "ready": due - timedelta(minutes=out),
            "slack": int(out), "broken": True}


# Why the driver cannot make the second job — one line per card kind, because
# "two jobs at the same time" is only true of a double-booking.
_BRACKET_DETAIL = {
    "overlap": "the first run doesn't clear in time",
    "late_cascade": "the next job starts before this one can finish",
    "overrun": "the next job starts before this one can finish",
    "flight_change": "the plane lands too late to make the next pickup",
}


def _bracket_detail(d, facts):
    """Why the second pickup can't be made — the honest one-liner. "Two jobs at
    the same time" is only true when the pickups genuinely overlap."""
    if d.kind == "overlap" and facts.get("same_time"):
        return "two jobs at the same time"
    return _BRACKET_DETAIL.get(d.kind, "the next job starts too soon")


def _conflict_timeline(board, d):
    """The problem drawn once. Returns None for cards with no real geometry
    (hygiene cards, "confirm the takeoff date" cards) — those render as text."""
    slots = _slot_index(board)
    facts = None
    if d.kind == "overlap":
        facts = _overlap_facts(board, d, slots)
    elif d.kind in ("late_cascade", "overrun"):
        facts = _cascade_facts(board, d, slots)
    elif d.kind == "flight_change":
        facts = _flight_facts(board, d, slots)
    elif d.kind == "unassigned":
        return _uncovered_timeline(board, d, slots)
    if facts is None:
        return None

    committed, blocked = facts["committed"], facts["blocked"]
    ready, due, slack = facts["ready"], facts["due"], facts["slack"]
    broken = facts.get("broken", True)
    axis = Axis.around([committed["start"], committed["end"], ready,
                        due, blocked["end"]])
    if axis is None:
        return None
    name = _driver_name(board, d.driver_id)
    short = _first_name(name)

    lanes = [{
        "driver": name, "initial": _initial(name), "role": "driver",
        "blocks": _placed([
            {"type": "run", "trip": committed["trip"],
             "start": committed["start"], "end": committed["end"],
             "time": committed["time"], "label": committed["route"]},
            {"type": "reset", "trip": "", "start": committed["end"],
             "end": ready, "time": "", "label": "drive back + reset"},
        ], axis),
    }, {
        "driver": "", "initial": "", "role": "conflict",
        "blocks": _placed([
            {"type": "conflict" if broken else "tight",
             "trip": blocked["trip"], "start": due,
             "end": blocked["end"], "time": clock(due),
             # On an overlap the run IS assigned — to the very driver who
             # can't reach it. "No driver free" would describe an unassigned
             # run, which this is not.
             "label": (f"{blocked['route']} — {short or 'the driver'} "
                       f"can't make it" if broken
                       else f"{blocked['route']} — {duration(slack)} to spare")},
        ], axis),
    }]

    markers = [
        {"kind": "pickup", "left_pct": axis.pct(due),
         "label": f"{_short_route(blocked['route'])} pickup · {clock(due)}"},
        {"kind": "free", "left_pct": axis.pct(ready),
         "label": f"{short} free · ~{clock(ready)}"},
    ]
    left, width = axis.span(due, ready, min_width=_MIN_BLOCK_PCT)
    bracket = {
        "left_pct": left, "width_pct": width,
        "mid_pct": round(left + width / 2.0, 3),
        "tone": "red" if broken else "amber",
        "label": (f"{duration(slack)} short" if broken
                  else f"{duration(slack)} to spare"),
        "detail": (_bracket_detail(d, facts) if broken
                   else "it works, but there is nothing spare"),
    }
    who = short or "this driver"
    if d.kind == "overlap" and broken and facts.get("same_time"):
        subtitle = (f"Both runs are on {who}. The red bracket is the gap "
                    f"{who} can't close.")
    elif d.kind == "overlap" and broken:
        subtitle = (f"Both runs are on {who}, back to back. The red bracket is "
                    f"how late the first one makes {who} for the second.")
    elif broken:
        subtitle = f"The red bracket is how far behind {who} ends up."
    else:
        subtitle = (f"Both runs are on {who} and the turn still works — the "
                    f"bracket is all the room {who} has left.")
    return {
        "title": _timeline_title(board, d),
        "subtitle": subtitle,
        "axis": {"ticks": axis.ticks()},
        "lanes": lanes, "markers": markers, "bracket": bracket,
        "legend": _legend(lanes, "the pickup that gets missed"),
    }


# Run colours are the board's own trip-type palette, so the legend names trip
# types rather than inventing a second vocabulary for the same colours.
_TRIP_LABEL = {"arrival": "airport arrival", "return": "departure",
               "cruise": "cruise", "other": "other run"}


def _legend(lanes, conflict_label="the run nobody's free for"):
    """Only the keys actually present on this picture — a legend listing
    colours that aren't on screen is noise a dispatcher has to filter."""
    trips, has_reset, has_conflict, has_tight = [], False, False, False
    for lane in lanes:
        for b in lane.get("blocks", []):
            if b["type"] == "reset":
                has_reset = True
            elif b["type"] == "conflict":
                has_conflict = True
            elif b["type"] == "tight":
                has_tight = True
            elif b.get("trip") and b["trip"] not in trips:
                trips.append(b["trip"])
    out = [{"cls": f"trip-{t}", "label": _TRIP_LABEL.get(t, t)} for t in trips]
    if has_reset:
        out.append({"cls": "b-reset", "label": "driving back + reset"})
    if has_conflict:
        out.append({"cls": "b-conflict", "label": conflict_label})
    if has_tight:
        out.append({"cls": "b-tight", "label": "makes it, but only just"})
    return out


def _uncovered_timeline(board, d, slots):
    """An unassigned leg has no committed run and no old driver — the picture is
    the job itself sitting on the board with nobody under it."""
    job = _job(board, d.anchor_leg_id, slots, detection=True)
    if job is None:
        return None
    # ONE pickup time per run. The headline quotes impact_dt (the flight-relaxed
    # deadline the driver is actually due at), so the block and the marker must
    # quote it too — a delayed plane otherwise puts the booked time under a
    # headline showing the deadline, two times for the same run on one card.
    due = _due_dt(board, d.anchor_leg_id, job["start"])
    if d.impact_dt is not None:
        due = d.impact_dt
    end = max(job["end"], due)
    axis = Axis.around([due, end])
    if axis is None:
        return None
    return {
        "title": _timeline_title(board, d),
        "subtitle": "Nobody is on this run yet.",
        "axis": {"ticks": axis.ticks()},
        "lanes": [{
            "driver": "", "initial": "", "role": "conflict",
            "blocks": _placed([
                {"type": "conflict", "trip": job["trip"], "start": due,
                 "end": end, "time": clock(due),
                 "label": f"{job['route']} — no driver assigned"},
            ], axis),
        }],
        "markers": [{"kind": "pickup", "left_pct": axis.pct(due),
                     "label": f"pickup · {clock(due)}"}],
        "bracket": None,
        "legend": [{"cls": "b-conflict",
                    "label": "the run nobody's free for"}],
    }


def _timeline_title(board, d):
    """"Tomorrow morning as it stands" — when the picture is set.

    English, not string concatenation: it is "this afternoon", never "today
    afternoon".
    """
    when = day_word(board.target_date, board.now_local.date())
    part = "morning"
    ref = d.impact_dt
    if isinstance(ref, datetime):
        if ref.hour >= 17:
            part = "evening"
        elif ref.hour >= 12:
            part = "afternoon"
    if when == "today":
        return f"This {part} as it stands"
    if when in ("tomorrow", "yesterday"):
        return f"{when.capitalize()} {part} as it stands"
    return f"{when[3:].capitalize() if when.startswith('on ') else when} " \
           f"{part}, as it stands"


def _short_route(route):
    """The pickup end of a route — "Port Orleans" from "Port Orleans → Sanford".
    Marker tags and prose both refer to a run by where it starts."""
    return (route or "").split("→")[0].strip() or (route or "")


def _placed(blocks, axis):
    """Attach geometry to drawable blocks; drop the degenerate ones."""
    out = []
    for b in blocks:
        if b["start"] is None or b["end"] is None or b["end"] <= b["start"]:
            continue
        left, width = axis.span(b["start"], b["end"], min_width=_MIN_BLOCK_PCT)
        item = {k: v for k, v in b.items() if k not in ("start", "end")}
        item["left_pct"], item["width_pct"] = left, width
        item["minutes"] = int(round((b["end"] - b["start"]).total_seconds() / 60))
        out.append(item)
    return out


# ── headline + story ────────────────────────────────────────────────────────

def _headline(board, d):
    """The problem named the way a dispatcher would say it."""
    name = _first_name(_driver_name(board, d.driver_id))
    at = clock(d.impact_dt)
    # Hygiene first — a "chase the button" card SHARES the late_cascade kind but
    # is the opposite claim: nobody is late, nobody tapped the app. Reading the
    # kind alone and calling it lateness is exactly the kind of wrong a
    # dispatcher acts on at 4 a.m.
    # An UNASSIGNED hygiene card is not a missed button — there is no driver and
    # nothing to tap. It has to be read as coverage, before the hygiene branch,
    # or a dispatcher closes an uncovered ride.
    if d.hygiene and d.kind != "unassigned":
        return (f"Nobody has marked the {at} pickup as done" if at
                else "A pickup has not been marked as done")
    if d.kind == "flight_change" and d.details.get("reason") == \
            "overnight_unconfirmed":
        return (f"Check which night the {at} flight actually lands" if at
                else "Check which night this flight actually lands")
    if d.kind == "overlap":
        if d.severity == "critical":
            f = _overlap_facts(board, d, _slot_index(board))
            if f and not f["same_time"] and name and at:
                # Sequential jobs, first one overruns — say what will actually
                # happen to the person, not that the board is impossible.
                return (f"{name} will be {duration_words(f['slack'])} late for "
                        f"the {at} pickup")
            return (f"{name} is double-booked at {at}" if name and at
                    else "Two jobs land on one driver at once")
        return (f"{name}'s {at} turn is getting tight" if name and at
                else "A turn is getting tight")
    if d.kind == "unassigned":
        leg = (board.legs_by_id or {}).get(d.anchor_leg_id)
        route = plain_route(leg) if leg is not None else "this run"
        return f"Nobody is on the {at} {route} pickup" if at else \
               f"Nobody is on the {route} pickup"
    if d.kind == "late_cascade":
        n = len(d.details.get("breaks") or [])
        if name and n:
            return (f"{name} is running late — {n} later job"
                    f"{'s' if n != 1 else ''} at risk")
        return f"{name} is running late" if name else "A driver is running late"
    if d.kind == "overrun":
        n = len(d.details.get("breaks") or [])
        if name and n:
            return (f"{name}'s current job is running long — {n} later job"
                    f"{'s' if n != 1 else ''} at risk")
        return (f"{name}'s current job is running long" if name
                else "A job is running long")
    if d.kind == "flight_change":
        # The consequence leads, always — and it leads as a person: "George
        # will be 13 minutes late", not "the turn out goes 13 min short".
        # There is no longer a fallback branch that names the plane on its own
        # ("a flight is landing 17 minutes later"): a card without a
        # consequence no longer exists, so a headline for one would be a
        # headline for a card that cannot be drawn. What moved is the CAUSE,
        # and the cause belongs in the story, not the headline.
        out = d.details.get("slack_out")
        if out is not None and out < 0:
            late = clock(d.impact_dt)
            if name:
                return (f"{name} will be {duration_words(out)} late for the "
                        f"{late} pickup" if late else
                        f"{name} will be {duration_words(out)} late")
            return (f"The {late} pickup is {duration_words(out)} out of reach"
                    if late else "The next pickup is out of reach")
        if out is not None:
            why = ("the plane moved" if d.details.get("flight_shift_min")
                   else "the pickup time moved")
            return (f"{name}'s next turn is down to {duration_words(out)} "
                    f"because {why}" if name else
                    f"The next turn is down to {duration_words(out)} "
                    f"because {why}")
        return d.headline
    return d.headline


def _booked_dt(board, d):
    """The pickup time as booked — the anchor a flight card talks about.
    ``impact_dt`` on those cards points at the BROKEN NEXT job, not this one."""
    leg = (board.legs_by_id or {}).get(d.anchor_leg_id)
    if leg is None or getattr(leg, "pickup_time", None) is None:
        return d.impact_dt
    return datetime.combine(board.target_date, leg.pickup_time)


def _story(board, d):
    """The problem told as what happens, with real times and no jargon.

    Falls back to the engine's own narrative for kinds with no story template —
    never to silence.
    """
    slots = _slot_index(board)
    name = _first_name(_driver_name(board, d.driver_id))
    if d.hygiene and d.kind != "unassigned":
        job = _job(board, d.anchor_leg_id, slots, detection=True)
        overdue = d.details.get("overdue_min")
        where = f"{job['time']} {job['route']}" if job else "this"
        ago = (f"{duration_words(overdue)} ago" if overdue else "a while back")
        return (f"The {where} pickup was due {ago} and nothing has been tapped "
                f"on it. That is almost always a missed button rather than a "
                f"driver in trouble — check the ride happened, then get the "
                f"status put right.")
    if d.kind == "flight_change" and d.details.get("reason") == \
            "overnight_unconfirmed":
        return ("This flight number lands every night, and we do not have a "
                "confirmed takeoff date — so we cannot tell which night this "
                "pickup belongs to. Confirm the date before anything else is "
                "planned around it.")
    if d.kind == "overlap":
        f = _overlap_facts(board, d, slots)
        if f:
            c, b = f["committed"], f["blocked"]
            gap = int(round((b["start"] - c["start"]).total_seconds() / 60))
            who = name or "one driver"
            if f.get("broken", True) and not f["same_time"]:
                # Back-to-back jobs where the first runs long. One job at the
                # second time, not two.
                return (f"{who}'s {c['time']} {_short_route(c['route'])} run "
                        f"doesn't drop off until {clock(c['end'])}, and after "
                        f"driving back {who} isn't ready again until about "
                        f"{clock(f['ready'])} — "
                        f"{duration_words(f['slack'])} after the {b['time']} "
                        f"{_short_route(b['route'])} pickup {who} is also on.")
            lead = (f"Two runs land on {who} {duration_words(gap)} apart."
                    if gap else f"Two runs land on {who} at the same time.")
            middle = (f"Once {who} takes the {c['time']} "
                      f"{_short_route(c['route'])} pickup, {who} isn't dropped "
                      f"off, back, and ready again until about "
                      f"{clock(f['ready'])}")
            if f.get("broken", True):
                return (f"{lead} {middle} — so {who} would be "
                        f"{duration_words(f['slack'])} late for the "
                        f"{b['time']} {_short_route(b['route'])} pickup.")
            # Positive slack: the turn works. Say so, and say what is left.
            return (f"{lead} {middle}, which still makes the {b['time']} "
                    f"{_short_route(b['route'])} pickup — but with only "
                    f"{duration_words(f['slack'])} to spare. Nothing can go "
                    f"wrong in between.")
    if d.kind in ("late_cascade", "overrun") and not (
            d.details.get("breaks") or []):
        # Nothing downstream breaks — this is a "keep an eye on it" card. The
        # engine's own narrative here is raw scheduler prose, so it never
        # reaches the surface.
        job = _job(board, d.anchor_leg_id, slots, detection=True)
        who = name or "the driver"
        if job and d.kind == "overrun":
            over = d.details.get("overrun_min")
            late = f" — {duration_words(over)} longer than expected" if over else ""
            return (f"{who} is still out on the {job['time']} "
                    f"{_short_route(job['route'])} run{late}. Nothing later on "
                    f"the day is broken by it yet, but it is worth a look.")
        if job:
            return (f"{who} is behind on the {job['time']} "
                    f"{_short_route(job['route'])} run. Nothing later on the "
                    f"day breaks because of it yet — keep an eye on it.")
    if d.kind in ("late_cascade", "overrun"):
        f = _cascade_facts(board, d, slots)
        if f:
            c, b = f["committed"], f["blocked"]
            who = name or "the driver"
            verb = ("is running behind on" if d.kind == "late_cascade"
                    else "is still out on")
            return (
                f"{who} {verb} the {c['time']} {_short_route(c['route'])} run "
                f"and won't be free until about {clock(f['ready'])} — so {who} "
                f"would be {duration_words(f['slack'])} late for the "
                f"{b['time']} {_short_route(b['route'])} pickup.")
    if d.kind == "unassigned":
        job = _job(board, d.anchor_leg_id, slots, detection=True)
        if job:
            due = d.impact_dt or _due_dt(board, d.anchor_leg_id, job["start"])
            mins = d.details.get("mins_to_pickup")
            if mins is not None and mins < 0:
                return (f"The {clock(due)} {job['route']} run still has no "
                        f"driver and the pickup time passed "
                        f"{duration_words(mins)} ago. Nobody is on it.")
            return (f"The {clock(due)} {job['route']} run has no driver on "
                    f"the board. Nothing is holding it.")
    if d.kind == "flight_change":
        leg = (board.legs_by_id or {}).get(d.anchor_leg_id)
        if leg is None:
            return d.narrative
        booked = clock(leg.pickup_time)
        run = f"{booked} {_short_route(plain_route(leg))}"
        bits = []
        div = d.details.get("divergence_min")
        if div:
            way = "later" if div > 0 else "earlier"
            bits.append(f"The flight for the {run} run is landing "
                        f"{duration_words(div)} {way} than the pickup was "
                        f"booked for.")
        if d.details.get("unacked"):
            bits.append("The pickup time on the board changed and nobody has "
                        "acknowledged it yet.")
        out = d.details.get("slack_out")
        nxt = (_job(board, d.leg_ids[1], slots, detection=True)
               if len(d.leg_ids) >= 2 else None)
        who = name or "the driver"
        if out is not None and out < 0:
            where = (f"the {nxt['time']} {_short_route(nxt['route'])} pickup"
                     if nxt else f"the {clock(d.impact_dt)} pickup")
            bits.append(f"Knock-on: {who} will be {duration_words(out)} late "
                        f"for {where}.")
        elif out is not None and out < _tight_min():
            where = (f"the {nxt['time']} {_short_route(nxt['route'])} pickup"
                     if nxt else "the next pickup")
            bits.append(f"That leaves {who} only {duration_words(out)} to get "
                        f"to {where}.")
        if d.details.get("affiliate"):
            bits.append(f"This one is with {_driver_name(board, d.driver_id)} "
                        f"— call them first; you can't reliably pull a run "
                        f"back the same day once they have it.")
        if bits:
            return " ".join(bits)
    return d.narrative


_DIAG_PLAIN = {
    "Outside driver window": "is outside the hours we have for them",
    "observed-history window (provisional), not a configured shift":
        "and those hours are a guess from when they usually work",
    "vehicle class can't take this job": "is in the wrong class of car",
    "shares the physical car with a working partner":
        "is sharing a car with someone who is already out in it",
}


def _why_nobody(board, d, diagnostic):
    """"Why nobody can take it", in dispatcher terms.

    The engine's diagnostic is per-driver scheduler prose — it names the window
    token and prints 24-hour times. Rewriting the phrases we recognise keeps the
    substance (WHICH driver, WHY not) without the vocabulary; the raw string
    still goes out verbatim on the card for the expander.
    """
    if not diagnostic:
        return ""
    text = diagnostic
    for raw, plain in _DIAG_PLAIN.items():
        text = text.replace(raw, plain)
    text = text.replace("No in-house driver can absorb it",
                        "No one of ours can take it")
    return text


def card_display(board, d):
    """The dispatcher-facing view of one disruption card."""
    return {
        "headline": _headline(board, d),
        "story": _story(board, d),
        "scope": _scope(d),
        "conflict": _conflict_timeline(board, d),
        "why_nobody": _why_nobody(board, d,
                                  (d.details or {}).get("swap_diagnostic")),
    }


# ════════════════════════════════════════════════════════════════════════════
# PLAN — what the board looks like AFTER the move
# ════════════════════════════════════════════════════════════════════════════

def _move_shape(board, plan):
    """Who loses work and who gains it, as ids — the skeleton every after-picture
    is drawn on. ``primary`` is the move whose landing the card is really about
    (the last reassignment; for a farm plan, the farm-out)."""
    reassigns, farms, unassigns, retimes = [], [], [], []
    for m in plan.moves:
        if m.op == "reassign":
            reassigns.append(m)
        elif m.op == "farm_out":
            farms.append(m)
        elif m.op == "unassign":
            unassigns.append(m)
        elif m.op == "retime":
            retimes.append(m)
    primary = (reassigns[-1] if reassigns else
               (farms[0] if farms else
                (unassigns[0] if unassigns else
                 (retimes[0] if retimes else None))))
    return {"reassigns": reassigns, "farms": farms, "unassigns": unassigns,
            "retimes": retimes, "primary": primary}


def _retimed_leg(board, plan, leg_id):
    """A read-only copy of the leg at its NEW pickup time, or None.

    A ``match_flight`` plan moves a pickup as well as (sometimes) its driver. If
    the after-picture drew that run at its old time it would show a board the
    plan does not produce — so the retimed leg is redrawn at ``new_time``, with
    its clear time recomputed from there.
    """
    import copy as _copy
    for c in (plan.time_changes or []):
        if c.leg_id != leg_id:
            continue
        leg = (board.legs_by_id or {}).get(leg_id)
        if leg is None or c.new_time is None:
            return None
        shifted = _copy.copy(leg)          # never saved; chain math reads only
        shifted.pickup_time = c.new_time   # pickup_time + locations + flight
        return shifted
    return None


def _sim_job(board, leg, leg_id, *, retimed=False):
    """One drawable job for a leg being placed somewhere it is not yet, using
    ``_make_sim_slot`` — the same simulated slot the validator built.

    If the slot cannot be built for a RETIMED leg we return None rather than
    falling back: the fallback reads the leg off the board at its OLD pickup
    time, which would draw a board this plan does not produce, under a heading
    that says "after the move". Omitting the lane is honest; drawing the wrong
    time silently is not.
    """
    from dispatching.scheduler import _make_sim_slot
    try:
        slot = _make_sim_slot(leg, board.target_date)
    except Exception:
        slot = None
    if slot is None and retimed:
        return None
    j = _job(board, leg_id, {leg_id: slot} if slot is not None else None)
    if j is not None and slot is not None:
        # _job reads the leg from the board (old time) when one exists; a
        # retimed leg must be drawn from the simulated slot instead.
        j["start"] = _pickup_dt(board, slot, slot)
        j["end"] = slot.chain_clear_dt or j["end"]
        j["time"] = clock(j["start"])
    return j


def _post_move_jobs(board, driver_id, plan, schedules=None):
    """That driver's jobs after this plan runs: their own, minus what leaves,
    plus what lands, with any retimed run redrawn at its new time. Built on the
    planning-clock schedules the validator used, so the picture and the verdict
    describe the same board."""
    scheds = schedules if schedules is not None else _planning_schedules(board)
    leaving = {m.leg_id for m in plan.moves
               if m.from_driver_id == driver_id and m.op != "retime"}
    retimed = {c.leg_id for c in (plan.time_changes or [])}
    jobs = [j for j in _driver_jobs(board, driver_id, exclude=leaving,
                                    schedules=scheds)
            if j["leg_id"] not in retimed]
    landed = set()

    for m in plan.moves:
        if m.op != "reassign" or m.to_driver_id != driver_id:
            continue
        retimed_leg = _retimed_leg(board, plan, m.leg_id)
        leg = retimed_leg or (board.legs_by_id or {}).get(m.leg_id)
        if leg is None:
            continue
        j = _sim_job(board, leg, m.leg_id, retimed=retimed_leg is not None)
        if j is not None:
            jobs.append(j)
            landed.add(m.leg_id)

    # A retimed run that stays with its driver still has to move on the picture.
    for c in (plan.time_changes or []):
        if c.leg_id in landed:
            continue
        holder = getattr((board.legs_by_id or {}).get(c.leg_id), "driver_id", None)
        if holder != driver_id:
            continue
        leg = _retimed_leg(board, plan, c.leg_id)
        j = (_sim_job(board, leg, c.leg_id, retimed=True)
             if leg is not None else None)
        if j is not None:
            jobs.append(j)

    jobs.sort(key=lambda j: (j["start"], j["leg_id"]))
    return jobs, landed


def _turn_between(board, prev_job, next_job, slots, plan=None):
    """Minutes of real slack between two consecutive jobs on one driver, via the
    one shared formula (``turn_slack_minutes``) rather than a clock subtraction:
    a same-terminal MCO turn and a cross-town turn are not the same gap, and the
    board pill, the assignment engine and this drawing must agree on which."""
    from dispatching.board_validation import turn_slack_minutes
    from dispatching.scheduler import _make_sim_slot

    def _slot_for(job):
        retimed = _retimed_leg(board, plan, job["leg_id"]) if plan else None
        if retimed is not None:
            return _make_sim_slot(retimed, board.target_date)
        s = slots.get(job["leg_id"])
        if s is not None:
            return s
        leg = (board.legs_by_id or {}).get(job["leg_id"])
        return _make_sim_slot(leg, board.target_date) if leg is not None else None

    try:
        a, b = _slot_for(prev_job), _slot_for(next_job)
    except Exception:
        return None
    if a is None or b is None:
        return None
    try:
        return turn_slack_minutes(a, b, board.target_date)
    except Exception:
        return None


def _gap(board, axis, prev_job, next_job, receiver_first_name, slots, plan,
         landed_ids=()):
    """The room — or the lack of it — between two jobs on one lane.

    Returns ``(gap, drive_back)``. The clock gap between two jobs is NOT all
    free time: getting from one drop-off to the next pickup eats part of it. So
    the hatched drive-back is drawn explicitly and the green region covers only
    the slack that is genuinely spare — which means the region's WIDTH and its
    LABEL measure the same thing, at the same scale as every block beside it.
    Fold the drive time into the green band instead and a dispatcher eyeballing
    the picture reads free time the driver does not have.

    Roomy renders green and dashed; tight renders as a red sliver with the
    minutes on it. Which one it is comes from the engine's own threshold, not
    from how wide it happens to look.
    """
    slack = _turn_between(board, prev_job, next_job, slots, plan)
    if slack is None:
        return None, None
    gap_end = next_job["start"]
    free_from = gap_end - timedelta(minutes=max(0, int(slack)))
    # Clamp into the real window between the two jobs. Both edges matter:
    #   * lower — the drive back is not free time, so the green region starts
    #     after it, not at the drop-off;
    #   * upper — a same-terminal airport turn carries a NEGATIVE required
    #     turnaround (the deplaning grace), so the next pickup can legally
    #     begin before the previous job clears. Without this the region would
    #     be drawn backwards and a 2-minute sliver would render 8 minutes wide.
    free_from = min(max(free_from, prev_job["end"]), gap_end)
    drive_back = None
    if free_from > prev_job["end"]:
        drive_back = {"type": "reset", "trip": "", "start": prev_job["end"],
                      "end": free_from, "time": "", "label": "drive back"}
    left, width = axis.span(free_from, gap_end, min_width=_MIN_BLOCK_PCT)
    # The label states what is DRAWN, so a clamped region can never claim more
    # room than its own width. The engine's own figure stays in "Show the math".
    drawn = int(round((gap_end - free_from).total_seconds() / 60.0))
    side = "after" if next_job["leg_id"] not in landed_ids else "before"
    common = {"left_pct": left, "minutes": drawn, "slack_min": int(slack),
              "side": side}
    # Tight vs roomy is the ENGINE's call, never how wide it happens to look.
    if slack < _tight_min():
        return dict(common, kind="tight",
                    width_pct=max(width, _MIN_BLOCK_PCT),
                    label=(f"only {duration(drawn)}" if slack >= 0
                           else f"{duration(slack)} short")), drive_back
    who = f"{receiver_first_name}'s" if receiver_first_name else "the"
    nxt = "next run" if side == "after" else "run after it"
    return dict(common, kind="room", width_pct=width,
                label=f"{duration(drawn)} free before {who} {nxt}"), drive_back


def _lane(board, driver_id, plan, axis, *, landed_ids, cleared, moved_from,
          scheds, slots):
    """One driver's row in the after-picture."""
    jobs, landed = _post_move_jobs(board, driver_id, plan, schedules=scheds)
    name = _driver_name(board, driver_id)
    short = _first_name(name)
    blocks = []
    for j in jobs:
        blocks.append({
            "type": "moved" if j["leg_id"] in landed_ids else (
                "keep" if driver_id == moved_from["driver_id"] else "other"),
            "trip": j["trip"],
            "start": j["start"], "end": j["end"],
            "time": j["time"], "label": j["route"], "leg_id": j["leg_id"],
        })

    # The hole the move leaves behind. A dashed ghost sits on the donor's lane
    # exactly where the run used to be — directly above the solid block that now
    # holds it, because a reassignment changes the driver and not the clock. The
    # two line up vertically, and the receiving lane's tag draws a stem up to
    # meet this, so "it came off george" is something you SEE rather than read.
    for m in plan.moves:
        if m.op not in ("reassign", "farm_out") or m.from_driver_id != driver_id:
            continue
        gone = _job(board, m.leg_id, slots)
        if gone is None:
            continue
        to_name = (_driver_name(board, m.to_driver_id) if m.op == "reassign"
                   and m.to_driver_id else (m.to_label or "an affiliate"))
        blocks.append({
            "type": "ghost", "trip": gone["trip"],
            "start": gone["start"], "end": gone["end"], "time": gone["time"],
            "label": f"↓ to {_first_name(to_name)}", "leg_id": m.leg_id,
        })

    # Gaps only where they mean something: on either side of what just landed.
    # Each one also yields the drive-back that eats into it, drawn as its own
    # hatched block so the green region is only ever genuinely free time.
    gaps = []
    for i in range(len(jobs) - 1):
        if jobs[i]["leg_id"] not in landed_ids and \
                jobs[i + 1]["leg_id"] not in landed_ids:
            continue
        g, drive_back = _gap(board, axis, jobs[i], jobs[i + 1], short,
                             slots, plan, landed_ids=landed_ids)
        if g is not None:
            gaps.append(g)
        if drive_back is not None:
            blocks.append(drive_back)

    lane = {
        "driver": name, "initial": _initial(name),
        "role": "to" if landed else "from",
        "blocks": _placed(blocks, axis),
        # `cleared` is a statement about the whole lane, not about a moment on
        # it, so it rides in the name column. Anchoring it to a time put it
        # underneath whatever job happened to be there.
        "cleared": bool(cleared and not landed),
        "tag": None, "gaps": gaps,
    }
    if landed:
        # Name the driver THIS run actually came from. A swap chain hands work
        # along several drivers, so a single card-wide donor would tell a
        # dispatcher to call someone who never touched the run.
        first = next((j for j in jobs if j["leg_id"] in landed_ids), None)
        if first is not None:
            donor = next((m for m in plan.moves
                          if m.op == "reassign" and m.leg_id == first["leg_id"]),
                         None)
            donor_name = _driver_name(board, donor.from_driver_id) if (
                donor is not None and donor.from_driver_id) else ""
            lane["tag"] = {
                "kind": "moved", "left_pct": axis.pct(first["start"]),
                "text": (f"moved from {_first_name(donor_name)}" if donor_name
                         else "was unassigned")}

    if landed and len(jobs) == 1:
        lane["gaps"].append({
            "kind": "room", "left_pct": axis.pct(jobs[0]["end"]),
            "width_pct": round(100.0 - axis.pct(jobs[0]["end"]), 3),
            "minutes": None,
            "label": f"nothing else on {short}'s day after this"
                     if short else "nothing else after this"})
    return lane, jobs


def _affiliate_lane(board, move, axis, slots):
    """A farmed run leaves the in-house board entirely — the receiving lane is
    the affiliate, with no schedule behind it to draw. We do not hold their day,
    so no free time is drawn around the run: an empty lane would read as
    availability we cannot actually see."""
    job = _job(board, move.leg_id, slots)
    if job is None:
        return None
    name = move.to_label or "affiliate"
    blocks = _placed([{"type": "moved", "trip": job["trip"],
                       "start": job["start"], "end": job["end"],
                       "time": job["time"], "label": job["route"],
                       "leg_id": job["leg_id"]}], axis)
    return {
        "driver": name, "initial": _initial(name), "role": "affiliate",
        "blocks": blocks, "gaps": [], "cleared": False,
        "tag": ({"kind": "moved", "left_pct": axis.pct(job["start"]),
                 "text": f"farmed out of {_first_name(move.from_label)}'s day"}   # a driver
                if move.from_label else
                {"kind": "moved", "left_pct": axis.pct(job["start"]),
                 "text": "farmed out"}),
        "note": "outside our fleet — no live tracking",
    }


def _after_timeline(board, d, plan):
    """The board after this plan, driver by driver. None for plans that move
    nothing (monitor) — there is no "after" to draw."""
    shape = _move_shape(board, plan)
    if not plan.moves or shape["primary"] is None:
        return None

    landed_ids = {m.leg_id for m in shape["reassigns"]}
    farm_ids = {m.leg_id for m in shape["farms"]}
    from_ids, to_ids = [], []
    for m in plan.moves:
        if m.from_driver_id and m.from_driver_id in (board.schedules or {}):
            if m.from_driver_id not in from_ids:
                from_ids.append(m.from_driver_id)
        if m.op == "reassign" and m.to_driver_id and m.to_driver_id not in to_ids:
            to_ids.append(m.to_driver_id)
    order = [x for x in from_ids if x not in to_ids] + to_ids
    if not order and not farm_ids:
        return None

    scheds = _planning_schedules(board)
    slots = _slot_index(board, scheds)

    # One axis over every lane, so the rows are comparable at a glance.
    moments, per_lane = [], {}
    for did in order:
        jobs, _ = _post_move_jobs(board, did, plan, schedules=scheds)
        per_lane[did] = jobs
        for j in jobs:
            moments += [j["start"], j["end"]]
    for m in shape["farms"]:
        j = _job(board, m.leg_id, slots)
        if j:
            moments += [j["start"], j["end"]]
    axis = Axis.around(moments)
    if axis is None:
        return None

    primary = shape["primary"]
    moved_from = {"driver_id": primary.from_driver_id,
                  "name": _driver_name(board, primary.from_driver_id)
                          if primary.from_driver_id else ""}

    lanes = []
    for did in order:
        # "Conflict cleared" is only said when it is TRUE: the card named a set
        # of legs that collided on this driver, and after the move at most one
        # of them is still on them. Anything less and the lane stays untagged
        # rather than claiming a fix it did not deliver.
        card_legs = set(d.leg_ids or [])
        still = {j["leg_id"] for j in per_lane.get(did, [])} & card_legs
        cleared = bool(card_legs) and len(still) <= 1 and \
            any(m.from_driver_id == did and m.op != "retime" and
                m.leg_id in card_legs for m in plan.moves)
        lane, _ = _lane(board, did, plan, axis, landed_ids=landed_ids,
                        cleared=cleared, moved_from=moved_from,
                        scheds=scheds, slots=slots)
        if lane["blocks"] or lane["tag"] or lane["cleared"]:
            lanes.append(lane)
    for m in shape["farms"]:
        al = _affiliate_lane(board, m, axis, slots)
        if al is not None:
            lanes.append(al)
    if not lanes:
        return None
    return {"axis": {"ticks": axis.ticks()}, "lanes": lanes}


# ── plan headline, outcome, warnings ────────────────────────────────────────

def _plan_headline(board, d, plan):
    """What the move IS, said once, in the dispatcher's words."""
    shape = _move_shape(board, plan)
    legs = board.legs_by_id or {}

    def _run(leg_id):
        leg = legs.get(leg_id)
        if leg is None:
            return "the run"
        return f"the {clock(leg.pickup_time)} {plain_route(leg)} run"

    if plan.kind == "monitor":
        return "Leave it alone and watch it"
    if plan.kind == "match_flight":
        base = f"Move {_run(plan.target_leg_id)} to match its flight"
        if shape["reassigns"]:
            m = shape["reassigns"][-1]
            return (f"{base}, and hand {_run(m.leg_id)} to "
                    f"{_first_name(_driver_name(board, m.to_driver_id))}")
        return base
    if plan.kind in ("reassign", "takeback") and shape["reassigns"]:
        m = shape["reassigns"][-1]
        who = _first_name(_driver_name(board, m.to_driver_id))
        if plan.kind == "takeback":
            return (f"Take {_run(m.leg_id)} back from "
                    f"{_first_name(m.from_label) or 'the affiliate'} and give "
                    f"it to {who}")
        return f"Move {_run(m.leg_id)} to {who}"
    if plan.kind == "swap_chain" and shape["reassigns"]:
        m = shape["reassigns"][-1]
        who = _first_name(_driver_name(board, m.to_driver_id))
        n = len(plan.moves)
        return (f"Shuffle {n} run{'s' if n != 1 else ''} so {_run(plan.target_leg_id)} "
                f"lands on {who}")
    if plan.kind == "farm_out" and shape["farms"]:
        m = shape["farms"][0]
        return f"Send {_run(m.leg_id)} to {m.to_label or 'an affiliate'}"
    if plan.kind == "evict_and_farm" and shape["farms"] and shape["reassigns"]:
        f, r = shape["farms"][0], shape["reassigns"][-1]
        who = _first_name(_driver_name(board, r.to_driver_id))
        return (f"Send {_run(f.leg_id)} to {f.to_label or 'an affiliate'} so "
                f"{who} can take {_run(r.leg_id)}")
    return plan.title


def _plan_outcome(board, d, plan, after):
    """One sentence on what the board looks like when this is done — read off
    the picture above it, so the words and the drawing can't disagree."""
    if plan.kind == "monitor":
        return ("Nothing downstream breaks at the current times. Worth watching, "
                "not worth moving.")
    if after is None:
        return ""
    bits = []
    for lane in after["lanes"]:
        short = _first_name(lane["driver"])
        if lane["role"] == "from" and lane.get("cleared"):
            kept = [b for b in lane["blocks"] if b["type"] in ("keep", "other")]
            if len(kept) == 1:
                bits.append(f"{short} keeps the {_short_route(kept[0]['label'])} run")
            elif kept:
                bits.append(f"{short} keeps the rest of the day")
            else:
                bits.append(f"{short} is clear")
        elif lane["role"] in ("to", "affiliate"):
            moved = next((b for b in lane["blocks"] if b["type"] == "moved"), None)
            if moved is None:
                continue
            who = short or lane["driver"]
            # Only gaps AFTER the landed run describe what follows it. Quoting
            # the one before it would report free time the driver has already
            # spent, attached to a run that does not exist.
            after = [g for g in lane["gaps"] if g.get("side") == "after"]
            tight = next((g for g in after if g["kind"] == "tight"), None)
            room = next((g for g in after
                         if g["kind"] == "room" and g["minutes"] is not None),
                        None)
            if lane["role"] == "affiliate":
                bits.append(f"{who} covers the {moved['time']} pickup")
            elif tight is not None and tight["slack_min"] < 0:
                bits.append(f"{who} covers the {moved['time']} pickup but "
                            f"lands {duration_words(tight['slack_min'])} late "
                            f"for the next one")
            elif tight is not None:
                bits.append(f"{who} covers the {moved['time']} pickup, but is "
                            f"left only {duration_words(tight['minutes'])} "
                            f"before the next one")
            elif room is not None:
                bits.append(f"{who} covers the {moved['time']} pickup with "
                            f"{duration_words(room['minutes'])} free before "
                            f"{who}'s next run")
            else:
                bits.append(f"{who} covers the {moved['time']} pickup")
    return ". ".join(bits) + "." if bits else ""


def _plan_warnings(board, d, plan, after):
    """The engine's risks, said the way a dispatcher needs to hear them.

    Every risk the engine raised comes through — recognised ones are rewritten,
    anything unrecognised passes VERBATIM rather than being dropped. Losing a
    safety line to a presentation layer is not an acceptable trade.
    """
    out, handled = [], set()
    flags = set(getattr(plan, "risk_flags", []) or [])
    when = day_word(board.target_date, board.now_local.date())

    # Tight turns manufactured by this move. EVERY worsened pair the engine
    # named gets its own chip — reporting only the first hid the worst turn on
    # a multi-driver plan, on a driver the chip never even named.
    v = getattr(plan, "validation", None)
    named = []
    for w in (getattr(v, "worsened_pairs", []) or []):
        named.append((_driver_name(board, w["driver_id"]), w["slack"]))
    if not named and after is not None:
        for lane in after["lanes"]:
            for g in lane["gaps"]:
                if g["kind"] == "tight":
                    named.append((lane["driver"], g.get("slack_min",
                                                        g["minutes"])))
    for who, mins in named:
        out.append({"tone": "red",
                    "text": f"Tight turn created: only {duration_words(mins)} "
                            f"between drop-off and {_first_name(who)}'s next "
                            f"pickup."})
    if named:
        handled.add("worsened")

    # The engine also flags a plan that merely DEPENDS on an already-tight turn
    # (min_buffer_after across all affected drivers). That turn need not be
    # adjacent to anything we drew, so it gets said in words or not at all —
    # marking it handled without a chip is how the line disappeared entirely.
    if "depends_tight_turn" in flags:
        mb = getattr(v, "min_buffer_after", None)
        if mb is not None and not _is_sentinel(mb):
            out.append({"tone": "red",
                        "text": f"After this move the tightest turn on the day "
                                f"is {duration_words(mb)} — there is no room "
                                f"left anywhere in it."})
            handled.add("depends_tight_turn")

    # Availability that is a guess rather than a shift.
    for m in plan.moves:
        if m.op != "reassign" or not m.to_driver_id:
            continue
        if not _window_is_guess(board, m.to_driver_id):
            continue
        who = _first_name(_driver_name(board, m.to_driver_id))
        text = (f"Check with {who} first — no set shift {when}; this window is "
                f"a guess from when {who} usually works.")
        if not any(o["text"] == text for o in out):
            out.append({"tone": "amber", "text": text})
        handled.add("stub_window")

    for m in plan.moves:
        if m.op == "farm_out":
            aff = m.to_label or "the affiliate"
            out.append({"tone": "amber",
                        "text": f"This only puts it on the board — call {aff} "
                                f"to confirm before you count it as covered."})
            out.append({"tone": "amber",
                        "text": f"No live tracking once it's farmed — {aff} "
                                f"runs outside our fleet."})
            handled.update({"farm_confirm", "gps_blind_affiliate"})
            break

    # Per-move chips mirror the engine's OWN gates exactly. A pure pickup-time
    # change never raises these, so neither may we: a red chip the engine did
    # not raise, sitting above an empty "engine risks" list, is the panel
    # inventing a hazard.
    seen_refund = seen_keoi = False
    for m in plan.moves:
        leg = (board.legs_by_id or {}).get(m.leg_id)
        status = (getattr(leg, "status", "") or "") if leg is not None else ""
        assign_move = m.op in ("reassign", "farm_out", "unassign")
        if assign_move and status in ("on-the-way", "confirmed"):
            who = _first_name(m.from_label) or "the driver"
            said = ("is already on the way to it" if status == "on-the-way"
                    else "has already confirmed it")
            out.append({"tone": "amber",
                        "text": f"{who} {said} — moving this run resets it and "
                                f"the new driver has to accept it again."})
            handled.add("status_move")
        if (assign_move and not seen_refund
                and m.leg_id in (board.pending_refund_leg_ids or set())):
            seen_refund = True
            out.append({"tone": "red",
                        "text": "This booking has a refund in flight — check "
                                "it before you move anything."})
            handled.add("pending_refund")
        if not seen_keoi and m.leg_id in (board.keoi_leg_ids or set()):
            seen_keoi = True
            out.append({"tone": "amber",
                        "text": "This trip is flagged keep-an-eye-on-it."})
            handled.add("keoi_flagged")

    if "far_unknown_route" in flags:
        out.append({"tone": "amber",
                    "text": "This run goes outside our usual area, so the drive "
                            "time behind it is a rough estimate."})
        handled.add("far_unknown_route")

    if plan.kind == "takeback":
        out.append({"tone": "red",
                    "text": "Call the affiliate first — you can't reliably pull "
                            "a run back the same day once they have it."})
        handled.add("takeback")

    # Anything the engine said that we did not recognise still gets shown.
    for raw in (plan.risks or []):
        if _risk_is_handled(raw, handled):
            continue
        out.append({"tone": "amber", "text": raw})
    return out


_RISK_MARKERS = (
    ("Creates a tight turn on", "worsened"),
    ("Leaves only", "depends_tight_turn"),
    ("observed-history window", "stub_window"),
    ("Assigns on the board only", "farm_confirm"),
    ("No live GPS once farmed", "gps_blind_affiliate"),
    ("pending refund request", "pending_refund"),
    ("keep-an-eye-on-it", "keoi_flagged"),
    ("far/unknown endpoint", "far_unknown_route"),
    ("moving it resets the status", "status_move"),
    ("you can't reliably pull back same-day", "takeback"),
)


def _risk_is_handled(raw, handled):
    for marker, flag in _RISK_MARKERS:
        if marker in raw:
            return flag in handled
    return False


def _math(board, d, plan, rank):
    """The engine's own output, verbatim, for the dispatcher who wants to check
    the working — plus the raw numbers behind the picture."""
    facts = []
    slack = d.details.get("slack")
    if slack is not None:
        facts.append(f"detection slack: {slack} min")
    if d.details.get("planning_slack") is not None:
        facts.append(f"planning slack: {d.details['planning_slack']} min")
    for m in plan.moves:
        if m.resulting_slack_min is None:
            continue
        who = _driver_name(board, m.to_driver_id) if m.to_driver_id else m.to_label
        # check_feasibility returns 999 for "no adjacent job on that side" and
        # -999 for a window/cap rejection — sentinels, not minutes. Printing
        # them as minutes is how a dispatcher learns to distrust the panel.
        if _is_sentinel(m.resulting_slack_min):
            facts.append(f"{who or 'receiver'} spare after move: unbounded "
                         f"(no adjacent job)")
        else:
            facts.append(f"{who or 'receiver'} spare after move: "
                         f"{m.resulting_slack_min} min (engine feasibility)")
    v = getattr(plan, "validation", None)
    if v is not None:
        mb = getattr(v, "min_buffer_after", None)
        if mb is not None and not _is_sentinel(mb):
            facts.append(f"tightest turn after move: {mb} min")
        for w in getattr(v, "worsened_pairs", []) or []:
            facts.append(f"turn {w['prev_leg_id']}→{w['next_leg_id']} on driver "
                         f"{w['driver_id']}: {w['before'] or 'clean'} → "
                         f"{w['after']} ({w['slack']} min)")
    for m in plan.moves:
        if m.op == "reassign" and m.to_driver_id and \
                _window_is_guess(board, m.to_driver_id):
            facts.append(f"availability source for "
                         f"{_driver_name(board, m.to_driver_id)}: observed "
                         f"history (provisional)")
    facts.append(f"issue class: {d.basis or 'n/a'}")
    facts.append(f"plan: {plan.kind} (tier {plan.tier}, rank #{rank}, "
                 f"score {plan.score})")
    if plan.price_impact is not None:
        facts.append(f"farm price: ${plan.price_impact}")
    return {
        "title": plan.title,
        "why": list(plan.why),
        "risks": list(plan.risks),
        "narrative": d.narrative,
        "facts": facts,
    }


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC: the same drawing, for the swap tester
# ════════════════════════════════════════════════════════════════════════════

def swap_board(schedules, legs_by_id, target_date, now_local=None):
    """A BoardState for callers that already hold schedules and legs.

    The swap tester assembles its own board (``build_driver_schedules`` over the
    day's legs) and has no Disruption. Rather than grow a second timeline
    renderer for it, we wrap what it already has in the engine's own dataclass
    so the SAME builder draws both surfaces. Build this ONCE and reuse it across
    solutions — the planning-clock sweep caches itself onto the board.
    """
    from django.utils import timezone as _tz
    from dispatching.conflict_advisor import BoardState

    now = _tz.now()
    local = now_local or _tz.localtime(now).replace(tzinfo=None)
    return BoardState(
        target_date=target_date, now=now, now_local=local,
        legs=list(legs_by_id.values()), legs_by_id=dict(legs_by_id),
        schedules=schedules)


def swap_timeline(board, moves, target_leg_id):
    """The board AFTER one swap solution, as driver lanes.

    ``moves`` is the swap optimiser's own move list — anything with
    ``leg_id`` / ``from_driver_id`` / ``to_driver_id`` (SwapMove objects or the
    dicts the endpoint serialises). Returns the same ``after`` structure a
    recovery plan carries, so the swap tester and the advisor draw identically.
    """
    from dispatching.conflict_advisor import CandidatePlan, Disruption, PlanMove

    plan_moves = []
    for m in moves:
        get = (lambda k: m.get(k)) if isinstance(m, dict) else (
            lambda k: getattr(m, k, None))
        leg = (board.legs_by_id or {}).get(get("leg_id"))
        plan_moves.append(PlanMove(
            leg_id=get("leg_id"), op="reassign",
            from_driver_id=get("from_driver_id"),
            to_driver_id=get("to_driver_id"),
            summary=(plain_route(leg) if leg is not None else ""),
            from_label=_driver_name(board, get("from_driver_id"))
                       if get("from_driver_id") else "",
            to_label=_driver_name(board, get("to_driver_id"))
                     if get("to_driver_id") else "",
            resulting_slack_min=get("buffer_minutes")))
    if not plan_moves:
        return None
    plan = CandidatePlan(kind="swap_chain", tier=2, title="",
                         target_leg_id=target_leg_id, moves=plan_moves)
    # leg_ids stays EMPTY on purpose: on the swap tester nothing was in
    # conflict, so no lane may be tagged "conflict cleared". The donor's ghost
    # block already says the run left — which is the true statement here.
    d = Disruption(id=f"swap:{target_leg_id}", kind="overlap",
                   severity="critical", headline="", narrative="", basis="",
                   leg_ids=[], anchor_leg_id=target_leg_id)
    return _after_timeline(board, d, plan)


def plan_display(board, d, plan, rank):
    """The dispatcher-facing view of one recovery plan."""
    after = _after_timeline(board, d, plan)
    price = None
    if plan.price_impact is not None:
        from dispatching.conflict_advisor import _fmt_money
        price = _fmt_money(plan.price_impact)
    return {
        "headline": _plan_headline(board, d, plan),
        "price_label": price,
        "after": after,
        "outcome": _plan_outcome(board, d, plan, after),
        "warnings": _plan_warnings(board, d, plan, after),
        "math": _math(board, d, plan, rank),
        "action_label": ("Apply this move" if plan.moves else "Nothing to apply"),
    }
