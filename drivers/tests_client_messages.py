"""
Standard guest texts + the communication KPIs built on them.

Invariants under test:
  * the copy ADAPTS to the pickup situation, and classification survives the
    holes in Leg.get_trip_type() (airport->airport, widened cruise keywords)
  * airport arrivals never promise a curbside pickup, and no message ever
    invents a vehicle colour
  * a tap is logged server-side with a server-rendered body
  * the rates count distinct legs, exclude what they say they exclude, and
    never let an affiliate score a phantom 0%

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_client_messages
"""

from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from drivers import comms_metrics
from drivers.client_messages import (
    ARRIVAL_TRACKED, ARRIVAL_UNTRACKED, CHARTER, CRUISE_FROM_PORT,
    CRUISE_TO_PORT_AIR, CRUISE_TO_PORT_LAND, DEPARTURE, KINDS, ON_LOCATION,
    ON_THE_WAY, OTHER, REVIEW, REVIEW_URL, build, classify, sms_href,
)
from drivers.models import Driver, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import (
    Cruise, Customer, Flight, Leg, LegClientMessage, LegStop, Reservation,
)

TRACK_FROM = date(2026, 1, 1)


def _make_driver(username, first="Marcus", last="Hale", driver_type="inhouse"):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    return Driver.objects.create(profile=user, driver_type=driver_type)


def _bootstrap_reservation(first="Jane", last="Carter", phone="4075550148"):
    vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
    origin = Location.objects.create(name="MCO")
    dest = Location.objects.create(name="Disney")
    route = Route.objects.create(origin=origin, destination=dest)
    rate = Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name=first, last_name=last, email="c@example.com", phone_number=phone,
    )
    reservation = Reservation.objects.create(
        trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
        base_price=Decimal("100"), total_price=Decimal("100"),
    )
    return reservation


def _make_leg(reservation, driver=None, *, pickup="MCO", dropoff="Disney",
              pickup_date=None, status="confirmed", pickup_time_=time(9, 0)):
    return Leg.objects.create(
        reservation=reservation, driver=driver,
        pickup_date=pickup_date or timezone.localdate(),
        pickup_time=pickup_time_,
        pickup_location=pickup, dropoff_location=dropoff, status=status,
    )


@override_settings(GOOGLE_MAPS_API_KEY="")
class ClassifyTests(TestCase):
    """Situation detection, including the cases get_trip_type() gets wrong."""

    @classmethod
    def setUpTestData(cls):
        cls.res = _bootstrap_reservation()

    def _leg(self, pickup, dropoff):
        return _make_leg(self.res, pickup=pickup, dropoff=dropoff)

    def test_airport_pickup_without_flight_is_untracked_arrival(self):
        self.assertEqual(classify(self._leg("MCO", "Disney")), ARRIVAL_UNTRACKED)

    def test_airport_pickup_with_arrival_time_is_tracked(self):
        leg = self._leg("MCO", "Disney")
        leg.flight_information = Flight.objects.create(
            airline="DL", flight_number="1423",
            scheduled_gate_arrival_local=timezone.now(),
        )
        leg.save(update_fields=["flight_information"])
        self.assertEqual(classify(leg), ARRIVAL_TRACKED)

    def test_dropoff_at_airport_is_departure(self):
        self.assertEqual(classify(self._leg("Disney", "MCO")), DEPARTURE)

    def test_airport_to_airport_is_an_arrival_not_other(self):
        """get_trip_type() calls MCO->SFB 'other', which would send lobby copy
        to a guest standing in a terminal."""
        leg = self._leg("MCO", "SFB")
        self.assertEqual(leg.get_trip_type(), "other")
        self.assertEqual(classify(leg), ARRIVAL_UNTRACKED)

    def test_cruise_port_pickup_is_debarkation(self):
        leg = self._leg("Port Canaveral Cruise Terminal", "Disney")
        self.assertEqual(classify(leg), CRUISE_FROM_PORT)

    def test_cruise_from_port_to_airport_still_debarkation(self):
        leg = self._leg("Port Canaveral Cruise Terminal", "MCO")
        self.assertEqual(classify(leg), CRUISE_FROM_PORT)

    def test_airport_to_port_is_embarkation_from_air(self):
        leg = self._leg("MCO", "Port Canaveral Cruise Terminal")
        self.assertEqual(classify(leg), CRUISE_TO_PORT_AIR)

    def test_hotel_to_port_is_embarkation_from_land(self):
        leg = self._leg("Hyatt Regency Grand Cypress", "Port Canaveral Cruise Terminal")
        self.assertEqual(classify(leg), CRUISE_TO_PORT_LAND)

    def test_widened_port_keywords_catch_cove_terminal(self):
        """Leg.CRUISE_PORT_KEYWORDS has no 'cove terminal' and no 'jetty park'.
        Written without the word "canaveral" — whose bare substring would
        otherwise catch it — a real port pickup reads as 'other' to
        get_trip_type() and would draw lobby copy. The widened list still
        calls it a cruise."""
        leg = self._leg("Cove Terminal", "Disney")
        self.assertEqual(leg.get_trip_type(), "other")
        self.assertEqual(classify(leg), CRUISE_FROM_PORT)

    def test_canaveral_spelling_also_classifies_as_cruise(self):
        leg = self._leg("Cove Terminal, Cape Canaveral", "Disney")
        self.assertEqual(leg.get_trip_type(), "cruise")
        self.assertEqual(classify(leg), CRUISE_FROM_PORT)

    def test_point_to_point_is_other(self):
        self.assertEqual(classify(self._leg("Hyatt Regency", "Universal Studios")), OTHER)

    def test_hourly_booking_is_charter(self):
        leg = self._leg("Hyatt Regency", "Universal Studios")
        LegStop.objects.create(
            leg=leg, sequence=0, stop_type="charter",
            duration_minutes=240, start_time=time(9, 0),
        )
        self.assertEqual(classify(leg), CHARTER)


@override_settings(GOOGLE_MAPS_API_KEY="")
class CopyTests(TestCase):
    """What the guest actually reads."""

    @classmethod
    def setUpTestData(cls):
        cls.res = _bootstrap_reservation(first="jane")

    def _leg(self, pickup="MCO", dropoff="Disney", **kw):
        return _make_leg(self.res, pickup=pickup, dropoff=dropoff, **kw)

    def _all_bodies(self):
        """Every kind x every situation — for blanket invariants. Tagged with
        (situation, kind) so a test can carve out a known exception."""
        specs = [
            ("MCO", "Disney"),
            ("Disney", "MCO"),
            ("MCO", "Port Canaveral Cruise Terminal"),
            ("Hyatt Regency", "Port Canaveral Cruise Terminal"),
            ("Port Canaveral Cruise Terminal", "Disney"),
            ("Hyatt Regency", "Universal Studios"),
        ]
        out = []
        for pickup, dropoff in specs:
            leg = self._leg(pickup, dropoff)
            for kind in KINDS:
                msg = build(leg, kind, driver_name="Marcus")
                out.append((msg.situation, kind, msg.body))
        return out

    def test_every_message_names_the_guest(self):
        for _, _, body in self._all_bodies():
            self.assertIn("Jane", body, body)

    def test_every_message_names_the_company(self):
        for _, _, body in self._all_bodies():
            self.assertIn("Grayson Towncar", body, body)

    def test_no_message_ever_promises_a_curbside_airport_pickup(self):
        for _, _, body in self._all_bodies():
            self.assertNotIn("curb", body.lower(), body)

    def test_no_message_invents_a_vehicle_colour(self):
        """No vehicle model in this system stores a colour."""
        for _, _, body in self._all_bodies():
            low = body.lower()
            for colour in ("black ", "white ", "silver ", "grey ", "gray "):
                self.assertNotIn(colour, low, body)

    def test_arrival_meets_at_the_baggage_claim_area_with_a_sign(self):
        leg = self._leg("MCO", "Disney")
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("baggage claim area", body)
        self.assertIn("sign with your name", body)

    def test_mco_arrival_names_its_verified_meet_point(self):
        """MCO has a specific, verified floor/landmark — see
        client_messages._MEET_POINT_BY_AIRPORT_CODE."""
        leg = self._leg("MCO", "Disney")
        for kind in (ON_THE_WAY, ON_LOCATION):
            body = build(leg, kind, driver_name="Marcus").body
            self.assertIn("2nd floor", body, body)
            self.assertIn("escalators", body, body)
            self.assertIn("information desk", body, body)

    def test_sfb_arrival_names_its_own_verified_meet_point(self):
        leg = self._leg("SFB", "Disney")
        for kind in (ON_THE_WAY, ON_LOCATION):
            body = build(leg, kind, driver_name="Marcus").body
            self.assertIn("level 1", body, body)
            self.assertIn("escalator or elevator", body, body)
            self.assertIn("information desk", body, body)

    def test_mco_terminal_c_gets_its_own_meet_point(self):
        """Terminal C (JetBlue and others) has a different physical layout
        from MCO's main terminal — Level 6 baggage claim, vehicle a floor
        down — keyed off Flight.terminal, which AeroAPI populates per
        flight, not guessed from the airline."""
        leg = self._leg("MCO", "Disney")
        leg.flight_information = Flight.objects.create(
            airline_display_name="JetBlue", flight_number="670", terminal="C",
        )
        leg.save(update_fields=["flight_information"])
        for kind in (ON_THE_WAY, ON_LOCATION):
            body = build(leg, kind, driver_name="Marcus").body
            self.assertIn("level 6", body, body)
            self.assertIn("escalators and elevators", body, body)
            self.assertNotIn("2nd floor", body, body)

    def test_mco_without_known_terminal_falls_back_to_the_main_terminal(self):
        """No flight, or a flight whose terminal AeroAPI hasn't reported yet
        — never guess "C", fall back to the verified main-terminal point."""
        leg = self._leg("MCO", "Disney")
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("2nd floor", body, body)

        leg.flight_information = Flight.objects.create(
            airline_display_name="Delta", flight_number="123", terminal="",
        )
        leg.save(update_fields=["flight_information"])
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("2nd floor", body, body)

    def test_unverified_airport_gets_the_plain_baggage_claim_area(self):
        """Melbourne and Lakeland have no verified meet-point instructions on
        file — the copy must not invent a floor or landmark for them."""
        leg = self._leg("MLB", "Disney")
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("baggage claim area", body)
        self.assertNotIn("2nd floor", body)
        self.assertNotIn("level 1", body)

    def test_arrival_does_not_name_the_flight_or_landing_time(self):
        """As of the 2026-08-31 rewrite the guest-facing copy no longer
        quotes a flight number or landing time, even when one is known —
        classify() still tracks tracked-vs-untracked internally, but the
        rendered text is the same either way."""
        leg = self._leg("MCO", "Disney")
        leg.flight_information = Flight.objects.create(
            airline_display_name="Delta", flight_number="1423",
            scheduled_gate_arrival_local=timezone.now(),
        )
        leg.save(update_fields=["flight_information"])
        self.assertEqual(classify(leg), ARRIVAL_TRACKED)
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertNotIn("Delta", body)
        self.assertNotIn("1423", body)
        self.assertNotIn("landing", body.lower())

    def test_cruise_from_the_airport_reads_exactly_like_a_plain_arrival(self):
        """Explicit product requirement: a cruise guest arriving by air gets
        the same on-the-way/on-location wording as a plain airport arrival —
        no mention of the cruise or the port in either message."""
        plain_arrival = self._leg("MCO", "Disney")
        cruise_from_air = self._leg("MCO", "Port Canaveral Cruise Terminal")
        for kind in (ON_THE_WAY, ON_LOCATION):
            plain_body = build(plain_arrival, kind, driver_name="Marcus").body
            cruise_body = build(cruise_from_air, kind, driver_name="Marcus").body
            self.assertEqual(plain_body, cruise_body)

    def test_departure_quotes_the_pickup_time_and_the_airport(self):
        leg = self._leg("Disney", "MCO", pickup_time_=time(6, 15))
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("6:15 AM", body)
        self.assertIn("Orlando International Airport", body)
        # The internal (MCO) shorthand is for staff pages, not guest copy.
        self.assertNotIn("(MCO)", body)

    def test_departure_on_the_way_greets_by_time_of_day_and_names_the_pickup(self):
        leg = self._leg("Hyatt Regency Grand Cypress", "MCO", pickup_time_=time(6, 15))
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("Good morning, Jane!", body)
        self.assertIn("This is Marcus with Grayson Towncar", body)
        self.assertIn("Hyatt Regency Grand Cypress", body)

    def test_departure_never_mentions_baggage_claim(self):
        body = build(self._leg("Disney", "MCO"), ON_THE_WAY, driver_name="Marcus").body
        self.assertNotIn("baggage claim", body)

    def test_departure_on_location_greets_by_time_of_day_and_names_the_pickup(self):
        leg = self._leg("Main entrance of Disney's Animal Kingdom Lodge", "MCO")
        body = build(leg, ON_LOCATION, driver_name="Marcus").body
        self.assertIn("Good morning, Jane!", body)
        self.assertIn("Main entrance of Disney's Animal Kingdom Lodge", body)
        self.assertIn("send me a quick message", body.lower())

    def test_greeting_follows_the_booked_pickup_time_not_the_clock(self):
        """The daypart greeting is keyed off leg.pickup_time — an evening
        booking reads 'Good evening' regardless of when the page happens to
        render."""
        leg = self._leg("Disney", "MCO", pickup_time_=time(18, 15))
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("Good evening, Jane!", body)

    def test_debarkation_waits_at_the_named_terminal_and_mentions_customs(self):
        leg = self._leg("Port Canaveral Cruise Terminal", "Disney")
        leg.cruise_information = Cruise.objects.create(
            cruise_line="Royal Caribbean", ship_name="Wonder of the Seas",
        )
        leg.save(update_fields=["cruise_information"])
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("Royal Caribbean terminal", body)
        self.assertIn("customs", body)

    def test_embarkation_from_hotel_names_the_port(self):
        leg = self._leg("Hyatt Regency", "Port Canaveral Cruise Terminal")
        leg.cruise_information = Cruise.objects.create(cruise_line="Disney Cruise Line")
        leg.save(update_fields=["cruise_information"])
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("Disney Cruise Line terminal at Port Canaveral", body)

    def test_charter_says_the_guest_directs_the_day(self):
        leg = self._leg("Hyatt Regency", "Universal Studios")
        LegStop.objects.create(
            leg=leg, sequence=0, stop_type="charter",
            duration_minutes=240, start_time=time(9, 0),
        )
        body = build(leg, ON_THE_WAY, driver_name="Marcus").body
        self.assertIn("chauffeur for the day", body)

    def test_the_three_kinds_differ_from_each_other(self):
        leg = self._leg("MCO", "Disney")
        bodies = {build(leg, k, driver_name="Marcus").body for k in KINDS}
        self.assertEqual(len(bodies), 3)

    def test_situations_produce_different_on_the_way_copy(self):
        specs = [("MCO", "Disney"), ("Disney", "MCO"),
                 ("Port Canaveral Cruise Terminal", "Disney"),
                 ("Hyatt Regency", "Universal Studios")]
        bodies = {
            build(self._leg(p, d), ON_THE_WAY, driver_name="Marcus").body
            for p, d in specs
        }
        self.assertEqual(len(bodies), len(specs))

    def test_review_carries_the_single_review_url(self):
        body = build(self._leg(), REVIEW, driver_name="Marcus").body
        self.assertIn(REVIEW_URL, body)

    def test_review_closing_matches_the_journey(self):
        self.assertIn("Enjoy your stay", build(self._leg("MCO", "Disney"), REVIEW).body)
        self.assertIn("Safe travels", build(self._leg("Disney", "MCO"), REVIEW).body)
        self.assertIn(
            "Welcome back",
            build(self._leg("Port Canaveral Cruise Terminal", "Disney"), REVIEW).body,
        )

    def test_driver_name_is_used_and_degrades_when_missing(self):
        leg = self._leg()
        self.assertIn("Marcus", build(leg, ON_THE_WAY, driver_name="Marcus").body)
        self.assertNotIn("None", build(leg, ON_THE_WAY).body)

    def test_vehicle_clause_uses_make_and_model_only(self):
        leg = self._leg("Disney", "Universal Studios")
        car = FleetVehicle.objects.create(
            vehicle_number="GT-9", year=2022, make="Chevrolet", model="Suburban",
        )
        body = build(leg, ON_LOCATION, driver_name="Marcus", vehicle=car).body
        self.assertIn("Chevrolet Suburban", body)
        self.assertNotIn("2022", body)

    def test_guest_first_name_is_title_cased(self):
        self.assertIn("Jane", build(self._leg(), ON_THE_WAY).body)


class SmsHrefTests(TestCase):
    def test_body_is_percent_encoded(self):
        """Phone normalization goes through drivers.sms.normalize_e164, same
        as every other outbound number — a 10-digit US number gets its +1."""
        href = sms_href("(407) 555-0148", "Hi Jane, it's Marcus & co.")
        self.assertTrue(href.startswith("sms:+14075550148?body="))
        self.assertNotIn(" ", href)
        self.assertIn("%20", href)
        self.assertIn("%26", href)

    def test_newlines_survive_encoding(self):
        self.assertIn("%0A", sms_href("4075550148", "one\ntwo"))

    def test_missing_phone_does_not_crash(self):
        self.assertTrue(sms_href(None, "hi").startswith("sms:?body="))


@override_settings(GOOGLE_MAPS_API_KEY="")
class LogEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("log_driver")
        cls.other = _make_driver("other_driver", first="Sam")
        cls.res = _bootstrap_reservation()
        cls.leg = _make_leg(cls.res, cls.driver, status="on-the-way")

    def setUp(self):
        self.client.force_login(self.driver.profile)

    def _url(self, leg=None):
        return reverse("driver_log_client_message", args=[(leg or self.leg).id])

    def test_tap_is_recorded_with_a_server_rendered_body(self):
        resp = self.client.post(self._url(), {"kind": ON_THE_WAY})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

        row = LegClientMessage.objects.get()
        self.assertEqual(row.leg, self.leg)
        self.assertEqual(row.driver, self.driver)
        self.assertEqual(row.sent_by, self.driver.profile)
        self.assertEqual(row.kind, ON_THE_WAY)
        self.assertEqual(row.situation, ARRIVAL_UNTRACKED)
        self.assertIn("Grayson Towncar", row.body)

    def test_client_supplied_body_is_ignored(self):
        self.client.post(self._url(), {"kind": REVIEW, "body": "anything I like"})
        self.assertNotIn("anything I like", LegClientMessage.objects.get().body)

    def test_unknown_kind_is_rejected(self):
        resp = self.client.post(self._url(), {"kind": "nope"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(LegClientMessage.objects.exists())

    def test_another_drivers_leg_is_not_reachable(self):
        theirs = _make_leg(self.res, self.other)
        self.assertEqual(self.client.post(self._url(theirs), {"kind": ON_THE_WAY}).status_code, 404)
        self.assertFalse(LegClientMessage.objects.exists())

    def test_anonymous_is_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.post(self._url(), {"kind": ON_THE_WAY}).status_code, 302)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    def test_resends_each_write_a_row(self):
        for _ in range(3):
            self.client.post(self._url(), {"kind": ON_THE_WAY})
        self.assertEqual(LegClientMessage.objects.count(), 3)


@override_settings(GOOGLE_MAPS_API_KEY="")
class MetricsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("kpi_driver")
        cls.affiliate = _make_driver("kpi_affiliate", first="Ana", driver_type="affiliate")
        cls.res = _bootstrap_reservation()
        cls.start = TRACK_FROM
        cls.end = TRACK_FROM + timedelta(days=30)

    def _completed(self, day_offset=1, driver=None):
        return _make_leg(
            self.res, driver or self.driver,
            pickup_date=TRACK_FROM + timedelta(days=day_offset),
            status="completed",
        )

    def _stats(self, driver=None):
        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            return comms_metrics.comms_stats(driver or self.driver, self.start, self.end)

    def test_rate_is_sent_over_completed_trips(self):
        legs = [self._completed(i) for i in range(1, 5)]
        for leg in legs[:3]:
            LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=ON_THE_WAY)
        stats = self._stats()
        self.assertEqual(stats[ON_THE_WAY]["sent"], 3)
        self.assertEqual(stats[ON_THE_WAY]["eligible"], 4)
        self.assertEqual(stats[ON_THE_WAY]["pct"], 75)

    def test_resends_count_the_leg_once(self):
        leg = self._completed()
        for _ in range(4):
            LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=ON_THE_WAY)
        stats = self._stats()
        self.assertEqual(stats[ON_THE_WAY]["sent"], 1)
        self.assertEqual(stats[ON_THE_WAY]["pct"], 100)

    def test_kinds_are_counted_separately(self):
        leg = self._completed()
        LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=REVIEW)
        stats = self._stats()
        self.assertEqual(stats[REVIEW]["pct"], 100)
        self.assertEqual(stats[ON_THE_WAY]["pct"], 0)

    def test_uncompleted_trips_are_not_in_the_denominator(self):
        self._completed()
        _make_leg(self.res, self.driver,
                  pickup_date=TRACK_FROM + timedelta(days=2), status="on-the-way")
        self.assertEqual(self._stats()[ON_THE_WAY]["eligible"], 1)

    def test_cancelled_reservations_are_excluded(self):
        leg = self._completed()
        res2 = _bootstrap_reservation()
        res2.status = "cancelled"
        res2.save(update_fields=["status"])
        _make_leg(res2, self.driver,
                  pickup_date=TRACK_FROM + timedelta(days=2), status="completed")
        self.assertEqual(self._stats()[ON_THE_WAY]["eligible"], 1)
        self.assertEqual(leg.status, "completed")

    def test_trips_before_tracking_started_are_not_counted(self):
        _make_leg(self.res, self.driver,
                  pickup_date=TRACK_FROM - timedelta(days=5), status="completed")
        self._completed()
        self.assertEqual(self._stats()[ON_THE_WAY]["eligible"], 1)

    def test_affiliates_are_excluded_entirely(self):
        self._completed(driver=self.affiliate)
        stats = self._stats(self.affiliate)
        self.assertEqual(stats[ON_THE_WAY]["eligible"], 0)
        self.assertIsNone(stats[ON_THE_WAY]["pct"])

    def test_no_trips_reads_as_no_rate_not_zero(self):
        self.assertIsNone(self._stats()[ON_THE_WAY]["pct"])

    def test_bulk_matches_single_and_isolates_drivers(self):
        mine = self._completed()
        LegClientMessage.objects.create(leg=mine, driver=self.driver, kind=ON_THE_WAY)
        mate = _make_driver("kpi_mate", first="Lee")
        self._completed(2, driver=mate)
        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            bulk = comms_metrics.comms_stats_bulk(
                [self.driver.id, mate.id], self.start, self.end
            )
        self.assertEqual(bulk[self.driver.id][ON_THE_WAY]["pct"], 100)
        self.assertEqual(bulk[mate.id][ON_THE_WAY]["pct"], 0)

    def test_reassignment_does_not_rewrite_who_gets_credit(self):
        """LegClientMessage.driver is denormalized specifically so a later
        reassignment can't rewrite history (see the model's own docstring).
        The chauffeur who actually tapped the message must keep the credit
        even after the leg is handed to someone else."""
        leg = self._completed()
        LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=ON_THE_WAY)
        mate = _make_driver("kpi_reassigned_to", first="Lee")
        leg.driver = mate
        leg.save(update_fields=["driver"])

        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            bulk = comms_metrics.comms_stats_bulk(
                [self.driver.id, mate.id], self.start, self.end
            )
        # The original chauffeur still gets credit for the tap he made...
        self.assertEqual(bulk[self.driver.id][ON_THE_WAY]["sent"], 1)
        # ...and the new driver, who never touched the button, does not.
        self.assertEqual(bulk[mate.id][ON_THE_WAY]["sent"], 0)

    def test_window_ends_today_and_is_clamped_to_tracking_start(self):
        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            start, end = comms_metrics.window_bounds(365, today=TRACK_FROM + timedelta(days=10))
        self.assertEqual(end, TRACK_FROM + timedelta(days=10))
        self.assertEqual(start, TRACK_FROM)

    def test_todays_completed_trip_counts_toward_todays_rate(self):
        """A tap on a trip completed earlier today must not wait until
        tomorrow — it's eligible and counted the same day."""
        today = TRACK_FROM + timedelta(days=5)
        leg = _make_leg(self.res, self.driver, pickup_date=today, status="completed")
        LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=ON_THE_WAY)
        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            start, end = comms_metrics.window_bounds(7, today=today)
            stats = comms_metrics.comms_stats(self.driver, start, end)
        self.assertEqual(stats[ON_THE_WAY]["eligible"], 1)
        self.assertEqual(stats[ON_THE_WAY]["sent"], 1)
        self.assertEqual(stats[ON_THE_WAY]["pct"], 100)

    def test_window_resolution_falls_back_to_default(self):
        self.assertEqual(comms_metrics.resolve_window("junk")[0], comms_metrics.WINDOW_DEFAULT)
        self.assertEqual(comms_metrics.resolve_window("7"), ("7", 7))

    def test_accents_track_the_thresholds(self):
        tiles = {t["kind"]: t for t in comms_metrics.as_tiles({
            ON_THE_WAY: {"sent": 9, "eligible": 10, "pct": 90},
            ON_LOCATION: {"sent": 6, "eligible": 10, "pct": 60},
            REVIEW: {"sent": 1, "eligible": 10, "pct": 10},
        })}
        self.assertEqual(tiles[ON_THE_WAY]["accent"], "green")
        self.assertEqual(tiles[ON_LOCATION]["accent"], "amber")
        self.assertEqual(tiles[REVIEW]["accent"], "red")


@override_settings(GOOGLE_MAPS_API_KEY="")
class DriverCardTests(TestCase):
    """The buttons as the chauffeur sees them."""

    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("card_driver")
        cls.res = _bootstrap_reservation()
        cls.leg = _make_leg(cls.res, cls.driver, status="on-the-way")

    def setUp(self):
        self.client.force_login(self.driver.profile)

    def test_schedule_card_offers_all_three_texts(self):
        html = self.client.get(reverse("schedule")).content.decode()
        for kind in KINDS:
            self.assertIn(f'data-msg-kind="{kind}"', html)

    def test_day_card_offers_all_three_texts(self):
        html = self.client.get(reverse("drivers_dashboard")).content.decode()
        for kind in KINDS:
            self.assertIn(f'data-msg-kind="{kind}"', html)

    def test_hardcoded_review_link_is_gone_from_both_cards(self):
        for name in ("schedule", "drivers_dashboard"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertNotIn("It was a pleasure being at your service today", html)

    def test_review_url_still_reaches_the_guest(self):
        html = self.client.get(reverse("schedule")).content.decode()
        self.assertIn("g.page", html)

    def test_sent_messages_render_as_sent(self):
        LegClientMessage.objects.create(leg=self.leg, driver=self.driver, kind=ON_THE_WAY)
        html = self.client.get(reverse("schedule")).content.decode()
        self.assertIn("is-sent", html)

    def test_affiliate_chauffeur_card_shows_guest_texts(self):
        """An affiliate who drives his own jobs (driver_type="affiliate",
        portal_role="driver", the default) gets the same job card as an
        in-house chauffeur — including a customer phone number — so he keeps
        the same texting buttons, same as the single hardcoded "Request
        Review" link every driver had before this feature existed. Only a
        true operator (portal_role="operator"), who never reaches this page
        at all, is excluded."""
        affiliate = _make_driver("card_affiliate", first="Ana", driver_type="affiliate")
        _make_leg(self.res, affiliate, status="on-the-way")
        self.client.force_login(affiliate.profile)
        html = self.client.get(reverse("schedule")).content.decode()
        self.assertIn('data-msg-kind="', html)


@override_settings(GOOGLE_MAPS_API_KEY="")
class KpiPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("page_driver")
        cls.staff = User.objects.create_user("page_staff", password="x", is_staff=True)
        cls.admin = User.objects.create_user(
            "page_admin", password="x", is_staff=True, is_superuser=True
        )

    def test_profile_shows_the_communication_panel(self):
        self.client.force_login(self.staff)
        html = self.client.get(
            reverse("driver_profile", args=[self.driver.id])
        ).content.decode()
        self.assertIn("Guest Communication", html)

    def test_profile_explains_itself_once_rates_are_possible(self):
        """With a window that can hold data, the panel must say plainly that it
        counts a text SENT FROM THE APP, not a delivery."""
        self.client.force_login(self.staff)
        with override_settings(CLIENT_MESSAGE_TRACKING_START=TRACK_FROM):
            html = self.client.get(
                reverse("driver_profile", args=[self.driver.id])
            ).content.decode()
        self.assertIn("sent from the driver app", html)
        self.assertNotIn("No rates yet", html)

    def test_profile_says_why_there_are_no_rates_before_launch_day(self):
        """A grid of dashes reads as 'nobody is doing it'. Before tracking begins
        it means 'no data could exist yet', and the page has to say which."""
        self.client.force_login(self.staff)
        with override_settings(
            CLIENT_MESSAGE_TRACKING_START=timezone.localdate() + timedelta(days=1)
        ):
            html = self.client.get(
                reverse("driver_profile", args=[self.driver.id])
            ).content.decode()
        self.assertIn("No rates yet", html)

    def test_profile_has_normal_rates_on_launch_day_itself(self):
        """Launch day is a real, valid window (today counts) — it just starts
        with zero eligible trips, same as any driver with no completed trips
        yet. That is the ordinary dash state, not the 'no data could exist'
        banner."""
        self.client.force_login(self.staff)
        with override_settings(CLIENT_MESSAGE_TRACKING_START=timezone.localdate()):
            html = self.client.get(
                reverse("driver_profile", args=[self.driver.id])
            ).content.decode()
        self.assertNotIn("No rates yet", html)
        self.assertIn("sent from the driver app", html)

    def test_admin_page_says_why_there_are_no_rates_before_launch_day(self):
        self.client.force_login(self.admin)
        with override_settings(
            CLIENT_MESSAGE_TRACKING_START=timezone.localdate() + timedelta(days=1)
        ):
            html = self.client.get(reverse("driver_comms_kpis")).content.decode()
        self.assertIn("No rates yet", html)
        self.assertIn("Texts logged", html)

    def test_admin_activity_counter_moves_on_a_tap(self):
        """The raw counter must ignore every window and completion rule — it is
        the only proof the system is recording during the first days."""
        res = _bootstrap_reservation()
        leg = _make_leg(res, self.driver, status="on-the-way")
        LegClientMessage.objects.create(leg=leg, driver=self.driver, kind=ON_THE_WAY)
        self.client.force_login(self.admin)
        activity = self.client.get(reverse("driver_comms_kpis")).context["activity"]
        self.assertEqual(activity["today"], 1)
        self.assertEqual(activity["total"], 1)

    def test_profile_window_selector_is_honoured(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("driver_profile", args=[self.driver.id]), {"comms": "7"}
        )
        self.assertEqual(resp.context["comms_window"], "7")

    def test_profile_hides_the_panel_for_affiliates(self):
        affiliate = _make_driver("page_affiliate", first="Ana", driver_type="affiliate")
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("driver_profile", args=[affiliate.id]))
        self.assertIsNone(resp.context["comms_tiles"])
        self.assertNotIn("Guest Communication", resp.content.decode())

    def test_admin_page_renders_for_a_superuser(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("driver_comms_kpis"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Guest Communication", resp.content.decode())

    def test_admin_page_is_closed_to_plain_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("driver_comms_kpis")).status_code, 302)

    def test_admin_page_is_closed_to_drivers(self):
        self.client.force_login(self.driver.profile)
        self.assertEqual(self.client.get(reverse("driver_comms_kpis")).status_code, 302)

    def test_admin_page_lists_inhouse_only(self):
        _make_driver("page_affiliate2", first="Ana", driver_type="affiliate")
        self.client.force_login(self.admin)
        rows = self.client.get(reverse("driver_comms_kpis")).context["rows"]
        self.assertTrue(all(r["driver"].driver_type == "inhouse" for r in rows))
