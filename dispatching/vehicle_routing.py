"""The right-click menu's "where is the car, and how far is it from either end".

The trip menu already answers "where is this job *supposed* to go" from the
booking (reservations/trip_links.py). This module answers the other half from
the vehicle's own telemetry: the car's live coordinates, and Google Maps
directions from those coordinates to BOTH ends of the trip — pickup and drop-off
— with the end the car is actually heading for marked.

Pure: no network, no database, no clock. Callers pass the stored position and
get back a URL, which is what makes the fallback rules testable without a single
mocked response.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not fetch, segment or draw the car's driven route. An earlier version
pulled GPS breadcrumbs from `/fleet/vehicles/locations/history`, split them into
drives and stops, and rendered a step-by-step itinerary plus a whole-window
route link. Both were cut as noise: the operational question is "how far out is
he?", and the rest was decoration around it.

That history endpoint IS entitled on this account and its measured behaviour is
recorded in docs/fleet-management.md ("GPS breadcrumbs"), so bringing it back is
a research-free job if the need ever appears. Until then nothing calls it, which
is why this menu costs a DB read rather than a Samsara round-trip.
"""

from reservations.constants import CLOSED_STATUSES, ON_TRIP_STATUSES
from reservations.trip_links import maps_directions_url, maps_place_url


def short_place(formatted):
    """
    Samsara's reverse-geo string trimmed to what a dispatcher would read.

    "5744 Crowntree Lane, Orlando, FL, 32829" -> "5744 Crowntree Lane, Orlando"

    Only the state and postal tail come off. No attempt is made to recognise
    venues: the reverse-geo already names the road or business, and rewriting it
    from a lookup table would be guessing about a place we can see.
    """
    parts = [p.strip() for p in (formatted or "").split(",") if p.strip()]
    if not parts:
        return ""
    # Drop a trailing ZIP and a trailing 2-letter state, in that order.
    if parts and parts[-1].replace("-", "").isdigit():
        parts = parts[:-1]
    if len(parts) > 1 and len(parts[-1]) == 2 and parts[-1].isalpha():
        parts = parts[:-1]
    return ", ".join(parts[:2])


def live_link(lat, lng, destination=""):
    """
    (url, destination_label) for the car's current position.

    With a `destination` — one end of the trip, from `leg_routes` below — this is
    DIRECTIONS, car -> that address, which is the question a dispatcher actually
    has open. Without one (the fleet pages, where no job is in view) it is a plain
    pin on the coordinates.

    Returns (None, "") when there is no position to link to.

    Two fallbacks worth knowing about:
      * `maps_directions_url` refuses a one-ended route, because Google silently
        resolves that to "directions from your current location" — a lie about
        where the car is. A blank address therefore falls back to the pin rather
        than to a wrong link, and the empty label is how `leg_routes` knows to
        drop that end.
      * The URL carries the FULL booked address because that is what Google
        resolves accurately; the returned label is trimmed to the venue, because
        the menu row is 300px wide and the street tail tells a dispatcher
        nothing.
    """
    if lat is None or lng is None:
        return None, ""

    # trip_links.map_query passes a bare "lat,lng" through untouched, so the
    # coordinate is never region-hinted into somewhere else.
    coord = f"{float(lat):.6f},{float(lng):.6f}"
    destination = (destination or "").strip()

    if destination:
        url = maps_directions_url(coord, destination)
        if url:
            label = destination.split(",")[0].strip() or destination
            return url, label

    return maps_place_url(coord), ""


def next_stop_kind(status):
    """
    Which end of the trip the car is heading for: "pickup", "dropoff", or "" when
    the job is over and nothing is next.

    Both ends are offered either way (see `leg_routes` below) — this only decides which
    one is MARKED, so a dispatcher reading the menu doesn't have to remember the
    chauffeur's status to know which number he wants. Before the guest is aboard
    it's the pickup ("how far out is he?"); the moment picked-up is marked the
    live question becomes the drop-off ("how much longer has he got?").

    On-location counts as aboard: he's standing at the pickup, so the pickup is no
    longer the open question. That is the same line dispatching/samsara_risk.py
    draws for the board's live ETA badge (both read the sets from
    reservations/constants.py), which is why badge and menu now agree.

    Pure string-in, string-out: the status is passed in, never read off a model,
    so the rule stays testable without a database.
    """
    status = (status or "").strip()
    if status in CLOSED_STATUSES:
        return ""
    if status in ON_TRIP_STATUSES:
        return "dropoff"
    return "pickup"


def leg_routes(lat, lng, status, pickup, dropoff):
    """
    Both ends of the trip as directions FROM the car, in the trip's own order:
    [{kind, url, destination, next}, ...].

    Always both, because the dispatcher's next question rarely stops at the one
    the chauffeur is driving to. "How far out is he?" is followed by "and how long
    once he's got them?", and on a job in progress by "where did that one start?"
    when a call comes in. A menu that answers only the live half sends him to
    another screen for the other half.

    In TRIP order rather than relevance order, so the rows never swap places under
    the cursor as a trip progresses — the same order the Copy pickup / Copy
    drop-off pair uses further down the same menu. `next` marks the live one
    instead; that is what moves.

    An end with no address is simply absent from the list: `live_link` falls back
    to a pin there, and a pin labelled "Route to drop-off" would be a lie about
    what the row opens. Callers get the reason into the note.
    """
    next_kind = next_stop_kind(status)
    routes = []
    for kind, address in (("pickup", pickup), ("dropoff", dropoff)):
        url, label = live_link(lat, lng, address)
        if not label:
            continue          # no position at all, or no address for this end
        routes.append({
            "kind": kind,
            "url": url,
            "destination": label,
            "next": kind == next_kind,
        })
    return routes
