"""Map and flight-tracker deep links — the URLs a dispatcher actually clicks.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_trip_links

The whole point of this feature is that a dispatcher stops retyping addresses
into another tab. That only works if the link lands where the trip actually
goes, so the cases below pin the three ways it could quietly lie:

  * a route that drops the stops, so the Publix run vanishes off the map;
  * a venue name with no state, so "Publix" resolves to another state;
  * a flight tracker built from an airline we don't recognize, or anchored on
    the pickup date when a red-eye departed the night before.
"""

from datetime import date, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from decimal import Decimal

from rates.models import Location, Rate, Route, Vehicle
from reservations import trip_links as tl
from reservations.models import Customer, Flight, Leg, LegFlight, LegStop, Reservation


def make_rate(vehicle, origin_name, destination_name):
    """Minimum viable Rate — Reservation won't save without one."""
    route = Route.objects.create(
        origin=Location.objects.create(name=origin_name),
        destination=Location.objects.create(name=destination_name),
        inhouse_base_pay=Decimal("50.00"),
    )
    return Rate.objects.create(
        route=route, vehicle=vehicle,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )


class MapQueryTests(TestCase):
    """The region hint: help the venue names, never touch a real address."""

    def test_venue_name_gets_the_state_appended(self):
        self.assertEqual(tl.map_query("Publix Lake Buena Vista"),
                         "Publix Lake Buena Vista, FL")

    def test_address_that_already_names_florida_is_left_alone(self):
        addr = "1251 Riverside Dr Lake Buena Vista FL 32830"
        self.assertEqual(tl.map_query(addr), addr)

    def test_out_of_state_address_is_never_dragged_into_florida(self):
        # The dispatcher typed GA on purpose. Appending FL would send the
        # driver 400 miles the wrong way.
        self.assertEqual(tl.map_query("100 Peachtree St, Atlanta, GA"),
                         "100 Peachtree St, Atlanta, GA")

    def test_fl_inside_a_word_does_not_count_as_the_state(self):
        # "Flamingo" contains "Fl" but is not a state token.
        self.assertEqual(tl.map_query("Flamingo Crossings Town Center"),
                         "Flamingo Crossings Town Center, FL")

    def test_coordinates_are_passed_through_untouched(self):
        self.assertEqual(tl.map_query("28.4312, -81.3081"), "28.4312, -81.3081")

    def test_blank_returns_empty_so_callers_can_skip_the_link(self):
        self.assertEqual(tl.map_query("   "), "")
        self.assertEqual(tl.map_query(None), "")


class MapUrlTests(TestCase):
    def test_place_url_encodes_the_address(self):
        url = tl.maps_place_url("MCO - Orlando International Airport")
        self.assertTrue(url.startswith("https://www.google.com/maps/search/?api=1&query="))
        self.assertIn("MCO%20-%20Orlando%20International%20Airport%2C%20FL", url)

    def test_place_url_is_none_when_there_is_no_address(self):
        self.assertIsNone(tl.maps_place_url(""))

    def test_directions_carry_both_ends_and_a_driving_mode(self):
        url = tl.maps_directions_url("1251 Riverside Dr Lake Buena Vista FL 32830",
                                     "MCO - Orlando International Airport")
        self.assertIn("origin=1251%20Riverside", url)
        self.assertIn("destination=MCO%20-%20Orlando", url)
        self.assertIn("travelmode=driving", url)
        self.assertNotIn("waypoints=", url)

    def test_directions_keep_the_stops_in_order(self):
        url = tl.maps_directions_url("Hotel", "MCO", ["Publix", "Walgreens"])
        self.assertIn("waypoints=Publix%2C%20FL|Walgreens%2C%20FL", url)

    def test_directions_refuse_a_one_ended_route(self):
        # Google silently reads a missing origin as "from your current
        # location", which is a lie about where the driver starts.
        self.assertIsNone(tl.maps_directions_url("", "MCO"))
        self.assertIsNone(tl.maps_directions_url("Hotel", "   "))

    def test_waypoints_are_capped_at_googles_limit(self):
        url = tl.maps_directions_url("A", "B", [f"Stop {i}" for i in range(20)])
        self.assertEqual(url.count("|"), tl.MAX_WAYPOINTS - 1)


class FlightTrackerUrlTests(TestCase):
    def test_flightaware_uses_its_own_airline_code(self):
        # JetBlue is B6 to the world and JBU to FlightAware.
        links = tl.flight_tracker_urls("B6", "123", date(2026, 8, 8))
        self.assertEqual(links["ident"], "JBU123")
        self.assertEqual(links["flightaware"],
                         "https://www.flightaware.com/live/flight/JBU123")

    def test_flightview_uses_iata_plus_the_departure_date(self):
        links = tl.flight_tracker_urls("B6", "123", date(2026, 8, 8))
        self.assertEqual(links["flightview"],
                         "https://app.flightview.com/flight-tracker/B6/123?date=2026-08-08")

    def test_airline_written_as_a_name_still_resolves(self):
        links = tl.flight_tracker_urls("Delta Airlines", "DL1691", date(2026, 8, 8))
        self.assertEqual(links["ident"], "DL1691")
        self.assertEqual(links["label"], "Delta Airlines 1691")

    def test_unrecognized_airline_produces_no_link_at_all(self):
        # A guessed ident sends the dispatcher to a 404 (or worse, another
        # airline's flight). Better to show nothing.
        self.assertIsNone(tl.flight_tracker_urls("Alliegant", "2942", date(2026, 8, 8)))

    def test_missing_flight_number_produces_no_link(self):
        self.assertIsNone(tl.flight_tracker_urls("DL", "", date(2026, 8, 8)))

    def test_zero_padded_flight_numbers_are_bared_before_linking(self):
        # ~1 in 30 stored numbers is padded, because that's how it reads on a
        # boarding pass. Every tracker treats "0574" as no such flight.
        links = tl.flight_tracker_urls("WN", "0574", date(2026, 8, 8))
        self.assertEqual(links["ident"], "WN574")
        self.assertIn("flightNumber=574&", links["airline_url"])
        self.assertIn("/WN/574?", links["flightview"])
        self.assertEqual(links["label"], "Southwest Airlines 574")

    def test_no_date_still_yields_a_usable_flightview_link(self):
        links = tl.flight_tracker_urls("DL", "1691", None)
        self.assertEqual(links["flightview"],
                         "https://app.flightview.com/flight-tracker/DL/1691")

    def test_porter(self):
        links = tl.flight_tracker_urls("Porter", "580", date(2026, 8, 8))
        self.assertEqual(links["ident"], "POE580")
        self.assertEqual(links["flightview"],
                         "https://app.flightview.com/flight-tracker/PD/580?date=2026-08-08")
        self.assertEqual(links["label"], "Porter Airlines 580")
        # No verified status-page format for flyporter.com, so the menu shows
        # the two aggregators only.
        self.assertEqual(links["airline_url"], "")

    def test_discover(self):
        # 4Y is the one code here with a digit in it — it survives both the
        # ident build and the FlightView path.
        links = tl.flight_tracker_urls("Discover Airlines", "111", date(2026, 8, 8))
        self.assertEqual(links["ident"], "OCN111")
        self.assertEqual(links["flightview"],
                         "https://app.flightview.com/flight-tracker/4Y/111?date=2026-08-08")
        self.assertEqual(links["label"], "Discover Airlines 111")


class AirlineTrackerUrlTests(TestCase):
    """The carrier's own status page. Each format below is pinned against a
    real URL — a drifted format is a dispatcher staring at an error page while
    they believe they're looking at the flight."""

    DAY = date(2026, 8, 8)

    def test_delta(self):
        links = tl.flight_tracker_urls("DL", "1548", self.DAY)
        self.assertEqual(links["airline_site"], "Delta.com")
        self.assertEqual(links["airline_url"],
                         "https://www.delta.com/flightstatus/1/1548/2026-08-08")

    def test_american(self):
        links = tl.flight_tracker_urls("AA", "1228", self.DAY)
        self.assertEqual(
            links["airline_url"],
            "https://www.aa.com/travelInformation/flights/status/detail"
            "?search=AA%7C1228%7C2026,8,8&ref=search",
        )

    def test_jetblue(self):
        links = tl.flight_tracker_urls("B6", "670", self.DAY)
        self.assertEqual(
            links["airline_url"],
            "https://www.jetblue.com/flight-tracker-and-status"
            "?by=flight&number=670&date=2026-08-08",
        )

    def test_southwest(self):
        links = tl.flight_tracker_urls("WN", "2659", self.DAY)
        self.assertEqual(
            links["airline_url"],
            "https://www.southwest.com/air/flight-status/path"
            "?flightNumber=2659&departureDate=2026-08-08&searchType=flight",
        )

    def test_united_carries_the_route_in_the_url(self):
        links = tl.flight_tracker_urls(
            "UA", "2245", self.DAY,
            origin="IAH - George Bush Intercontinental",
            destination="MCO - Orlando Intl",
        )
        self.assertEqual(
            links["airline_url"],
            "https://www.united.com/en/us/flightstatus/details"
            "/2245/2026-08-08/IAH/MCO/UA",
        )

    def test_united_link_is_skipped_until_we_know_the_route(self):
        # AeroAPI fills origin/destination on refresh. Before that we cannot
        # build United's URL, and half a route in it resolves to nothing.
        links = tl.flight_tracker_urls("UA", "2245", self.DAY)
        self.assertEqual(links["airline_url"], "")
        self.assertTrue(links["flightaware"])

    def test_airport_codes_survive_both_storage_shapes(self):
        self.assertEqual(tl.airport_code("ORD - Chicago O'Hare Intl"), "ORD")
        self.assertEqual(tl.airport_code("MCO"), "MCO")
        self.assertEqual(tl.airport_code("KMCO"), "MCO")  # ICAO prefix stripped
        self.assertEqual(tl.airport_code(""), "")
        self.assertEqual(tl.airport_code("Terminal B"), "")

    def test_allegiant_gets_no_native_link(self):
        # Its status page keys off an opaque per-session token, so there is no
        # URL to build. Documented here so nobody "fixes" it with a guess.
        links = tl.flight_tracker_urls("G4", "1234", self.DAY)
        self.assertEqual(links["airline_url"], "")
        self.assertIn("AAY1234", links["flightaware"])

    def test_airline_written_as_a_name_still_reaches_its_own_site(self):
        links = tl.flight_tracker_urls("Southwest Airlines", "2659", self.DAY)
        self.assertIn("southwest.com", links["airline_url"])

    def test_carrier_without_a_verified_format_gets_no_native_link(self):
        # Spirit isn't in the table. Rather than guess at a URL, the menu shows
        # only the two aggregators.
        links = tl.flight_tracker_urls("NK", "123", self.DAY)
        self.assertEqual(links["airline_url"], "")
        self.assertEqual(links["airline_site"], "")
        self.assertTrue(links["flightaware"])

    def test_no_date_means_no_native_link(self):
        # Every one of these pages keys off the departure date; without one the
        # URL resolves to some other day's flight.
        links = tl.flight_tracker_urls("DL", "1548", None)
        self.assertEqual(links["airline_url"], "")


class LegTripLinksTests(TestCase):
    """The whole payload, built off a real leg."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=3, luggage_capacity=3)
        cls.rate = make_rate(cls.vehicle, "MCO", "Disney")
        cls.customer = Customer.objects.create(
            first_name="George", last_name="Marsh",
            email="george@example.com", phone_number="4075551234",
        )

    def _leg(self, **kwargs):
        reservation = Reservation.objects.create(
            customer=self.customer, vehicle=self.vehicle, rate=self.rate,
            trip_type="one-way",
            base_price=Decimal("100.00"), total_price=Decimal("100.00"),
        )
        defaults = dict(
            reservation=reservation,
            pickup_date=date(2026, 8, 8),
            pickup_time=time(6, 45),
            pickup_location="MCO - Orlando International Airport",
            dropoff_location="1251 Riverside Dr Lake Buena Vista FL 32830",
        )
        defaults.update(kwargs)
        return Leg.objects.create(**defaults)

    def test_simple_leg_maps_pickup_dropoff_and_route(self):
        leg = self._leg()
        data = tl.leg_trip_links(leg)
        self.assertIn("MCO", data["pickup"]["url"])
        self.assertIn("Riverside", data["dropoff"]["url"])
        self.assertIn("origin=MCO", data["route_url"])
        self.assertIn("destination=1251%20Riverside", data["route_url"])
        self.assertEqual(data["stops"], [])
        self.assertEqual(data["flights"], [])
        self.assertEqual(data["customer"], "George Marsh")

    def test_route_includes_the_grocery_stop_as_a_waypoint(self):
        leg = self._leg()
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="Publix Lake Buena Vista",
            stop_type="stop", duration_minutes=15,
        )
        data = tl.leg_trip_links(leg)
        self.assertIn("waypoints=Publix%20Lake%20Buena%20Vista%2C%20FL",
                      data["route_url"])
        self.assertEqual(len(data["stops"]), 1)
        self.assertEqual(data["stops"][0]["text"], "Publix Lake Buena Vista")

    def test_second_dropoff_becomes_the_end_of_the_route(self):
        # The booked drop-off is no longer where the trip ends — it's a
        # waypoint on the way to the second resort.
        leg = self._leg()
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="Disney Yacht Club",
            stop_type="dropoff",
        )
        data = tl.leg_trip_links(leg)
        self.assertIn("destination=Disney%20Yacht%20Club%2C%20FL", data["route_url"])
        self.assertIn("waypoints=1251%20Riverside", data["route_url"])

    def test_charter_stop_without_an_address_is_never_mapped(self):
        # display_location renders "Open destination — guest directs the
        # driver" for these. Handing that to Google is nonsense.
        leg = self._leg()
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="", stop_type="charter",
            duration_minutes=240, start_time=time(9, 0),
        )
        data = tl.leg_trip_links(leg)
        self.assertEqual(data["stops"], [])
        self.assertNotIn("waypoints=", data["route_url"])

    def test_structured_location_is_used_when_the_stop_matched_one(self):
        leg = self._leg()
        loc = Location.objects.create(name="Universal Studios")
        LegStop.objects.create(
            leg=leg, sequence=0, location_text="universal", location=loc,
            stop_type="stop",
        )
        data = tl.leg_trip_links(leg)
        self.assertEqual(data["stops"][0]["text"], "Universal Studios")

    def test_legacy_single_flight_still_gets_tracker_links(self):
        flight = Flight.objects.create(airline="DL", flight_number="1691",
                                       flight_type="arrival")
        leg = self._leg(flight_information=flight)
        data = tl.leg_trip_links(leg)
        self.assertEqual(len(data["flights"]), 1)
        self.assertEqual(data["flights"][0]["ident"], "DL1691")
        self.assertTrue(data["flights"][0]["is_controlling"])

    def test_flightview_anchors_on_the_departure_date_not_the_pickup_date(self):
        # Red-eye: takes off the 7th, lands (and is picked up) on the 8th.
        # FlightView indexes it under the 7th.
        flight = Flight.objects.create(airline="DL", flight_number="1691",
                                       departure_date=date(2026, 8, 7))
        leg = self._leg(flight_information=flight)
        data = tl.leg_trip_links(leg)
        self.assertIn("date=2026-08-07", data["flights"][0]["flightview"])

    def test_pickup_date_is_the_fallback_when_no_departure_date_is_set(self):
        flight = Flight.objects.create(airline="DL", flight_number="1691")
        leg = self._leg(flight_information=flight)
        data = tl.leg_trip_links(leg)
        self.assertIn("date=2026-08-08", data["flights"][0]["flightview"])

    def test_multi_flight_leg_lists_the_controlling_flight_first(self):
        leg = self._leg()
        early = Flight.objects.create(airline="AA", flight_number="100")
        late = Flight.objects.create(airline="UA", flight_number="200")
        LegFlight.objects.create(leg=leg, flight=early, sequence=0,
                                 is_controlling=False)
        LegFlight.objects.create(leg=leg, flight=late, sequence=1,
                                 is_controlling=True)
        data = tl.leg_trip_links(leg)
        self.assertEqual([f["ident"] for f in data["flights"]], ["UA200", "AA100"])
        self.assertTrue(data["flights"][0]["is_controlling"])

    def test_model_shortcuts_agree_with_the_builder(self):
        leg = self._leg()
        data = tl.leg_trip_links(leg)
        self.assertEqual(leg.pickup_map_url, data["pickup"]["url"])
        self.assertEqual(leg.dropoff_map_url, data["dropoff"]["url"])
        self.assertEqual(leg.route_map_url, data["route_url"])
        self.assertEqual(leg.flight_tracker_links, data["flights"])


class TripLinksEndpointTests(TestCase):
    """The JSON the right-click menu fetches."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            username="dispatcher", password="pw", is_staff=True)
        cls.outsider = User.objects.create_user(username="guest", password="pw")
        vehicle = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6)
        rate = make_rate(vehicle, "MCO Endpoint", "Grand Floridian")
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", phone_number="4075550000",
        )
        reservation = Reservation.objects.create(
            customer=customer, vehicle=vehicle, rate=rate, trip_type="one-way",
            base_price=Decimal("150.00"), total_price=Decimal("150.00"))
        cls.leg = Leg.objects.create(
            reservation=reservation,
            pickup_date=date(2026, 8, 8), pickup_time=time(14, 30),
            pickup_location="MCO - Orlando International Airport",
            dropoff_location="Disney Grand Floridian",
        )

    def test_staff_gets_the_links(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("leg_trip_links", args=[self.leg.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["leg_id"], self.leg.id)
        self.assertIn("google.com/maps", data["route_url"])
        # The two internal links the pure builder deliberately doesn't know about.
        self.assertIn(str(self.leg.reservation.uuid), data["reservation_url"])
        self.assertIn(str(self.leg.id), data["leg_history_url"])

    def test_non_staff_is_refused(self):
        self.client.force_login(self.outsider)
        resp = self.client.get(reverse("leg_trip_links", args=[self.leg.id]))
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        resp = self.client.get(reverse("leg_trip_links", args=[self.leg.id]))
        self.assertEqual(resp.status_code, 302)

    def test_unknown_leg_is_a_404_not_a_crash(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("leg_trip_links", args=[self.leg.id + 9999]))
        self.assertEqual(resp.status_code, 404)
