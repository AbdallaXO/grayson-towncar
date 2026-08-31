"""
Standard client-facing text messages an IN-HOUSE chauffeur sends from the driver app.

Three moments in a trip, one standard wording for each:

    on_the_way   — the chauffeur has started toward the pickup
    on_location  — the chauffeur is standing at the meet point
    review       — sent in person, before the trip is marked complete

The wording ADAPTS to what kind of pickup it is. An airport arrival, a departure
to the airport, a cruise embarkation and a debarkation are four different guest
experiences, and a single generic "I'm on my way" text reads wrong in three of
them. `classify()` picks the situation; `build()` renders the copy.

    from drivers.client_messages import build, ON_THE_WAY
    msg = build(leg, ON_THE_WAY, driver_name="Marcus", vehicle=fleet_vehicle)
    msg.situation  -> "arrival_tracked"
    msg.body       -> "Hello, Jane! This is Marcus with Grayson Towncar. ..."

DESIGN RULES baked into the copy — do not "simplify" these away:

* Airport pickups are NEVER at the curb. Grayson's driver parks and walks in to
  the in-terminal commercial-lane meet point (dispatching/pickup_policy.py:40-47,
  founder rule reaffirmed 2026-07-31). The copy names the baggage-claim meet
  point (see _meet_point below), matching the confirmation SMS the guest
  already received (dispatching/confirmation_sms.py:326). Never write "curb",
  "outside" or "arrivals level" for an airport arrival.
* No vehicle COLOR is ever promised. rates.Vehicle and drivers.FleetVehicle both
  have no colour field (drivers/models.py:587-589), so "look for the black
  Suburban" would be invented. Make + model only.
* Never quote a departing flight TIME. Flight carries arrival datetimes only
  (reservations/models.py:2684-2704); its sole departure field is a DateField.
  A departure text quotes leg.pickup_time and nothing else.
* Private notes never appear. reservations.Leg.private_notes and
  Reservation.private_notes are dispatcher-only.
* The guest-facing copy never names a flight number or a landing time —
  deliberate, as of the 2026-08-31 rewrite. classify() still tracks
  ARRIVAL_TRACKED vs ARRIVAL_UNTRACKED (and other internal uses may want that
  distinction later), but the two currently render byte-identical text, and
  so does CRUISE_TO_PORT_AIR — a cruise guest arriving by air gets the exact
  same on-the-way/on-location wording as a plain airport arrival.
* The meet point named in an arrival text is airport-specific
  (_meet_point below) and ONLY as precise as verified instructions exist for
  that airport (MCO, SFB today). Never invent a floor or landmark for an
  airport without verified instructions — fall back to the plain "baggage
  claim area".

CLASSIFICATION does not trust Leg.get_trip_type() alone. That helper returns
'other' for an airport->airport transfer (reservations/models.py:1946-1955), which
would send lobby copy to a guest standing in a terminal, and its cruise keyword
list is narrower than the analytics one (missing "cape canaveral", "cove
terminal"), so a real port pickup can fall through to 'other'. We branch on the
specific predicates instead and union both keyword lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from business.datefmt import time12

# ── Message kinds ──────────────────────────────────────────────────────────
ON_THE_WAY = "on_the_way"
ON_LOCATION = "on_location"
REVIEW = "review"

KIND_CHOICES = [
    (ON_THE_WAY, "On the way"),
    (ON_LOCATION, "On location"),
    (REVIEW, "Review request"),
]
KINDS = [k for k, _ in KIND_CHOICES]

# ── Situations ─────────────────────────────────────────────────────────────
ARRIVAL_TRACKED = "arrival_tracked"
ARRIVAL_UNTRACKED = "arrival_untracked"
DEPARTURE = "departure"
CRUISE_TO_PORT_AIR = "cruise_to_port_air"
CRUISE_TO_PORT_LAND = "cruise_to_port_land"
CRUISE_FROM_PORT = "cruise_from_port"
CHARTER = "charter"
OTHER = "other"

SITUATION_CHOICES = [
    (ARRIVAL_TRACKED, "Airport arrival (flight tracked)"),
    (ARRIVAL_UNTRACKED, "Airport arrival (no flight time)"),
    (DEPARTURE, "Departure to the airport"),
    (CRUISE_TO_PORT_AIR, "Cruise embarkation (from the airport)"),
    (CRUISE_TO_PORT_LAND, "Cruise embarkation (from a hotel)"),
    (CRUISE_FROM_PORT, "Cruise debarkation"),
    (CHARTER, "Charter / hourly"),
    (OTHER, "Point to point"),
]

COMPANY = "Grayson Towncar"

#: The Google review destination. Consolidated here from four byte-identical
#: hardcoded copies that used to live in the driver templates.
REVIEW_URL = "https://g.page/r/CRWIXii71sLGEBM/review"

#: Situations where the guest is standing inside an airport terminal and the
#: chauffeur walks in to them. Drives the "baggage claim / name sign" wording.
_IN_TERMINAL = {ARRIVAL_TRACKED, ARRIVAL_UNTRACKED, CRUISE_TO_PORT_AIR}

#: Union of reservations.Leg.CRUISE_PORT_KEYWORDS and the wider list in
#: dispatching/analytics.py:95-99 (as of 2026-08-31 — the three lists are
#: independent and can drift again; there is no public function to delegate
#: to the way _is_airport() delegates to is_airport_location(), since
#: analytics.py's list is private and used only to EXCLUDE hotels from
#: airport classification, not to answer "is this a port" on its own). A leg
#: written as "Cove Terminal, Cape Canaveral" matches only the analytics
#: one — it is neither 'cruise' nor an airport to get_trip_type(), so it
#: lands in 'other' and would otherwise get lobby copy for a cruise-terminal
#: pickup. "cocoa beach"/"cocoa"/"brevard" (analytics-only until now) are
#: included for the same reason.
_PORT_KEYWORDS = (
    "port canaveral", "canaveral", "cape canaveral", "cruise port",
    "cruise terminal", "cruise termina", "cruise ship", "cove terminal",
    "jetty park", "cocoa beach", "cocoa", "brevard",
)


@dataclass(frozen=True)
class Message:
    """One rendered message, ready for an sms: composer."""

    kind: str
    situation: str
    body: str

    @property
    def label(self) -> str:
        return dict(KIND_CHOICES).get(self.kind, self.kind)


# ── Small helpers ──────────────────────────────────────────────────────────

def _is_port(text: str) -> bool:
    low = (text or "").lower()
    return any(kw in low for kw in _PORT_KEYWORDS)


def _is_airport(text: str) -> bool:
    """Terminal-level airport test. Delegates to the single source of truth so
    airport-area hotels and cruise ports stay excluded."""
    from dispatching.analytics import is_airport_location

    return is_airport_location(text or "")


def _first_name(leg) -> str:
    cust = getattr(getattr(leg, "reservation", None), "customer", None)
    name = (getattr(cust, "first_name", "") or "").strip()
    return name.title() if name else "there"


def _pickup_time(leg) -> str:
    """'6:15 AM'. Uses the cross-platform formatter — %-I raises on Windows."""
    return time12(getattr(leg, "pickup_time", None)) or ""


def _vehicle_clause(vehicle) -> str:
    """' in a Chevrolet Suburban', or '' when no physical car is known.

    `vehicle` is a drivers.FleetVehicle (from the day's DriverVehicleAssignment).
    Never mentions colour — no colour field exists on any vehicle model.
    """
    if vehicle is None:
        return ""
    make = (getattr(vehicle, "make", "") or "").strip()
    model = (getattr(vehicle, "model", "") or "").strip()
    desc = " ".join(p for p in (make, model) if p)
    return f" in a {desc}" if desc else ""


def _airport_name(text: str) -> str:
    """'Orlando International Airport (MCO)' — reuses the confirmation-SMS map."""
    from dispatching.confirmation_sms import _detect_airport

    return _detect_airport(text or "") or "the airport"


def _airport_name_plain(text: str) -> str:
    """'Orlando International Airport' — _airport_name without the internal
    (MCO) shorthand. Guest-facing departure copy names the airport itself,
    not our internal code for it."""
    return _airport_name(text).split(" (")[0]


#: Verified meet-point instructions, by airport code. Deliberately sparse —
#: only MCO and SFB are confirmed as of the 2026-08-31 rewrite. An airport
#: not listed here (Melbourne, Lakeland, or anything unrecognized) falls back
#: to the plain "baggage claim area" rather than inventing a floor or landmark
#: nobody has verified.
_MEET_POINT_BY_AIRPORT_CODE = {
    "MCO": (
        "the baggage claim area on the 2nd floor, right at the bottom of "
        "the escalators by the information desk"
    ),
    "SFB": (
        "the baggage claim area on level 1, at the bottom of the escalator "
        "or elevator by the information desk"
    ),
}

_DEFAULT_MEET_POINT = "the baggage claim area"


def _meet_point(location: str) -> str:
    """Where to tell an arriving guest to expect the chauffeur, for a pickup
    at `location`. See _MEET_POINT_BY_AIRPORT_CODE."""
    name = _airport_name(location)
    code = name.rsplit("(", 1)[-1].rstrip(")") if "(" in name else ""
    return _MEET_POINT_BY_AIRPORT_CODE.get(code, _DEFAULT_MEET_POINT)


def _who_intro(driver: str) -> str:
    """'This is Marcus with Grayson Towncar.' / degrades when no name is set."""
    return f"This is {driver} with {COMPANY}." if driver else f"I'm your {COMPANY} chauffeur."


def _signature(driver: str) -> str:
    """'— Marcus, Grayson Towncar' / degrades when no name is set."""
    return f"— {driver}, {COMPANY}" if driver else f"— Your {COMPANY} chauffeur"


def _daypart(t) -> str:
    """morning / afternoon / evening from a datetime.time. Keyed off the
    booked pickup time, not the moment the text is actually sent — the copy
    is rendered onto the job card well before a chauffeur taps the button."""
    hour = getattr(t, "hour", None)
    if hour is None:
        return "day"
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def _scheduled_greeting(leg, first: str) -> str:
    """'Good morning, Jane!' — for a situation tied to a fixed appointment,
    where the chauffeur is the one initiating contact at a known time."""
    return f"Good {_daypart(getattr(leg, 'pickup_time', None))}, {first}!"


def _controlling_flight(leg):
    """The flight this pickup actually keys off. Prefers an already-loaded
    OneToOne so the card loop never fires a per-leg query."""
    flight = getattr(leg, "flight_information", None)
    if flight is not None:
        return flight
    try:
        from dispatching.pickup_policy import controlling_flight

        return controlling_flight(leg)
    except Exception:
        return None


def _landing_phrase(leg) -> str:
    """' (landing 4:35 PM)' when a usable arrival time exists, else ''."""
    flight = _controlling_flight(leg)
    if flight is None:
        return ""
    best = None
    getter = getattr(flight, "best_arrival_local", None)
    if callable(getter):
        try:
            best = getter()
        except Exception:
            best = None
    if best is None:
        return ""
    return f" (landing {time12(best)})" if time12(best) else ""


def _cruise_terminal(leg) -> str:
    """'the Royal Caribbean terminal' / 'the cruise terminal'."""
    cruise = getattr(leg, "cruise_information", None)
    line = (getattr(cruise, "cruise_line", "") or "").strip() if cruise else ""
    return f"the {line} terminal" if line else "the cruise terminal"


def _port_destination(leg) -> str:
    """'the Royal Caribbean terminal at Port Canaveral'."""
    cruise = getattr(leg, "cruise_information", None)
    line = (getattr(cruise, "cruise_line", "") or "").strip() if cruise else ""
    return (
        f"the {line} terminal at Port Canaveral"
        if line
        else "the cruise terminal at Port Canaveral"
    )


def _is_charter(leg) -> bool:
    """True when the booking is an hourly / as-directed charter. There is no
    trip-type value for charter — it is carried by a LegStop row.

    Uses the public prefetch cache (leg.legstop_set.all()) rather than reading
    _prefetched_objects_cache directly — Django transparently serves that from
    the prefetch when one was requested with prefetch_related("legstop_set"),
    and falls back to a per-leg query otherwise, with no risk of silently
    reading the wrong cache key if a to_attr is ever added upstream.
    """
    try:
        return any(getattr(s, "stop_type", "") == "charter" for s in leg.legstop_set.all())
    except Exception:
        return False


# ── Classification ─────────────────────────────────────────────────────────

def classify(leg) -> str:
    """Which of the eight situations this leg is. Never raises."""
    pickup = getattr(leg, "pickup_location", "") or ""
    dropoff = getattr(leg, "dropoff_location", "") or ""

    pickup_is_port = _is_port(pickup)
    dropoff_is_port = _is_port(dropoff)
    pickup_is_airport = _is_airport(pickup)

    # Cruise first — a port pickup outranks everything, and a guest coming off a
    # ship is the most distinctive experience of the eight.
    if pickup_is_port:
        return CRUISE_FROM_PORT
    if dropoff_is_port:
        return CRUISE_TO_PORT_AIR if pickup_is_airport else CRUISE_TO_PORT_LAND

    # An airport PICKUP is an arrival regardless of where it drops — this is the
    # airport->airport case get_trip_type() calls 'other'.
    if pickup_is_airport:
        return ARRIVAL_TRACKED if _landing_phrase(leg) else ARRIVAL_UNTRACKED

    if _is_charter(leg):
        return CHARTER

    if _is_airport(dropoff):
        return DEPARTURE

    return OTHER


# ── Copy ───────────────────────────────────────────────────────────────────

def _on_the_way(leg, situation, *, first, driver, vehicle_clause) -> str:
    intro = _who_intro(driver)
    time_str = _pickup_time(leg)
    at_time = f" {time_str}" if time_str else ""

    if situation in _IN_TERMINAL:
        meet = _meet_point(leg.pickup_location)
        return (
            f"Hello, {first}! {intro} Welcome to Orlando — I hope you had a "
            f"great flight.\n\n"
            f"Please send me a quick message as soon as you get off the "
            f"plane. I'll meet you in {meet}. I'll be holding a sign with "
            f"your name.\n\n"
            f"I look forward to meeting you shortly!"
        )

    greeting = _scheduled_greeting(leg, first)

    if situation == DEPARTURE:
        airport = _airport_name_plain(leg.dropoff_location)
        pickup_location = (getattr(leg, "pickup_location", "") or "").strip()
        from_clause = f" from {pickup_location}" if pickup_location else ""
        return (
            f"{greeting} {intro} I'm on my way for your{at_time} pickup"
            f"{from_clause} to {airport}.\n\n"
            f"I'll send you a quick message as soon as I arrive. I look "
            f"forward to seeing you shortly!"
        )

    if situation == CRUISE_TO_PORT_LAND:
        return (
            f"{greeting} {intro} I'm on my way for your{at_time} pickup to "
            f"{_port_destination(leg)}.\n\n"
            f"I'll send you a quick message as soon as I arrive. I look "
            f"forward to seeing you shortly!"
        )

    if situation == CRUISE_FROM_PORT:
        return (
            f"{greeting} {intro} Welcome back! I'll be your chauffeur from "
            f"{_port_destination(leg)} today.\n\n"
            f"Once you're through customs and ready for pickup, please send "
            f"me a quick message. I'll be nearby and ready to meet you.\n\n"
            f"See you shortly!"
        )

    if situation == CHARTER:
        return (
            f"{greeting} {intro} I'm on my way for your{at_time} pickup and "
            f"will be your chauffeur for the day.\n\n"
            f"I'll send you a quick message as soon as I arrive. I look "
            f"forward to seeing you shortly!"
        )

    return (
        f"{greeting} {intro} I'm on my way for your{at_time} pickup.\n\n"
        f"I'll send you a quick message as soon as I arrive. I look forward "
        f"to seeing you shortly!"
    )


def _on_location(leg, situation, *, first, driver, vehicle_clause) -> str:
    sig = _signature(driver)

    if situation in _IN_TERMINAL:
        meet = _meet_point(leg.pickup_location)
        return (
            f"Hi {first}, I'm here at {meet}. I'll be holding a sign with "
            f"your name.\n\n"
            f"See you shortly!\n\n"
            f"{sig}"
        )

    if situation == DEPARTURE:
        greeting = _scheduled_greeting(leg, first)
        pickup_location = (getattr(leg, "pickup_location", "") or "").strip()
        where = f" at {pickup_location}" if pickup_location else ""
        return (
            f"{greeting} I've arrived{where} and I'm outside for your "
            f"pickup.\n\n"
            f"Just send me a quick message when you're coming out, and "
            f"I'll be ready to assist you with your luggage.\n\n"
            f"{sig}"
        )

    if situation == CRUISE_TO_PORT_LAND:
        greeting = _scheduled_greeting(leg, first)
        return (
            f"{greeting} I've arrived and I'm outside for your pickup"
            f"{vehicle_clause}.\n\n"
            f"Just send me a quick message when you're coming out, and "
            f"I'll be ready to assist you with your luggage.\n\n"
            f"{sig}"
        )

    if situation == CRUISE_FROM_PORT:
        return (
            f"Hi {first}, I'm here at {_cruise_terminal(leg)}{vehicle_clause}."
            f"\n\n"
            f"Once you're through customs and ready for pickup, just send me "
            f"a quick message and I'll pull around to meet you.\n\n"
            f"{sig}"
        )

    # CHARTER and OTHER (point to point) share the same closing — no luggage
    # promised, since neither implies one the way an airport/cruise trip does.
    greeting = _scheduled_greeting(leg, first)
    return (
        f"{greeting} I've arrived and I'm outside for your pickup"
        f"{vehicle_clause}.\n\n"
        f"Just send me a quick message when you're coming out, and I'll be "
        f"ready for you.\n\n"
        f"{sig}"
    )


def _review(leg, situation, *, first, driver, vehicle_clause) -> str:
    if situation in (ARRIVAL_TRACKED, ARRIVAL_UNTRACKED):
        closing = "Enjoy your stay"
    elif situation in (CRUISE_TO_PORT_AIR, CRUISE_TO_PORT_LAND):
        closing = "Have a wonderful cruise"
    elif situation == DEPARTURE:
        closing = "Safe travels"
    elif situation == CRUISE_FROM_PORT:
        closing = "Welcome back"
    else:
        closing = "Take care"

    return (
        f"It was a pleasure driving you today, {first}. If I took good care of "
        f"you, a quick review means a great deal to us at {COMPANY} — and it's the surest way "
        f"to have me requested again. {closing}!\n\n{REVIEW_URL}"
    )


_RENDERERS = {
    ON_THE_WAY: _on_the_way,
    ON_LOCATION: _on_location,
    REVIEW: _review,
}


def build(leg, kind, *, driver_name=None, vehicle=None, situation=None) -> Message:
    """Render one standard message for `leg`.

    driver_name  first name of the chauffeur, so the guest knows who is texting.
    vehicle      the day's drivers.FleetVehicle, for "in a Chevrolet Suburban".
    situation    override the classifier (tests, and a dispatcher preview).
    """
    if kind not in _RENDERERS:
        raise ValueError(f"unknown message kind: {kind!r}")

    situation = situation or classify(leg)
    body = _RENDERERS[kind](
        leg,
        situation,
        first=_first_name(leg),
        driver=(driver_name or "").strip(),
        vehicle_clause=_vehicle_clause(vehicle),
    )
    return Message(kind=kind, situation=situation, body=body)


def build_all(leg, *, driver_name=None, vehicle=None) -> dict:
    """All three messages for a leg, keyed by kind. One classify() call."""
    situation = classify(leg)
    return {
        kind: build(
            leg, kind, driver_name=driver_name, vehicle=vehicle, situation=situation
        )
        for kind in KINDS
    }


# ── Delivery ───────────────────────────────────────────────────────────────

def sms_href(phone, body) -> str:
    """An `sms:` deep link that opens the driver's own Messages app pre-filled.

    The guest's reply then lands on the chauffeur's handset, which is the whole
    point — the office Twilio number is GoHighLevel-managed and its inbound
    replies are invisible to this app (drivers/wakeup.py:16-18).

    Everything is percent-encoded. The four hand-written hrefs this replaces
    interpolated a name straight into the URL with raw spaces and commas.

    Phone normalization goes through drivers.sms.normalize_e164 — the same
    function every other outbound number in this app goes through — rather
    than a second hand-rolled digit-strip, so a 10-digit US number reliably
    gets its +1 here too.
    """
    from drivers.sms import normalize_e164

    digits = normalize_e164(phone)
    return f"sms:{digits}?body={quote(body or '', safe='')}"
