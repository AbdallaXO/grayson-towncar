"""Booking debounce guard.

Rapid re-clicks of the public "Book" button must NOT each create a reservation.
The second/third click within 30s should redirect to the first booking's checkout
instead of racing the delete+create-duplicate logic (which caused the 133s booking
freeze -- incident 2026-07-18).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Reservation

MCO = "Orlando International Airport (MCO)"
RESORT = "Disney Pop Century Resort"


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class BookingDebounceTests(TestCase):
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

    def _post_data(self):
        return {
            "first_name": "Pat", "last_name": "Guest",
            "email": "pat@example.com", "phone_number": "4075551212",
            "zipcode": "32819",
            "passenger_count": "2", "luggage_count": "2", "luggage_type": "checked",
            "rf_carseats": "0", "ff_carseats": "0", "booster_seats": "0",
            "extra_carseats": "0", "extra_boosters": "0",
            "leg1-pickup_date": self.future.isoformat(),
            "leg1-pickup_time": "14:00:00",
            "leg1-pickup_location": MCO,
            "leg1-dropoff_location": RESORT,
        }

    def _url(self):
        return reverse("reserve", args=[self.rate.pk]) + "?round=1"

    def test_single_booking_creates_one(self):
        resp = self.client.post(self._url(), self._post_data())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Reservation.objects.count(), 1)

    def test_rapid_reclicks_do_not_duplicate(self):
        url, data = self._url(), self._post_data()

        r1 = self.client.post(url, data)
        self.assertEqual(r1.status_code, 302)
        res = Reservation.objects.get()          # exactly one so far
        first_uuid = str(res.uuid)

        # Two more immediate "clicks", well within the 30s debounce window.
        r2 = self.client.post(url, data)
        r3 = self.client.post(url, data)

        # Still exactly ONE reservation, and the re-clicks land on ITS checkout.
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(r3.status_code, 302)
        self.assertIn(first_uuid, r2.url)
        self.assertIn(first_uuid, r3.url)

    def test_old_unpaid_attempt_is_not_debounced(self):
        url, data = self._url(), self._post_data()
        self.client.post(url, data)
        r1 = Reservation.objects.get()
        old_uuid = str(r1.uuid)

        # Backdate beyond the 30s debounce window (still inside the 10-min cleanup).
        Reservation.objects.filter(pk=r1.pk).update(
            created_at=django_timezone.now() - timedelta(minutes=5)
        )

        # A genuine re-book now must NOT debounce to the stale attempt; the 10-min
        # cleanup replaces it, so we still end with exactly one -- a NEW uuid.
        self.client.post(url, data)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertNotEqual(str(Reservation.objects.get().uuid), old_uuid)
