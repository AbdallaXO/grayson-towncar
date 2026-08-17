"""Deep links out of a leg: Google Maps directions and public flight trackers.

Dispatchers already live in two other tabs all day — Google Maps for "where is
this actually going" and FlightAware / FlightView for "is the plane really
landing when we think". Every one of those URLs is built here, so the
right-click trip menu, the inline P/U and D/O buttons and the reservation page
can never disagree about where a trip goes.

Nothing in here touches the network or the database. Callers pass a Leg (or the
raw strings) and get back URLs, or None when there is nothing worth linking to —
a menu item is never rendered pointing at a blank address.
"""

import re
from urllib.parse import quote

from business.datefmt import strf

# Google's documented Maps URL API (api=1). Preferred over the older
# ?saddr=&daddr= form because it is the supported contract AND it takes
# waypoints, which matters here: a leg with a Publix stop and a second drop-off
# is a four-point route, and dispatchers need to see the real one.
MAPS_DIRECTIONS_BASE = "https://www.google.com/maps/dir/?api=1"
MAPS_SEARCH_BASE = "https://www.google.com/maps/search/?api=1"

# Public trackers. FlightAware keys off its own airline code (POE580);
# FlightView keys off the IATA code with the flight number as a path segment
# and the DEPARTURE date as a query param (…/PD/580?date=2026-08-08).
FLIGHTAWARE_BASE = "https://www.flightaware.com/live/flight/"
FLIGHTVIEW_BASE = "https://app.flightview.com/flight-tracker/"

# The airline's own status page, which is the one that knows about gate changes
# and equipment swaps before the aggregators do. Keyed by IATA code; each entry
# is (short site label, builder). The builder takes a context dict —
# {"number", "date", "origin", "destination"} — and returns a URL, or None when
# that carrier needs something this flight hasn't got.
#
# Only airlines whose URL format has been verified against a real flight belong
# here. A guessed format lands the dispatcher on an error page while they think
# they're looking at the flight, which is worse than not offering the link —
# unlisted airlines simply fall back to FlightAware / FlightView, which cover
# every carrier.
#
# Allegiant (G4) is deliberately absent and can't be added: its status page
# carries all of its state in an opaque per-session token
# (allegiantair.com/flight-status#!&init=…&m=4Fj88Pn9xRG…), so there is no URL
# to construct for an arbitrary flight.
AIRLINE_TRACKERS = {
    # https://www.delta.com/flightstatus/1/1548/2026-08-08
    "DL": ("Delta.com", lambda c: (
        f"https://www.delta.com/flightstatus/1/{c['number']}/{c['date'].isoformat()}"
    )),
    # https://www.aa.com/travelInformation/flights/status/detail?search=AA%7C1228%7C2026,8,8&ref=search
    "AA": ("aa.com", lambda c: (
        "https://www.aa.com/travelInformation/flights/status/detail"
        f"?search=AA%7C{c['number']}%7C{c['date'].year},{c['date'].month},{c['date'].day}"
        "&ref=search"
    )),
    # https://www.jetblue.com/flight-tracker-and-status?by=flight&number=670&date=2026-08-08
    "B6": ("jetblue.com", lambda c: (
        "https://www.jetblue.com/flight-tracker-and-status"
        f"?by=flight&number={c['number']}&date={c['date'].isoformat()}"
    )),
    # https://www.southwest.com/air/flight-status/path?flightNumber=2659&departureDate=2026-08-08&searchType=flight
    "WN": ("southwest.com", lambda c: (
        "https://www.southwest.com/air/flight-status/path"
        f"?flightNumber={c['number']}&departureDate={c['date'].isoformat()}"
        "&searchType=flight"
    )),
    # https://www.united.com/en/us/flightstatus/details/2245/2026-08-08/IAH/MCO/UA
    # The only one that needs the route in the URL. AeroAPI fills origin and
    # destination on refresh (99.5% of refreshed flights have both), so this
    # link appears on every United flight we've actually tracked and is quietly
    # skipped on one we haven't.
    "UA": ("united.com", lambda c: (
        "https://www.united.com/en/us/flightstatus/details"
        f"/{c['number']}/{c['date'].isoformat()}/{c['origin']}/{c['destination']}/UA"
    ) if c["origin"] and c["destination"] else None),
}

# Google caps the URL API at 9 waypoints between origin and destination.
MAX_WAYPOINTS = 9

# Every location this operation touches is Central Florida, and half of them are
# written as venue names ("Publix Lake Buena Vista", "Terminal B") rather than
# addresses. Appending the state stops Google from dropping the dispatcher on a
# Publix in another state. Skipped whenever the text already names a state, so a
# genuine out-of-state address ("Atlanta, GA") is left exactly as written.
REGION_HINT = "FL"
_US_STATES = frozenset(
    """AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS
    MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI
    WY DC PR VI""".split()
)
_STATE_RE = re.compile(r"\b(" + "|".join(_US_STATES) + r"|FLORIDA)\b")
# "28.4312, -81.3081" — already a coordinate, so leave it completely alone.
_LATLNG_RE = re.compile(r"^\s*-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+\s*$")


def map_query(location):
    """The string handed to Google for `location`, region-hinted when it needs it.

    Returns "" for anything blank so callers can skip the link entirely.
    """
    text = (location or "").strip()
    if not text:
        return ""
    if _LATLNG_RE.match(text):
        return text
    if _STATE_RE.search(text.upper()):
        return text
    return f"{text}, {REGION_HINT}"


def maps_place_url(location):
    """Google Maps search for a single address — the P/U or D/O button."""
    query = map_query(location)
    if not query:
        return None
    return f"{MAPS_SEARCH_BASE}&query={quote(query)}"


def maps_directions_url(origin, destination, waypoints=None):
    """Google Maps driving directions, origin → waypoints → destination.

    Returns None unless BOTH ends are present — a one-ended route silently
    resolves to "directions from your current location", which is a lie about
    where the driver starts.
    """
    origin_q = map_query(origin)
    dest_q = map_query(destination)
    if not origin_q or not dest_q:
        return None

    url = (
        f"{MAPS_DIRECTIONS_BASE}"
        f"&origin={quote(origin_q)}"
        f"&destination={quote(dest_q)}"
        f"&travelmode=driving"
    )
    stops = [map_query(w) for w in (waypoints or [])]
    stops = [s for s in stops if s][:MAX_WAYPOINTS]
    if stops:
        url += "&waypoints=" + quote("|".join(stops), safe="|")
    return url


# "ORD - Chicago O'Hare Intl" → ORD. Flight.origin/destination are written by
# the AeroAPI refresh in that "CODE - Name" shape; older rows hold a bare code.
_AIRPORT_CODE_RE = re.compile(r"^([A-Z]{3,4})\b")


def airport_code(text):
    """Bare IATA code from a stored airport string, or "" when there isn't one.

    Strips the ICAO K-prefix (KMCO → MCO) the same way the AeroAPI parser does,
    so both storage shapes resolve to the code an airline URL expects.
    """
    match = _AIRPORT_CODE_RE.match((text or "").strip().upper())
    if not match:
        return ""
    code = match.group(1)
    if len(code) == 4 and code.startswith("K"):
        code = code[1:]
    return code if len(code) == 3 else ""


def airline_tracker_url(iata, flight_number, flight_date, origin="", destination=""):
    """The airline's own status page as (site_label, url).

    None when we have no verified format for that carrier, when there's no date
    to anchor it on (every one of these pages needs the departure date), or when
    the carrier needs a route we don't have yet.
    """
    entry = AIRLINE_TRACKERS.get((iata or "").upper())
    if not entry or not flight_date or not flight_number:
        return None
    site, build = entry
    url = build({
        "number": flight_number,
        "date": flight_date,
        "origin": airport_code(origin),
        "destination": airport_code(destination),
    })
    return (site, url) if url else None


def flight_tracker_urls(airline, flight_number, flight_date=None,
                        origin="", destination=""):
    """FlightAware + FlightView + (when we have it) the airline's own page.

    Returns None when the flight isn't linkable at all.

    `airline` is anything the airline field holds ("Delta", "DL", "delta air
    lines"); it is normalized to IATA the same way the AeroAPI callers do, and
    an unrecognized airline returns None rather than a URL that 404s.

    `flight_date` should be the flight's DEPARTURE date, not the pickup date —
    a red-eye that leaves the 7th and lands the 8th is the 7th's flight on
    FlightView, and passing the pickup date there shows the wrong instance.
    """
    from .utils import (
        normalize_airline,
        normalize_flight_number,
        get_flightaware_code,
        get_airline_display_name,
    )

    raw_airline = (airline or "").strip()
    digits = normalize_flight_number((flight_number or "").strip()) or ""
    # Roughly 1 in 30 stored flight numbers is zero-padded ("WN 0574", "B6 0969")
    # because that's how the guest typed it off a boarding pass. Every tracker
    # here wants the bare number and reads "0574" as no such flight.
    # normalize_flight_number itself is left alone — the AeroAPI callers have
    # their own behaviour and this is a link-building concern.
    digits = digits.lstrip("0") or digits
    if not raw_airline or not digits:
        return None

    iata = normalize_airline(raw_airline)
    # Same guard get_flight_ident() uses: real IATA codes are 2-3 alphanumeric
    # characters. Anything else means we don't actually know the airline, and a
    # tracker link built on a guess sends the dispatcher somewhere wrong.
    if not iata or len(iata) > 3 or not iata.isalnum():
        return None

    fa_code = get_flightaware_code(iata) or iata
    label = f"{get_airline_display_name(iata) or iata} {digits}".strip()

    flightview = f"{FLIGHTVIEW_BASE}{quote(iata)}/{quote(digits)}"
    if flight_date:
        flightview += f"?date={flight_date.isoformat()}"

    native = airline_tracker_url(iata, digits, flight_date, origin, destination)

    return {
        "label": label,
        "ident": f"{fa_code}{digits}",
        "flightaware": f"{FLIGHTAWARE_BASE}{quote(fa_code + digits)}",
        "flightview": flightview,
        # Empty for carriers we haven't verified a URL format for — the menu
        # just shows the two aggregators in that case.
        "airline_site": native[0] if native else "",
        "airline_url": native[1] if native else "",
    }


def stop_address(stop):
    """A LegStop's mappable address, or "" when it hasn't got one.

    Charter stops legitimately carry no destination (the guest directs the
    driver); `display_location` renders a friendly sentence for those, which is
    exactly the string we must not hand to Google.
    """
    if getattr(stop, "location_id", None) and stop.location:
        return (stop.location.name or "").strip()
    return (getattr(stop, "location_text", "") or "").strip()


def leg_flights(leg):
    """Every flight attached to a leg, controlling one first.

    Reads the prefetched legflight_set and falls back to the legacy
    flight_information OneToOne for legs that predate multi-flight support —
    the same resolution order as Leg.controlling_flight.
    """
    rows = []
    seen = set()
    try:
        legflights = list(leg.legflight_set.all())
    except (AttributeError, ValueError):
        legflights = []
    for lf in legflights:
        if lf.flight_id in seen:
            continue
        seen.add(lf.flight_id)
        rows.append((lf.flight, bool(lf.is_controlling)))
    if not rows and leg.flight_information_id:
        rows.append((leg.flight_information, True))
    # Controlling flight first — that's the one driving the pickup time, so it's
    # the one the dispatcher wants to open.
    rows.sort(key=lambda r: not r[1])
    return rows


def leg_trip_links(leg):
    """Every external link for one leg, ready to render as a menu.

    Shape (keys are always present; values are None/[] when unavailable):

        {"leg_id", "label", "customer", "pickup", "dropoff", "stops",
         "route_url", "flights"}

    `pickup`/`dropoff`/`stops[]` entries are {"label", "text", "url"}.
    """
    pickup_text = (leg.pickup_location or "").strip()
    dropoff_text = (leg.dropoff_location or "").strip()

    stops = []
    for stop in leg.intermediate_stops:
        address = stop_address(stop)
        if not address:
            continue
        stops.append(
            {
                "label": stop.get_stop_type_display(),
                "text": address,
                "url": maps_place_url(address),
            }
        )

    extra_dropoffs = []
    for stop in leg.additional_dropoffs:
        address = stop_address(stop)
        if not address:
            continue
        extra_dropoffs.append(
            {
                "label": "Additional drop-off",
                "text": address,
                "url": maps_place_url(address),
            }
        )

    # The real itinerary: pickup → on-the-way stops → primary drop-off →
    # additional drop-offs. When extra drop-offs exist the LAST one is the true
    # end of the route and the booked drop-off becomes a waypoint.
    tail = [s["text"] for s in extra_dropoffs]
    if tail:
        final_destination = tail[-1]
        waypoints = [s["text"] for s in stops] + [dropoff_text] + tail[:-1]
    else:
        final_destination = dropoff_text
        waypoints = [s["text"] for s in stops]

    flights = []
    for flight, is_controlling in leg_flights(leg):
        links = flight_tracker_urls(
            flight.airline,
            flight.flight_number,
            flight.departure_date or leg.pickup_date,
            origin=flight.origin,
            destination=flight.destination,
        )
        if not links:
            continue
        links["is_controlling"] = is_controlling
        flights.append(links)

    customer = ""
    if leg.reservation_id and leg.reservation.customer_id:
        customer = str(leg.reservation.customer)

    when = ""
    if leg.pickup_time:
        when = leg.pickup_time.strftime("%I:%M %p").lstrip("0")
    if leg.pickup_date:
        # strf, not strftime: %-d is glibc-only and 500s the whole board on Windows.
        when = f"{when} · {strf(leg.pickup_date, '%a, %b %-d')}".strip(" ·")

    return {
        "leg_id": leg.id,
        "label": " · ".join(p for p in (customer, when) if p),
        "customer": customer,
        "pickup": {
            "label": "Pickup",
            "text": pickup_text,
            "url": maps_place_url(pickup_text),
        },
        "dropoff": {
            "label": "Drop-off",
            "text": dropoff_text,
            "url": maps_place_url(dropoff_text),
        },
        "stops": stops + extra_dropoffs,
        "route_url": maps_directions_url(pickup_text, final_destination, waypoints),
        "flights": flights,
    }
