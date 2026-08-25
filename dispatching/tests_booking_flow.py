"""Dispatcher booking wizard: legs are added on the trip-details step, and the
trip type is derived from how many the dispatcher ended up with."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Reservation

User = get_user_model()


class BookingWizardFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher_flow", password="x", is_staff=True)
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        mco = Location.objects.create(name="MCO Airport")
        resort = Location.objects.create(name="Disney Pop Century")
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=Route.objects.create(origin=mco, destination=resort),
            oneway_price="150.00",
            round_trip_price="250.00",
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.future = django_timezone.localdate() + timedelta(days=30)

    def _leg(self, pickup="MCO Airport", dropoff="Disney Pop Century", time="14:30", day_offset=0):
        return {
            "pickup_date": (self.future + timedelta(days=day_offset)).isoformat(),
            "pickup_time": time,
            "pickup_location": pickup,
            "dropoff_location": dropoff,
            "private_notes": "",
        }

    def _return_leg(self, day_offset=5, time="10:00"):
        return self._leg(
            pickup="Disney Pop Century", dropoff="MCO Airport",
            time=time, day_offset=day_offset,
        )

    def _legs_payload(self, legs):
        n = len(legs)
        data = {
            "legs-TOTAL_FORMS": str(n), "legs-INITIAL_FORMS": "0",
            "legs-MIN_NUM_FORMS": "1", "legs-MAX_NUM_FORMS": "5",
            "flights-TOTAL_FORMS": str(n), "flights-INITIAL_FORMS": "0",
            "flights-MIN_NUM_FORMS": "0", "flights-MAX_NUM_FORMS": "5",
        }
        for i, leg in enumerate(legs):
            for k, v in leg.items():
                data[f"legs-{i}-{k}"] = v
        return data

    def _walk_to_legs(self):
        """start -> customer -> reservation details, returning the legs-step response."""
        resp = self.client.get(reverse("dispatcher_booking_start"))
        self.assertRedirects(
            resp, reverse("dispatcher_booking_customer"), fetch_redirect_response=False
        )

        resp = self.client.post(reverse("dispatcher_booking_customer"), {
            "first_name": "Jane", "last_name": "Guest",
            "email": "jane@example.com", "phone_number": "4075551234",
            "zipcode": "32819",
        })
        self.assertRedirects(
            resp, reverse("dispatcher_booking_reservation"), fetch_redirect_response=False
        )

        resp = self.client.post(reverse("dispatcher_booking_reservation"), {
            "passenger_count": "2", "luggage_count": "2", "luggage_type": "checked",
            "rf_carseats": "0", "ff_carseats": "0", "booster_seats": "0",
            "manual_vehicle": str(self.vehicle.id),
        })
        self.assertRedirects(
            resp, reverse("dispatcher_booking_legs"), fetch_redirect_response=False
        )
        return self.client.get(reverse("dispatcher_booking_legs"))

    def test_start_no_longer_asks_for_trip_type(self):
        """The entry point drops straight into the customer step."""
        resp = self.client.get(reverse("dispatcher_booking_start"))
        self.assertRedirects(
            resp, reverse("dispatcher_booking_customer"), fetch_redirect_response=False
        )
        self.assertNotIn("trip_type", self.client.session["dispatcher_booking"])

    def test_legs_step_starts_with_exactly_one_leg(self):
        """No spare blank card — a fresh booking opens with a single leg."""
        resp = self._walk_to_legs()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["leg_formset"].total_form_count(), 1)
        self.assertEqual(resp.context["flight_formset"].total_form_count(), 1)
        self.assertContains(resp, 'id="add-leg-btn"')
        self.assertContains(resp, 'id="leg-blueprint"')

    def test_one_leg_books_as_one_way(self):
        self._walk_to_legs()
        resp = self.client.post(
            reverse("dispatcher_booking_legs"), self._legs_payload([self._leg()])
        )
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["trip_type"], "one_way")
        self.assertEqual(saved["num_legs"], 1)

    def test_two_legs_book_as_round_trip(self):
        """Adding a second leg is what makes it a round trip now."""
        self._walk_to_legs()
        resp = self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._return_leg(),
        ]))
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["trip_type"], "round_trip")
        self.assertEqual(saved["num_legs"], 2)

    def test_three_legs_book_as_multi_leg(self):
        self._walk_to_legs()
        resp = self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._leg(pickup="Disney Pop Century", dropoff="Disney Springs", time="18:00"),
            self._leg(pickup="Disney Springs", dropoff="MCO Airport", time="21:00", day_offset=5),
        ]))
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["trip_type"], "multi_leg")
        self.assertEqual(saved["num_legs"], 3)

    def test_legs_step_rerenders_the_count_that_was_posted(self):
        """A dispatcher who added legs client-side keeps them on a bounce-back."""
        self._walk_to_legs()
        payload = self._legs_payload([
            self._leg(),
            self._return_leg(),
            self._leg(pickup="MCO Airport", dropoff="", time="12:00"),  # invalid: no dropoff
        ])
        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["num_legs"], 3)
        self.assertEqual(len(resp.context["leg_overrides"]), 3)

    def test_back_nav_redisplays_the_saved_legs(self):
        """Going back to trip details shows the legs already entered, no spare card."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._return_leg(),
        ]))
        resp = self.client.get(reverse("dispatcher_booking_legs"))
        self.assertEqual(resp.context["leg_formset"].total_form_count(), 2)
        self.assertEqual(resp.context["num_legs"], 2)
        forms = list(resp.context["leg_formset"])
        self.assertEqual(forms[0].initial["pickup_location"], "MCO Airport")
        self.assertEqual(forms[1].initial["pickup_location"], "Disney Pop Century")

    def test_removing_a_leg_after_back_nav_drops_it(self):
        """Back-nav then remove leaves INITIAL_FORMS above the posted count —
        the surviving leg still saves, and the trip downgrades to one-way."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._return_leg(),
        ]))
        # Back on the step, the dispatcher removes the return before resubmitting.
        payload = self._legs_payload([self._leg()])
        payload["legs-INITIAL_FORMS"] = "2"
        payload["flights-INITIAL_FORMS"] = "2"
        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["num_legs"], 1)
        self.assertEqual(saved["trip_type"], "one_way")
        self.assertEqual(len(saved["legs_data"]), 1)
        self.assertEqual(len(saved["flights_data"]), 1)

    def test_round_trip_books_end_to_end(self):
        """Full walk-through: two legs land on one reservation."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._return_leg(),
        ]))
        resp = self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "250.00", "additional_charges": "0",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "250.00", "private_notes": "",
        })
        self.assertRedirects(
            resp, reverse("dispatcher_booking_review"), fetch_redirect_response=False
        )

        before = Reservation.objects.count()
        resp = self.client.post(reverse("dispatcher_booking_review"), {"confirm": "1"})
        self.assertEqual(Reservation.objects.count(), before + 1)

        reservation = Reservation.objects.order_by("-id").first()
        self.assertEqual(reservation.trip_type, "round_trip")
        self.assertEqual(reservation.legs.count(), 2)
        self.assertNotIn("dispatcher_booking", self.client.session)

    def test_max_legs_is_enforced_server_side(self):
        """A hand-rolled POST past the cap is rejected, not silently truncated."""
        self._walk_to_legs()
        resp = self.client.post(
            reverse("dispatcher_booking_legs"), self._legs_payload([self._leg()] * 6)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("legs_data", self.client.session["dispatcher_booking"])


class BookingCustomerCleanupTests(TestCase):
    """Customers created on the customer step shouldn't multiply on back-nav."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher_cleanup", password="x", is_staff=True)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)

    def test_customer_step_reached_without_trip_type_step(self):
        self.client.get(reverse("dispatcher_booking_start"))
        resp = self.client.get(reverse("dispatcher_booking_customer"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["step"], 1)
        self.assertEqual(Customer.objects.count(), 0)
