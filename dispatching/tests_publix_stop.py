"""Publix grocery-stop rules.

1. Hours guard: no store stop while Publix is closed (pickups 9 PM-6 AM) —
   hard 'error' severity on the dispatcher wizard, form error on the public
   booking form, ValueError backstop at reservation creation.
2. Display: the Publix badge belongs ONLY to the leg the stop rides on
   (Leg.shows_store_stop) — never the departure/return leg of the same
   reservation.
"""
import re
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from dispatching.booking_guards import (
    PUBLIX_CLOSED_END_HOUR,
    PUBLIX_CLOSED_START_HOUR,
    check_publix_store_stop,
    publix_closed_at,
)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

User = get_user_model()

MCO = "Orlando International Airport (MCO)"
RESORT = "Disney Pop Century Resort"
HOME = "1234 Maple St, Kissimmee"
PORT = "Port Canaveral Cruise Terminal 6"


def _leg(pickup_date, pickup_time, pickup=MCO, dropoff=RESORT):
    return {
        "pickup_date": pickup_date.isoformat() if isinstance(pickup_date, date) else pickup_date,
        "pickup_time": pickup_time,
        "pickup_location": pickup,
        "dropoff_location": dropoff,
        "private_notes": "",
    }


class PublixClosedAtTests(TestCase):
    def test_window_boundaries(self):
        self.assertEqual(PUBLIX_CLOSED_START_HOUR, 21)
        self.assertEqual(PUBLIX_CLOSED_END_HOUR, 6)
        self.assertFalse(publix_closed_at("20:59"))       # last open minute
        self.assertTrue(publix_closed_at("21:00"))        # 9 PM sharp blocks
        self.assertTrue(publix_closed_at("23:45:00"))
        self.assertTrue(publix_closed_at("00:15"))        # after midnight
        self.assertTrue(publix_closed_at(time(5, 59)))
        self.assertFalse(publix_closed_at(time(6, 0)))    # 6 AM opens the window
        self.assertFalse(publix_closed_at("12:00"))

    def test_unparseable_time_is_not_blocked(self):
        self.assertFalse(publix_closed_at(None))
        self.assertFalse(publix_closed_at(""))
        self.assertFalse(publix_closed_at("bogus"))


class CheckPublixStoreStopTests(TestCase):
    def setUp(self):
        self.future = django_timezone.localdate() + timedelta(days=30)

    def test_no_store_stop_never_errors(self):
        self.assertEqual(
            check_publix_store_stop([_leg(self.future, "22:00:00")], store_stop=False), []
        )

    def test_daytime_arrival_passes(self):
        self.assertEqual(
            check_publix_store_stop([_leg(self.future, "14:30:00")], store_stop=True), []
        )

    def test_closed_hours_arrival_errors(self):
        out = check_publix_store_stop([_leg(self.future, "22:00:00")], store_stop=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "publix_closed")
        self.assertEqual(out[0]["severity"], "error")
        self.assertEqual(out[0]["leg"], 1)
        self.assertIn("10:00 PM", out[0]["message"])

    def test_stop_rides_on_the_airport_leg_not_leg_one(self):
        # Leg 1 is a daytime hotel transfer; leg 2 is the late airport arrival.
        legs = [
            _leg(self.future, "10:00:00", pickup=RESORT, dropoff=HOME),
            _leg(self.future + timedelta(days=1), "22:30:00", pickup=MCO, dropoff=RESORT),
        ]
        out = check_publix_store_stop(legs, store_stop=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["leg"], 2)

    def test_late_return_leg_does_not_block(self):
        # Arrival at noon carries the stop; the 10 PM ride TO the airport is fine.
        legs = [
            _leg(self.future, "12:00:00"),
            _leg(self.future + timedelta(days=7), "22:00:00", pickup=RESORT, dropoff=MCO),
        ]
        self.assertEqual(check_publix_store_stop(legs, store_stop=True), [])

    def test_no_airport_leg_falls_back_to_leg_one(self):
        legs = [_leg(self.future, "23:00:00", pickup=HOME, dropoff=RESORT)]
        out = check_publix_store_stop(legs, store_stop=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["leg"], 1)

    def test_missing_time_does_not_crash_or_block(self):
        legs = [_leg(self.future, None)]
        self.assertEqual(check_publix_store_stop(legs, store_stop=True), [])


class ShowsStoreStopTests(TestCase):
    """The Publix badge belongs only to the leg the stop actually rides on."""

    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(
            first_name="Badge", last_name="Test",
            email="badge@example.com", phone_number="4070000001",
        )
        origin = Location.objects.create(name="Orlando International Airport")
        destination = Location.objects.create(name="Disney Property")
        route = Route.objects.create(origin=origin, destination=destination)
        vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("120"), round_trip_price=Decimal("220"),
        )
        cls.future = django_timezone.localdate() + timedelta(days=30)

    def _res(self, store_stop=True):
        return Reservation.objects.create(
            customer=self.customer,
            rate=self.rate,
            trip_type="round_trip",
            base_price=Decimal("100"),
            total_price=Decimal("100"),
            store_stop=store_stop,
        )

    def _mk_leg(self, res, pickup, dropoff, days=0, at=time(12, 0)):
        return Leg.objects.create(
            reservation=res,
            pickup_date=self.future + timedelta(days=days),
            pickup_time=at,
            pickup_location=pickup,
            dropoff_location=dropoff,
        )

    def test_round_trip_badges_arrival_only(self):
        res = self._res()
        arrival = self._mk_leg(res, MCO, RESORT)
        ret = self._mk_leg(res, RESORT, MCO, days=7)
        self.assertTrue(arrival.shows_store_stop)
        self.assertFalse(ret.shows_store_stop)  # the reported bug

    def test_no_store_stop_no_badge_anywhere(self):
        res = self._res(store_stop=False)
        arrival = self._mk_leg(res, MCO, RESORT)
        self.assertFalse(arrival.shows_store_stop)

    def test_airport_to_cruise_port_counts_as_the_grocery_leg(self):
        res = self._res()
        to_port = self._mk_leg(res, MCO, PORT)
        from_port = self._mk_leg(res, PORT, MCO, days=7)
        self.assertTrue(to_port.shows_store_stop)
        self.assertFalse(from_port.shows_store_stop)

    def test_no_airport_leg_falls_back_to_first_leg_only(self):
        res = self._res()
        first = self._mk_leg(res, HOME, RESORT)
        second = self._mk_leg(res, RESORT, HOME, days=3)
        self.assertTrue(first.shows_store_stop)
        self.assertFalse(second.shows_store_stop)

    def test_other_leg_defers_to_the_arrival_sibling(self):
        res = self._res()
        transfer = self._mk_leg(res, HOME, RESORT)          # created first
        arrival = self._mk_leg(res, MCO, RESORT, days=1)
        self.assertTrue(arrival.shows_store_stop)
        self.assertFalse(transfer.shows_store_stop)


class WizardPublixHardBlockTests(TestCase):
    """The legs step hard-blocks a closed-hours grocery stop — no ack bypass."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher-px", password="x", is_staff=True)
        cls.customer = Customer.objects.create(
            first_name="Wizard", last_name="Guest",
            email="wizard@example.com", phone_number="4070000002",
        )
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.future = django_timezone.localdate() + timedelta(days=30)

    def _seed_session(self, store_stop=True, num_legs=1):
        session = self.client.session
        session["dispatcher_booking"] = {
            "customer_id": self.customer.id,
            "num_legs": num_legs,
            "trip_type": "one_way" if num_legs == 1 else "round_trip",
            "reservation_data": {
                "manual_vehicle": str(self.vehicle.id),
                "passenger_count": "2",
                "luggage_count": "2",
                "store_stop": "True" if store_stop else "False",
            },
            "step": 3,
        }
        session.save()

    def _post_data(self, legs):
        n = len(legs)
        data = {
            "legs-TOTAL_FORMS": str(n), "legs-INITIAL_FORMS": "0",
            "legs-MIN_NUM_FORMS": "1", "legs-MAX_NUM_FORMS": "5",
            "flights-TOTAL_FORMS": str(n), "flights-INITIAL_FORMS": "0",
            "flights-MIN_NUM_FORMS": "0", "flights-MAX_NUM_FORMS": "5",
        }
        for i, leg in enumerate(legs):
            for k, v in leg.items():
                if v is not None:
                    data[f"legs-{i}-{k}"] = v
        return data

    def test_closed_hours_blocks_without_ack_option(self):
        self._seed_session(store_stop=True)
        resp = self.client.post(
            reverse("dispatcher_booking_legs"),
            self._post_data([_leg(self.future, "22:00")]),
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("sanityPanel", html)
        self.assertIn("sev-error", html)
        self.assertIn("grocery stop", html)
        # No warnings → no acknowledge checkbox at all
        self.assertNotIn('name="sanity_ack"', html)
        self.assertNotIn("legs_data", self.client.session["dispatcher_booking"])

    def test_ack_cannot_bypass_the_error(self):
        self._seed_session(store_stop=True)
        payload = self._post_data([_leg(self.future, "22:00")])
        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        token = re.search(
            r'name="sanity_ack_token" value="([0-9a-f]+)"', resp.content.decode()
        ).group(1)
        payload.update({"sanity_ack": "1", "sanity_ack_token": token})
        resp2 = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("sev-error", resp2.content.decode())
        self.assertNotIn("legs_data", self.client.session["dispatcher_booking"])

    def test_daytime_store_stop_passes(self):
        self._seed_session(store_stop=True)
        resp = self.client.post(
            reverse("dispatcher_booking_legs"),
            self._post_data([_leg(self.future, "14:30")]),
        )
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False,
        )

    def test_late_pickup_without_store_stop_passes(self):
        self._seed_session(store_stop=False)
        resp = self.client.post(
            reverse("dispatcher_booking_legs"),
            self._post_data([_leg(self.future, "22:00")]),
        )
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False,
        )

    def test_create_backstop_raises(self):
        from dispatching.views import create_dispatcher_reservation
        booking_data = {
            "customer_id": self.customer.id,
            "reservation_data": {
                "manual_vehicle": str(self.vehicle.id),
                "store_stop": "True",
            },
            "pricing_data": {"manual_base_price": "100"},
            "legs_data": [_leg(self.future, "22:00:00")],
        }
        with self.assertRaisesRegex(ValueError, "Publix"):
            create_dispatcher_reservation(booking_data)


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class CustomerBookingFormPublixTests(TestCase):
    """The public booking form refuses a store stop at a closed-hours pickup."""

    @classmethod
    def setUpTestData(cls):
        origin = Location.objects.create(name="Orlando International Airport")
        destination = Location.objects.create(name="Disney Property")
        route = Route.objects.create(origin=origin, destination=destination)
        vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.rate = Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("120"), round_trip_price=Decimal("220"),
        )
        cls.future = django_timezone.localdate() + timedelta(days=30)

    def _post_data(self, pickup_time, store_stop=True):
        data = {
            "first_name": "Pat", "last_name": "Guest",
            "email": "pat@example.com", "phone_number": "4075551212",
            "zipcode": "32819",
            "passenger_count": "2", "luggage_count": "2", "luggage_type": "checked",
            "rf_carseats": "0", "ff_carseats": "0", "booster_seats": "0",
            "extra_carseats": "0", "extra_boosters": "0",
            "leg1-pickup_date": self.future.isoformat(),
            "leg1-pickup_time": pickup_time,
            "leg1-pickup_location": MCO,
            "leg1-dropoff_location": RESORT,
        }
        if store_stop:
            data["store_stop"] = "on"
        return data

    def _url(self):
        return reverse("reserve", args=[self.rate.pk]) + "?round=1"

    def test_closed_hours_store_stop_rejected(self):
        resp = self.client.post(self._url(), self._post_data("22:00"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Publix is closed at your pickup time")
        self.assertEqual(Reservation.objects.count(), 0)

    def test_after_midnight_store_stop_rejected(self):
        resp = self.client.post(self._url(), self._post_data("00:30"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Publix is closed at your pickup time")
        self.assertEqual(Reservation.objects.count(), 0)

    def test_daytime_store_stop_books(self):
        resp = self.client.post(self._url(), self._post_data("14:00"))
        self.assertEqual(resp.status_code, 302)
        res = Reservation.objects.get()
        self.assertTrue(res.store_stop)

    def test_closed_hours_without_store_stop_books(self):
        resp = self.client.post(self._url(), self._post_data("22:00", store_stop=False))
        self.assertEqual(resp.status_code, 302)
        res = Reservation.objects.get()
        self.assertFalse(res.store_stop)
