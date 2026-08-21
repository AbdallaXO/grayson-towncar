"""The right-click menu's "where is the car, and how does it get to its next stop".

The trip menu already answers "where is this job *supposed* to go" from the
booking (reservations/trip_links.py). This module answers the other half from
the vehicle's own telemetry: the car's live coordinates, and a Google Maps
directions link from those coordinates to whichever end of the trip is still
ahead of it — the pickup, or the DROP-OFF once the guest is aboard.

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


def leg_destination(status, pickup, dropoff):
    """
    Which end of this trip the car is still heading for. Returns
    (address, kind) with kind one of "pickup", "dropoff", "".

    A trip has two halves and the dispatcher's question follows the car through
    them. Before the guest is aboard it is "how far out is he from the pickup?".
    The moment the chauffeur marks picked-up the question flips to "how far is he
    from dropping off?" — and routing him to a pickup he has already made answers
    nothing at all. On-location counts as aboard for the same reason: he is
    standing at that address, so directions to it are a link to where he already
    is (this is the same line dispatching/samsara_risk.py draws for the board's
    live ETA badge, which is why the two now agree).

    A completed or cancelled leg has no next stop in either direction. It gets no
    destination, and the caller falls back to a plain pin on the car — the honest
    answer to "where is he now" when the job itself is over.

    Pure string-in, string-out: the status is passed in, never read off a model,
    so the rule stays testable without a database.
    """
    status = (status or "").strip()
    if status in CLOSED_STATUSES:
        return "", ""
    if status in ON_TRIP_STATUSES:
        return (dropoff or "").strip(), "dropoff"
    return (pickup or "").strip(), "pickup"


def live_link(lat, lng, destination=""):
    """
    (url, destination_label) for the car's current position.

    With a `destination` — the leg's next stop, from `leg_destination` above —
    this is DIRECTIONS, car -> that address, which is the question a dispatcher
    actually has open. Without one (the fleet pages, where no job is in view, and
    trips already closed out) it is a plain pin on the coordinates.

    Returns (None, "") when there is no position to link to.

    Two fallbacks worth knowing about:
      * `maps_directions_url` refuses a one-ended route, because Google silently
        resolves that to "directions from your current location" — a lie about
        where the car is. A leg with a blank address at the end it is heading for
        therefore falls back to the pin rather than to a wrong link.
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
