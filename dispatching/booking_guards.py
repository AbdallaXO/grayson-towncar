"""
Sanity guards for the dispatcher booking wizard (trip-legs step).

Required-field validation can't catch a pickup that is *valid but wrong* —
a fat-fingered date, a flipped AM/PM, legs typed out of order. These checks
flag those cases as WARNINGS the dispatcher must explicitly acknowledge
before moving on. They never hard-block: every flagged situation can be
legitimate (a 3 AM shuttle, a same-day booking), so the dispatcher always
gets a "yes, this is correct" path.

The strongest check compares the pickup against the attached flight's
published schedule via AeroAPI — /schedules/ covers dates up to a year out,
so advance bookings are verified at entry time, not just at T-2 days when
the live refresh cycle picks the flight up.

Each warning dict: {code, leg (1-based or None), severity, message}.
Severities:
  'error'   — hard block, no acknowledge-and-proceed path (business rule,
              e.g. a Publix stop while the store is closed)
  'warning' — requires acknowledgment before the step can proceed
  'info'    — shown but non-blocking (e.g. flight check unavailable)
  'ok'      — positive confirmation (flight verified), non-blocking
"""
import hashlib
import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone as django_timezone

from .overnight_arrival import OVERNIGHT_END_HOUR

logger = logging.getLogger(__name__)

# Master switches (same convention as scheduler.py / feasibility_guards.py flags)
SANITY_CHECKS_ENABLED = True
FLIGHT_CHECK_ENABLED = True

# 00:00–04:59 pickups with no flight backing them up are the classic AM/PM flip.
# Boundary 5 (not 3 like NIGHT_LEG_BOUNDARY_HOUR): this is a "look twice" prompt,
# not a scheduling rule, and 3–5 AM typos are as common as 12–3 AM ones.
EARLY_MORNING_END_HOUR = 5
# Departure runs (hotel → airport) from this hour on are ROUTINE — the fleet
# does them every morning for early flights out. Founder 2026-07-02: don't
# make the desk "double-check with the customer" for those; a quick
# "it's AM, not PM" tick is enough. Before this hour a departure run is still
# suspicious (almost nothing takes off that needs a 1 AM hotel pickup).
EARLY_DEPARTURE_ROUTINE_START_HOUR = 3
FAR_FUTURE_DAYS = 365          # beyond this → probable year typo
FLIGHT_CHECK_MAX_DAYS = 360    # /schedules/ only covers ~1 year; skip beyond

# Arrival legs: pickup should sit shortly after the flight lands.
ARRIVAL_EARLY_TOLERANCE_MIN = 45     # pickup earlier than landing−45m → flag
ARRIVAL_LATE_TOLERANCE_HOURS = 4     # pickup later than landing+4h → flag
# Departure legs: pickup should sit a sane lead ahead of takeoff.
DEPARTURE_MIN_LEAD_MIN = 60          # under 1h before takeoff → too tight
DEPARTURE_MAX_LEAD_HOURS = 8         # over 8h before takeoff → probable AM/PM flip

FLIGHT_CACHE_TTL = 900  # seconds; soft-confirm re-submits shouldn't re-bill AeroAPI

# ── Publix grocery-stop hours ──────────────────────────────────────────────
# The store (Lake Cay Commons, open 7 AM-10 PM) can't be visited when it's
# closed. Founder rule: no grocery stop for pickups from 9 PM through 5:59 AM
# — a 9 PM+ pickup reaches the store at close, a pre-6 AM one before open.
# Unlike the sanity checks above, this is a hard business rule (severity
# 'error'): there is no acknowledge-and-proceed path.
PUBLIX_CLOSED_START_HOUR = 21  # 9:00 PM
PUBLIX_CLOSED_END_HOUR = 6     # closed until 5:59 AM; 6:00 AM pickups are OK

EASTERN = ZoneInfo("America/New_York")


def _parse_date(value) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _parse_time(value) -> Optional[time]:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
    return None


def _fmt_dt(dt: datetime) -> str:
    """'Sat, Aug 15 at 5:05 PM' — readable in a warning message."""
    return f"{dt.strftime('%a, %b')} {dt.day} at {_fmt_time(dt)}"


def _fmt_time(dt) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _fmt_date(d: date) -> str:
    return f"{d.strftime('%A, %b')} {d.day}"


def _fmt_date_short(d: date) -> str:
    """'Wed, Jul 2' — compact form for the overnight flight-path card."""
    return f"{d.strftime('%a, %b')} {d.day}"


def _overnight_visual(*, pickup_date: date, land_time_str: str,
                      takeoff_date: date, takeoff_time_str: str,
                      origin: str, dest: str, verified: bool,
                      footnote: str = "") -> Dict[str, Any]:
    """Structured payload for the wizard's overnight flight-path card (mirrors
    the guest booking form's takeoff → past-midnight → landing visual). All
    values are display-ready strings so the template stays dumb."""
    return {
        "kind": "overnight",
        "takeoff_date": _fmt_date_short(takeoff_date),
        "takeoff_time": takeoff_time_str,
        "origin": origin,
        "land_date": _fmt_date_short(pickup_date),
        "land_time": land_time_str,
        "dest": dest,
        "verified": verified,
        "wrong_depart_date": _fmt_date_short(pickup_date),
        "wrong_land_date": _fmt_date_short(pickup_date + timedelta(days=1)),
        "footnote": footnote,
    }


def _naive_eastern(dt: Optional[datetime]) -> Optional[datetime]:
    """AeroAPI times come back Eastern-aware; pickups are naive Eastern."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(EASTERN).replace(tzinfo=None)
    return dt


def build_flight_ident(airline: str, flight_number: str) -> Optional[str]:
    """
    Build an AeroAPI ident ('DAL1691') from the wizard's free-text airline +
    flight number, mirroring Flight.get_flight_ident() so booking-time checks
    query the exact ident the refresh cycle will use later.
    """
    airline = (airline or "").strip()
    number = (flight_number or "").strip()
    if not number:
        return None

    from reservations.utils import (
        get_flightaware_code,
        normalize_airline,
        normalize_flight_number,
    )

    digits = normalize_flight_number(number)
    if airline and digits:
        iata = normalize_airline(airline)
        # Same guard as Flight.get_flight_ident: an unrecognized airline comes
        # back as the bare uppercased string — only 2-3 alnum chars is a code.
        if iata and len(iata) <= 3 and iata.isalnum():
            return f"{get_flightaware_code(iata)}{digits}"

    # No usable airline — accept a prefixed flight number like 'DL567' / 'B6 351'.
    # Prefix = 2-char IATA (alphanumeric with a letter in it, e.g. B6, 9E) or
    # 3-letter ICAO (JBU); the lookahead rejects all-digit "prefixes".
    match = re.match(
        r"^((?=[0-9]?[A-Za-z])[A-Za-z0-9]{2}|[A-Za-z]{3})\s*-?\s*(\d{1,4})[A-Za-z]?$",
        number,
    )
    if match:
        return f"{get_flightaware_code(match.group(1).upper())}{match.group(2)}"
    return None


def _looks_like_departure_run(flight_type: str, leg: Dict) -> bool:
    """True when a leg is (or strongly looks like) a hotel → airport departure
    run: explicit departure flight type wins, else infer from the locations
    using the shared airport detector."""
    if flight_type == "departure":
        return True
    if flight_type == "arrival":
        return False
    pickup_loc = (leg.get("pickup_location") or "").lower()
    dropoff_loc = (leg.get("dropoff_location") or "").lower()
    if not dropoff_loc:
        return False
    try:
        from .analytics import is_airport_location
        return is_airport_location(dropoff_loc) and not is_airport_location(pickup_loc)
    except Exception:
        return False


def _fetch_flight(aeroapi, ident: str, flight_date: date, trip_type: Optional[str]) -> Dict[str, Any]:
    cache_key = f"bkguard_flight_{ident}_{flight_date.isoformat()}_{trip_type or 'any'}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    result = aeroapi.get_flight_data(
        ident, flight_date=flight_date.isoformat(), trip_type=trip_type
    )
    # Cache errors briefly too — a soft-confirm re-submit lands seconds later
    cache.set(cache_key, result, FLIGHT_CACHE_TTL)
    return result


def _check_flight(aeroapi, ident: str, trip_type: Optional[str],
                  pickup_date: date, pickup_dt: Optional[datetime],
                  leg_no: int) -> List[Dict[str, Any]]:
    """
    Compare one leg's pickup date/time against the flight's published schedule.
    Returns warning dicts; empty list means nothing to compare (never = verified).
    """
    result = _fetch_flight(aeroapi, ident, pickup_date, trip_type)
    status = result.get("status")

    if status in ("error", "rate_limited"):
        return [{
            "code": "flight_unverified", "leg": leg_no, "severity": "info",
            "message": (
                f"Leg {leg_no}: couldn't automatically verify flight {ident} "
                f"(flight data service unavailable) — please double-check the "
                f"date and time manually."
            ),
        }]

    if status == "not_orlando":
        return [{
            "code": "flight_not_orlando", "leg": leg_no, "severity": "warning",
            "message": f"Leg {leg_no}: {result.get('error')}",
        }]

    if status == "not_found":
        return [{
            "code": "flight_not_found", "leg": leg_no, "severity": "warning",
            "message": (
                f"Leg {leg_no}: no flight {ident} found for "
                f"{_fmt_date(pickup_date)} — the pickup date or the flight "
                f"number may be wrong."
            ),
        }]

    if status != "success":
        return []

    # Resolve direction: explicit trip_type wins, else infer from which end
    # of the route is Orlando.
    from .aeroapi_service import ORLANDO_AIRPORT_CODES
    direction = trip_type
    if direction not in ("arrival", "return"):
        if result.get("destination") in ORLANDO_AIRPORT_CODES:
            direction = "arrival"
        elif result.get("origin") in ORLANDO_AIRPORT_CODES:
            direction = "return"
        else:
            return []

    label = result.get("flight_iata") or ident

    if direction == "arrival":
        flight_dt = _naive_eastern(
            result.get("scheduled_gate_arrival_local") or result.get("scheduled_arrival_local")
        )
        if flight_dt is None:
            return []

        vouching = result  # whichever fetch produced flight_dt (for depart info)
        if flight_dt.date() != pickup_date:
            # After-midnight landings: the schedule lookup keys on departure
            # date, so a late-evening departure that lands after midnight on
            # the pickup date shows up under the PREVIOUS day. Retry once
            # before declaring a mismatch.
            retry = _fetch_flight(aeroapi, ident, pickup_date - timedelta(days=1), trip_type)
            retry_dt = _naive_eastern(
                retry.get("scheduled_gate_arrival_local") or retry.get("scheduled_arrival_local")
            ) if retry.get("status") == "success" else None
            if retry_dt is not None and retry_dt.date() == pickup_date:
                flight_dt = retry_dt
                vouching = retry
            else:
                return [{
                    "code": "flight_date_mismatch", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: flight {label} lands in Orlando "
                        f"{_fmt_dt(flight_dt)}, but this pickup is dated "
                        f"{_fmt_date(pickup_date)} — wrong date? For an "
                        f"after-midnight landing, the pickup date must be the "
                        f"LANDING date (the flight departs the evening before)."
                    ),
                }]

        if pickup_dt is None:
            return []
        if pickup_dt < flight_dt - timedelta(minutes=ARRIVAL_EARLY_TOLERANCE_MIN):
            return [{
                "code": "flight_time_mismatch", "leg": leg_no, "severity": "warning",
                "message": (
                    f"Leg {leg_no}: pickup is set for {_fmt_time(pickup_dt)}, but "
                    f"flight {label} doesn't land until {_fmt_time(flight_dt)} — "
                    f"possible AM/PM mix-up?"
                ),
            }]
        if pickup_dt > flight_dt + timedelta(hours=ARRIVAL_LATE_TOLERANCE_HOURS):
            gap_h = (pickup_dt - flight_dt).total_seconds() / 3600
            return [{
                "code": "flight_time_mismatch", "leg": leg_no, "severity": "warning",
                "message": (
                    f"Leg {leg_no}: pickup at {_fmt_time(pickup_dt)} is "
                    f"{gap_h:.1f}h after flight {label} lands "
                    f"({_fmt_time(flight_dt)}) — possible AM/PM mix-up?"
                ),
            }]
        items = [{
            "code": "flight_verified", "leg": leg_no, "severity": "ok",
            "message": (
                f"Leg {leg_no}: flight {label} verified — lands "
                f"{_fmt_dt(flight_dt)}, pickup {_fmt_time(pickup_dt)}."
            ),
        }]
        # After-midnight landing: verification proves the flight EXISTS, not
        # which night the guest is on — the same number lands every night.
        # Spell out the departs-the-day-before rule with the flight's real
        # schedule and make the dispatcher acknowledge it (founder wording).
        if flight_dt.hour < OVERNIGHT_END_HOUR:
            dep_dt = _naive_eastern(vouching.get("scheduled_departure_local"))
            origin = (vouching.get("origin") or "").strip() or "origin"
            dest = (vouching.get("destination") or "").strip() or "MCO"
            dep_date_for_msg = dep_dt.date() if dep_dt is not None else pickup_date - timedelta(days=1)
            if dep_dt is not None:
                journey = (
                    f"departs {origin} at {_fmt_time(dep_dt)} on "
                    f"{_fmt_date(dep_dt.date())} (the day BEFORE) → lands {dest} "
                    f"at {_fmt_time(flight_dt)} on {_fmt_date(flight_dt.date())}"
                )
            else:
                journey = (
                    f"takes off the evening of {_fmt_date(dep_date_for_msg)} "
                    f"(the day BEFORE) → lands {dest} at {_fmt_time(flight_dt)} "
                    f"on {_fmt_date(flight_dt.date())}"
                )
            items.append({
                "code": "overnight_arrival", "leg": leg_no, "severity": "warning",
                "message": (
                    f"Leg {leg_no}: OVERNIGHT arrival — {label} {journey}. "
                    f"Confirm the guest's ticket shows the "
                    f"{_fmt_date(dep_date_for_msg)} departure: if their ticket "
                    f"departs {_fmt_date(pickup_date)} instead, they land "
                    f"{_fmt_date(pickup_date + timedelta(days=1))} and this "
                    f"pickup is a day early."
                ),
                "visual": _overnight_visual(
                    pickup_date=pickup_date,
                    land_time_str=_fmt_time(flight_dt),
                    takeoff_date=dep_date_for_msg,
                    takeoff_time_str=_fmt_time(dep_dt) if dep_dt is not None else "evening",
                    origin=origin,
                    dest=dest,
                    verified=True,
                ),
            })
        return items

    # Departure from Orlando
    dep_dt = _naive_eastern(result.get("scheduled_departure_local"))
    if dep_dt is None:
        return []

    if dep_dt.date() != pickup_date:
        return [{
            "code": "flight_date_mismatch", "leg": leg_no, "severity": "warning",
            "message": (
                f"Leg {leg_no}: flight {label} departs Orlando "
                f"{_fmt_dt(dep_dt)}, but this pickup is dated "
                f"{_fmt_date(pickup_date)} — wrong date?"
            ),
        }]

    if pickup_dt is None:
        return []
    if pickup_dt >= dep_dt:
        return [{
            "code": "flight_time_mismatch", "leg": leg_no, "severity": "warning",
            "message": (
                f"Leg {leg_no}: pickup at {_fmt_time(pickup_dt)} is AFTER flight "
                f"{label} departs ({_fmt_time(dep_dt)}) — the customer would "
                f"miss the flight."
            ),
        }]
    lead = dep_dt - pickup_dt
    if lead < timedelta(minutes=DEPARTURE_MIN_LEAD_MIN):
        return [{
            "code": "flight_time_mismatch", "leg": leg_no, "severity": "warning",
            "message": (
                f"Leg {leg_no}: pickup at {_fmt_time(pickup_dt)} is only "
                f"{int(lead.total_seconds() // 60)} min before flight {label} "
                f"departs ({_fmt_time(dep_dt)}) — extremely tight."
            ),
        }]
    if lead > timedelta(hours=DEPARTURE_MAX_LEAD_HOURS):
        return [{
            "code": "flight_time_mismatch", "leg": leg_no, "severity": "warning",
            "message": (
                f"Leg {leg_no}: pickup at {_fmt_time(pickup_dt)} is "
                f"{lead.total_seconds() / 3600:.1f}h before flight {label} "
                f"departs ({_fmt_time(dep_dt)}) — possible AM/PM mix-up?"
            ),
        }]
    return [{
        "code": "flight_verified", "leg": leg_no, "severity": "ok",
        "message": (
            f"Leg {leg_no}: flight {label} verified — departs "
            f"{_fmt_dt(dep_dt)}, pickup {_fmt_time(pickup_dt)}."
        ),
    }]


def run_leg_sanity_checks(legs_data: List[Dict], flights_data: List[Dict],
                          check_flights: bool = True) -> List[Dict[str, Any]]:
    """
    Run all plausibility checks over the wizard's legs/flights payload
    (session format: stringified dates/times; index i of flights_data pairs
    with index i of legs_data, {} = no flight). Returns warning dicts.
    """
    if not SANITY_CHECKS_ENABLED:
        return []

    warnings: List[Dict[str, Any]] = []
    today = django_timezone.localdate()
    now = django_timezone.localtime().replace(tzinfo=None)

    aeroapi = None
    flight_api_down = False
    parsed = []  # (leg_no, pickup_date, pickup_dt)

    for i, leg in enumerate(legs_data):
        leg_no = i + 1
        pickup_date = _parse_date(leg.get("pickup_date"))
        pickup_time = _parse_time(leg.get("pickup_time"))
        pickup_dt = datetime.combine(pickup_date, pickup_time) if pickup_date and pickup_time else None
        parsed.append((leg_no, pickup_date, pickup_dt))
        if pickup_date is None:
            continue

        # -- Date plausibility -------------------------------------------------
        days_out = (pickup_date - today).days
        if days_out > FAR_FUTURE_DAYS:
            warnings.append({
                "code": "far_future", "leg": leg_no, "severity": "warning",
                "message": (
                    f"Leg {leg_no}: pickup {_fmt_date(pickup_date)}, "
                    f"{pickup_date.year} is more than a year away "
                    f"({days_out} days) — double-check the year."
                ),
            })
        elif pickup_date == today:
            if pickup_dt is not None and pickup_dt < now:
                warnings.append({
                    "code": "today_past", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: pickup is TODAY at {_fmt_time(pickup_dt)} "
                        f"— that time has already passed."
                    ),
                })
            else:
                hrs = ((pickup_dt - now).total_seconds() / 3600) if pickup_dt else None
                when = f" at {_fmt_time(pickup_dt)} ({hrs:.1f}h from now)" if hrs is not None else ""
                warnings.append({
                    "code": "today", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: pickup is TODAY{when} — confirm this "
                        f"is a same-day booking, not a wrong date."
                    ),
                })

        # -- Flight cross-check ------------------------------------------------
        flight = flights_data[i] if i < len(flights_data) and flights_data[i] else {}
        ident = build_flight_ident(flight.get("airline"), flight.get("flight_number"))
        flight_checked = False
        if (
            check_flights and FLIGHT_CHECK_ENABLED and ident
            and not flight_api_down
            and today <= pickup_date <= today + timedelta(days=FLIGHT_CHECK_MAX_DAYS)
        ):
            if aeroapi is None:
                from .aeroapi_service import AeroAPIService
                aeroapi = AeroAPIService()
            trip_type = {"arrival": "arrival", "departure": "return"}.get(
                (flight.get("flight_type") or "").strip().lower()
            )
            try:
                flight_warnings = _check_flight(
                    aeroapi, ident, trip_type, pickup_date, pickup_dt, leg_no
                )
            except Exception:
                logger.exception("Flight sanity check failed for %s", ident)
                flight_warnings = []
            warnings.extend(flight_warnings)
            flight_checked = any(w["severity"] in ("ok", "warning") for w in flight_warnings)
            # One unavailable response = don't burn timeouts on remaining legs
            if any(w["code"] == "flight_unverified" for w in flight_warnings):
                flight_api_down = True

        # -- AM/PM plausibility (only when no flight vouches for the time) ------
        if (
            pickup_time is not None
            and pickup_time.hour < EARLY_MORNING_END_HOUR
            and not flight_checked
        ):
            flight_type = (flight.get("flight_type") or "").strip().lower()
            if (
                _looks_like_departure_run(flight_type, leg)
                and pickup_time.hour >= EARLY_DEPARTURE_ROUTINE_START_HOUR
            ):
                # Routine early departure run — light one-tick confirmation,
                # no "call the customer" implication.
                warnings.append({
                    "code": "early_morning_departure", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: {_fmt_time(pickup_dt)} pickup heading to "
                        f"the airport — normal for an early flight out. Quick "
                        f"check: that's {_fmt_time(pickup_dt)} in the morning, "
                        f"not {_fmt_time(pickup_dt + timedelta(hours=12))}."
                    ),
                })
            elif ident and flight_type != "departure":
                # After-midnight ARRIVAL with a flight attached: the bigger trap
                # than an AM/PM flip is the overnight date mix-up. Spell out the
                # departs-the-day-before rule with this booking's real dates
                # (founder wording 2026-07-02: "departs MIA 10:30 PM Jul 2 →
                # lands MCO 12:30 AM Jul 3 — I want it to be very clear").
                prev_day = pickup_date - timedelta(days=1)
                next_day = pickup_date + timedelta(days=1)
                warnings.append({
                    "code": "early_morning", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: {_fmt_time(pickup_dt)} on "
                        f"{_fmt_date(pickup_date)} is an OVERNIGHT arrival — "
                        f"flights landing after midnight take off the day BEFORE. "
                        f"Double-check the ticket: the flight should depart on "
                        f"{_fmt_date(prev_day)} and land after midnight — for "
                        f"example, departs MIA 10:30 PM {_fmt_date(prev_day)} → "
                        f"lands MCO {_fmt_time(pickup_dt)} {_fmt_date(pickup_date)}. "
                        f"If the ticket shows a {_fmt_date(pickup_date)} departure, "
                        f"the guest lands {_fmt_date(next_day)} and this pickup is "
                        f"a day early. Also confirm it isn't an AM/PM mix-up "
                        f"(meant to be {_fmt_time(pickup_dt + timedelta(hours=12))})."
                    ),
                    "visual": _overnight_visual(
                        pickup_date=pickup_date,
                        land_time_str=_fmt_time(pickup_dt),
                        takeoff_date=prev_day,
                        takeoff_time_str="evening",
                        origin="",
                        dest="MCO",
                        verified=False,
                        footnote=(
                            f"Also confirm it isn't an AM/PM mix-up — meant to be "
                            f"{_fmt_time(pickup_dt + timedelta(hours=12))}?"
                        ),
                    ),
                })
            else:
                warnings.append({
                    "code": "early_morning", "leg": leg_no, "severity": "warning",
                    "message": (
                        f"Leg {leg_no}: pickup at {_fmt_time(pickup_dt)} is between "
                        f"midnight and {EARLY_MORNING_END_HOUR}:00 AM — after-midnight "
                        f"times are the most common AM/PM mix-up. Confirm it isn't "
                        f"meant to be {_fmt_time((pickup_dt + timedelta(hours=12)))}."
                    ),
                })

    # -- Cross-leg chronological order ------------------------------------------
    known = [(no, dt) for no, _, dt in parsed if dt is not None]
    for (prev_no, prev_dt), (cur_no, cur_dt) in zip(known, known[1:]):
        if cur_dt < prev_dt:
            warnings.append({
                "code": "legs_out_of_order", "leg": cur_no, "severity": "warning",
                "message": (
                    f"Leg {cur_no} ({_fmt_dt(cur_dt)}) is EARLIER than "
                    f"Leg {prev_no} ({_fmt_dt(prev_dt)}) — legs are out of "
                    f"chronological order. Wrong date on one of them?"
                ),
            })
        elif cur_dt == prev_dt:
            warnings.append({
                "code": "legs_same_time", "leg": cur_no, "severity": "warning",
                "message": (
                    f"Leg {prev_no} and Leg {cur_no} have the exact same pickup "
                    f"date and time ({_fmt_dt(cur_dt)}) — is one of them wrong?"
                ),
            })

    return warnings


def publix_closed_at(pickup_time) -> bool:
    """True when a Publix grocery stop can't be honored for this pickup time
    (accepts a datetime.time or an 'HH:MM'/'HH:MM:SS' string)."""
    t = _parse_time(pickup_time)
    if t is None:
        return False
    return t.hour >= PUBLIX_CLOSED_START_HOUR or t.hour < PUBLIX_CLOSED_END_HOUR


def check_publix_store_stop(legs_data: List[Dict], store_stop: bool) -> List[Dict[str, Any]]:
    """
    Hard-stop check: a reservation with a Publix grocery stop whose stop-leg
    pickup falls inside the store's closed window (9 PM-6 AM).

    The grocery run happens on the leg that brings the guest INTO town — the
    first leg with an airport pickup — falling back to leg 1 when no leg
    starts at an airport. Returns a severity-'error' dict (hard block; the
    store is physically closed, so there is nothing to acknowledge) or [].
    """
    if not store_stop or not legs_data:
        return []

    from .analytics import is_airport_location

    stop_idx = 0
    for i, leg in enumerate(legs_data):
        if is_airport_location((leg.get("pickup_location") or "").lower()):
            stop_idx = i
            break

    pickup_time = _parse_time(legs_data[stop_idx].get("pickup_time"))
    if pickup_time is None or not publix_closed_at(pickup_time):
        return []

    return [{
        "code": "publix_closed", "leg": stop_idx + 1, "severity": "error",
        "message": (
            f"Leg {stop_idx + 1}: this reservation includes a Publix grocery stop, "
            f"but the {_fmt_time(pickup_time)} pickup is while the store is closed — "
            f"grocery stops are only possible for pickups between "
            f"{_fmt_time(time(PUBLIX_CLOSED_END_HOUR))} and "
            f"{_fmt_time(time(PUBLIX_CLOSED_START_HOUR))}. Remove the grocery stop "
            f"on the Details step, or fix the pickup time."
        ),
    }]


def warnings_token(warnings: List[Dict[str, Any]]) -> str:
    """
    Stable fingerprint of the blocking warnings. The acknowledgment checkbox
    carries this token back; if the dispatcher edits the form and different
    warnings result, the stale token no longer matches and the new warnings
    block again.
    """
    blocking = sorted(
        f"{w['code']}|{w.get('leg')}|{w['message']}"
        for w in warnings if w["severity"] == "warning"
    )
    return hashlib.sha1("\n".join(blocking).encode("utf-8")).hexdigest()[:16]
