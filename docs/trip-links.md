# Trip links — Mapping and Flight Tracker

Dispatchers used to keep two other tabs open all day: Google Maps, to see where
a trip actually goes, and FlightAware / FlightView, to see whether the plane is
really landing when the booking says it is. Both meant retyping an address or a
flight number that the board was already showing.

This feature puts both one click away, everywhere a leg appears.

## What a dispatcher sees

**Map buttons.** Every pickup, drop-off and stop on the board carries a small
`📍 Map` button that opens that one address in Google Maps. Rows on the
dashboard also carry a **Route** button for the whole trip — pickup, on-the-way
stops, then the drop-offs, in the order the driver will actually do them.

**Right-click any trip.** On the dashboard, All Legs, the schedule board, the
capacity planner and the reservation page, right-clicking a trip row (or a
timeline bar, or an unassigned chip) opens a menu:

- **Mapping ▸** — full route, then each address on its own
- **Flight Tracker ▸** — the airline's own status page (Delta, American,
  JetBlue, Southwest), then FlightAware and FlightView, for every flight on the
  leg, with the one that controls the pickup time listed first
- Copy pickup / drop-off address
- Open reservation · Leg history

On a tablet, long-press does the same thing. Shift + right-click gives the
browser's own menu instead, and right-clicking a text field is always left
alone so copy/paste keeps working.

## Where the code lives

| Piece | File |
| --- | --- |
| URL builders (the only place a link is constructed) | `reservations/trip_links.py` |
| Per-leg shortcuts for templates | `Leg.pickup_map_url`, `Leg.dropoff_map_url`, `Leg.route_map_url`, `Leg.flight_tracker_links`, `LegStop.map_url` |
| JSON the menu fetches | `dispatching/trip_link_views.py` → `leg_trip_links` |
| Menu (CSS + markup + JS, self-contained) | `dispatching/templates/dispatching/includes/_trip_context_menu.html` |
| The `📍 Map` button | `dispatching/templates/dispatching/includes/_map_link_btn.html` |
| Tests | `dispatching/tests_trip_links.py` |

### Adding the menu to another page

1. Make sure the row carries a numeric `data-leg-id`.
2. Include the menu once, at the end of the page:
   `{% include "dispatching/includes/_trip_context_menu.html" %}`

That's it — there is no per-page payload to render. The menu fetches
`/dispatching/leg/<id>/trip-links/` on first open and caches the result, so a
page can carry a thousand rows without paying for a single one it never opens.

### Adding a Map button somewhere

```django
{% include "dispatching/includes/_map_link_btn.html" with url=leg.pickup_map_url text=leg.pickup_location %}
```

Pass `compact=1` for the icon without the word "Map". The partial renders
nothing when `url` is empty, so a leg with a blank drop-off never shows a button
that goes nowhere.

## Decisions worth knowing

**Google's `dir/?api=1` form, not `?saddr=&daddr=`.** The documented URL API is
the supported contract and it takes waypoints — and waypoints are the point. A
leg with a Publix stop and a second resort drop-off is a four-point route, and
the old two-parameter form would quietly show a straight line that skips both.

**Addresses get `, FL` appended — unless they already name a state.** Half the
locations in this business are venue names ("Publix Lake Buena Vista",
"Terminal B") rather than addresses, and Google will happily resolve those in
another state. The hint is skipped whenever the text already contains a US
state token, so a genuine `Atlanta, GA` is never dragged into Florida.

**FlightView is anchored on the flight's DEPARTURE date, not the pickup date.**
A red-eye that leaves on the 7th and lands on the 8th is indexed under the 7th.
`Flight.departure_date` exists for exactly this; the pickup date is only the
fallback. Getting this backwards shows the wrong night's flight — the same trap
the overnight-arrival work already fixed elsewhere.

**FlightAware needs its own airline code.** JetBlue is `B6` to the world and
`JBU` to FlightAware. `get_flightaware_code()` already held that mapping for
AeroAPI, so the links reuse it rather than starting a second one.

**Only verified airline URL formats ship.** `AIRLINE_TRACKERS` in
`reservations/trip_links.py` maps IATA code → (site label, URL builder), and
today it holds the four that have been checked against a real flight:

| Airline | Format |
| --- | --- |
| Delta (`DL`) | `delta.com/flightstatus/1/{number}/{YYYY-MM-DD}` |
| American (`AA`) | `aa.com/travelInformation/flights/status/detail?search=AA\|{number}\|{YYYY,M,D}&ref=search` |
| JetBlue (`B6`) | `jetblue.com/flight-tracker-and-status?by=flight&number={number}&date={YYYY-MM-DD}` |
| Southwest (`WN`) | `southwest.com/air/flight-status/path?flightNumber={number}&departureDate={YYYY-MM-DD}&searchType=flight` |
| United (`UA`) | `united.com/en/us/flightstatus/details/{number}/{YYYY-MM-DD}/{origin}/{dest}/UA` |

Adding a carrier is one line in that dict plus a test pinning the exact URL.
Guessing at a format is worse than leaving it out: the dispatcher lands on an
error page while believing they're looking at the flight. Carriers not in the
table simply show FlightAware and FlightView, which cover every airline.

All of these key off the departure date, so they follow the same red-eye rule
as FlightView above — and a flight with no date gets no airline link at all.

**United is the only one that needs the route.** `Flight.origin` /
`Flight.destination` are written by the AeroAPI refresh in a `CODE - Name`
shape; `airport_code()` bares the IATA code and strips the ICAO K-prefix. On
the live data 8201 of 8240 refreshed flights carry both, so the link shows up
on effectively every United flight we've tracked and is quietly skipped on one
we haven't.

**Allegiant (`G4`) cannot be added.** Its status page holds all of its state in
an opaque per-session token —
`allegiantair.com/flight-status#!&init=1786222639438&m=4Fj88Pn9xRG…` — so there
is no URL to construct for an arbitrary flight. A test pins this so nobody
"fixes" it with a guess.

**Flight numbers are un-padded before linking.** About 1 in 30 stored numbers
is zero-padded (`WN 0574`, `B6 0969`) because that's how it reads on a boarding
pass, and every tracker treats `0574` as no such flight.
`normalize_flight_number()` itself is left alone — the AeroAPI callers have
their own behaviour and this is a link-building concern.

**An unrecognized airline produces no link at all.** A tracker URL built on a
guessed code either 404s or — worse — opens a different airline's flight. The
same 2-3-alphanumeric guard `get_flight_ident()` uses applies here, and when it
fails the menu says "No trackable flight on this trip".

**Charter stops are never mapped.** They legitimately have no destination — the
guest directs the driver — and `display_location` renders a friendly sentence
for them. Handing that sentence to Google is nonsense, so `stop_address()`
reads the raw location and returns nothing.
