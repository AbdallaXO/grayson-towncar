"""Dispatcher booking wizard: legs are added on the trip-details step, and the
trip type is derived from how many the dispatcher ended up with."""
from datetime import timedelta
from decimal import Decimal

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
        # The read-back box is part of the create gate now, and an airport leg
        # with no flight number is a warning that has to be acknowledged.
        resp = self.client.post(
            reverse("dispatcher_booking_review"),
            {"confirm": "1", "read_back": "on", "ack_flags": "on"},
        )
        self.assertEqual(Reservation.objects.count(), before + 1)

        reservation = Reservation.objects.order_by("-id").first()
        self.assertEqual(reservation.trip_type, "round_trip")
        self.assertEqual(reservation.legs.count(), 2)
        self.assertNotIn("dispatcher_booking", self.client.session)

    def test_walking_back_does_not_switch_the_grocery_stop_on(self):
        """A box never ticked comes back out of the session as the string
        "False" — which a checkbox would read as ticked."""
        self._walk_to_legs()
        resp = self.client.get(reverse("dispatcher_booking_reservation"))
        form = resp.context["form"]
        self.assertIs(form["store_stop"].value(), False)
        self.assertIs(form["need_carseats"].value(), False)
        self.assertNotIn('name="store_stop" checked', resp.content.decode())

    def _late_leg(self, day_offset=0):
        return self._leg(time="23:30", day_offset=day_offset)

    def test_after_hours_pickup_is_priced_not_remembered(self):
        """A 10 PM-6 AM pickup carries a flat fee. The suggested rate names it
        instead of leaving the dispatcher to know the rule."""
        from reservations.utils import AFTERHOURS_FEE_AMOUNT

        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"),
                         self._legs_payload([self._late_leg()]))

        resp = self.client.get(reverse("dispatcher_booking_pricing"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["afterhours_legs"], [1])
        self.assertEqual(resp.context["afterhours_fee"], AFTERHOURS_FEE_AMOUNT)
        self.assertEqual(
            resp.context["suggested_total"],
            resp.context["suggested_price"] + AFTERHOURS_FEE_AMOUNT,
        )
        self.assertIn("after-hours fee", resp.content.decode())

    def test_after_hours_fee_left_out_is_flagged_at_review(self):
        """Pricing the route and forgetting the fee is exactly how it goes
        uncollected, so the audit says so."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"),
                         self._legs_payload([self._late_leg()]))
        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "150.00", "additional_charges": "0",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "150.00", "private_notes": "",
        })

        resp = self.client.get(reverse("dispatcher_booking_review"))
        texts = [f["text"] for f in resp.context["review_flags"]]
        self.assertTrue(any("after-hours fee is not" in t for t in texts), texts)

    def test_after_hours_marker_only_set_when_the_fee_was_charged(self):
        """The marker is what stops a later delay pass asking for the same $20
        twice — so it must never claim money nobody collected."""
        from reservations.utils import AFTERHOURS_FEE_AMOUNT

        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"),
                         self._legs_payload([self._late_leg()]))
        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "150.00", "additional_charges": "0",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "150.00", "private_notes": "",
        })
        self.client.post(reverse("dispatcher_booking_review"),
                         {"confirm": "1", "read_back": "on", "ack_flags": "on"})
        leg = Reservation.objects.order_by("-id").first().legs.first()
        self.assertEqual(leg.afterhours_fee, Decimal("0.00"))

        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"),
                         self._legs_payload([self._late_leg()]))
        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "150.00", "additional_charges": "20.00",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "170.00", "private_notes": "",
        })
        self.client.post(reverse("dispatcher_booking_review"),
                         {"confirm": "1", "read_back": "on", "ack_flags": "on"})
        leg = Reservation.objects.order_by("-id").first().legs.first()
        self.assertEqual(leg.afterhours_fee, AFTERHOURS_FEE_AMOUNT)

    def test_every_step_renders(self):
        """Each screen of the wizard renders, with the pieces that catch errors."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
            self._return_leg(),
        ]))
        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "250.00", "additional_charges": "0",
            "gratuity_option": "20", "gratuity_amount": "50.00",
            "total_price": "300.00", "private_notes": "",
        })

        expected = {
            "dispatcher_booking_customer": ["Quick Customer Lookup"],
            "dispatcher_booking_reservation": ["Vehicle Selection", "Free Grocery Stop"],
            "dispatcher_booking_legs": ["Fill Reverse Of Leg 1", "Add Another Leg"],
            "dispatcher_booking_pricing": ["Use This Rate", "Total Price"],
            "dispatcher_booking_review": [
                "Read This Back To The Guest",
                "let me read this back to you",
                "Create Reservation",
            ],
        }
        for name, needles in expected.items():
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            body = resp.content.decode()
            self.assertIn("Booking Summary", body, name)
            for needle in needles:
                self.assertIn(needle, body, f"{name}: {needle}")

    def test_read_back_carries_no_flight_time_it_cannot_vouch_for(self):
        """With no verified schedule the sentence omits the time entirely —
        it never falls back to the pickup time, which is a different fact."""
        self._walk_to_legs()
        payload = self._legs_payload([self._leg()])
        payload["flights-0-airline"] = "Delta"
        payload["flights-0-flight_number"] = "1204"

        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        # A flight the schedule service can't vouch for stops at the sanity
        # panel first; acknowledge it and carry on.
        if resp.status_code == 200 and resp.context.get("sanity_panel"):
            payload["sanity_ack"] = "1"
            payload["sanity_ack_token"] = resp.context["sanity_panel"]["token"]
            resp = self.client.post(reverse("dispatcher_booking_legs"), payload)

        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "150.00", "additional_charges": "0",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "150.00", "private_notes": "",
        })

        resp = self.client.get(reverse("dispatcher_booking_review"))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context["legs_data"][0]["flight_sched"])
        body = resp.content.decode()
        self.assertIn("arriving", body)
        self.assertNotIn("arriving at <b>2:30 PM</b>", body)

    def test_create_is_refused_until_the_trip_is_read_back(self):
        """The read-back tick is enforced here, not only in the browser."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg(),
        ]))
        self.client.post(reverse("dispatcher_booking_pricing"), {
            "manual_base_price": "150.00", "additional_charges": "0",
            "gratuity_option": "none", "gratuity_amount": "0",
            "total_price": "150.00", "private_notes": "",
        })

        before = Reservation.objects.count()
        self.client.post(
            reverse("dispatcher_booking_review"), {"confirm": "1", "ack_flags": "on"}
        )
        self.assertEqual(Reservation.objects.count(), before)
        # The booking is still in play, not thrown away.
        self.assertIn("dispatcher_booking", self.client.session)

        self.client.post(
            reverse("dispatcher_booking_review"),
            {"confirm": "1", "read_back": "on", "ack_flags": "on"},
        )
        self.assertEqual(Reservation.objects.count(), before + 1)

    def test_max_legs_is_enforced_server_side(self):
        """A hand-rolled POST past the cap is rejected, not silently truncated."""
        self._walk_to_legs()
        resp = self.client.post(
            reverse("dispatcher_booking_legs"), self._legs_payload([self._leg()] * 6)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("legs_data", self.client.session["dispatcher_booking"])


class BookingWizardMultiVehiclePricingTests(TestCase):
    """A second leg that goes somewhere else entirely (different route AND a
    different vehicle) is NOT a round trip and must not be priced as one.

    Regression for a real mispriced reservation: MCO -> Disney (SUV) then
    Disney -> Port (Van) was quoted as a single $275 MCO<->Disney SUV round
    trip -- leg 2's own route and vehicle were silently ignored. It should
    price as two independent one-ways: $140 SUV + $235 Van = $375.
    """

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher_multiveh", password="x", is_staff=True)
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.van = Vehicle.objects.create(vehicle_type="van", capacity=10, luggage_capacity=15)
        mco = Location.objects.create(name="MCO Airport")
        disney = Location.objects.create(name="Disney Pop Century")
        port = Location.objects.create(name="Port Canaveral")
        Rate.objects.create(
            vehicle=cls.suv,
            route=Route.objects.create(origin=mco, destination=disney),
            oneway_price="140.00", round_trip_price="275.00",
        )
        Rate.objects.create(
            vehicle=cls.van,
            route=Route.objects.create(origin=disney, destination=port),
            oneway_price="235.00", round_trip_price="420.00",
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.future = django_timezone.localdate() + timedelta(days=30)

    def _leg(self, pickup, dropoff, time, day_offset=0, vehicle_override=None):
        leg = {
            "pickup_date": (self.future + timedelta(days=day_offset)).isoformat(),
            "pickup_time": time,
            "pickup_location": pickup,
            "dropoff_location": dropoff,
            "private_notes": "",
        }
        if vehicle_override is not None:
            leg["override-vehicle"] = str(vehicle_override)
        return leg

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
        self.client.get(reverse("dispatcher_booking_start"))
        self.client.post(reverse("dispatcher_booking_customer"), {
            "first_name": "Jane", "last_name": "Guest",
            "email": "jane.multiveh@example.com", "phone_number": "4075551236",
            "zipcode": "32819",
        })
        self.client.post(reverse("dispatcher_booking_reservation"), {
            "passenger_count": "2", "luggage_count": "2", "luggage_type": "checked",
            "rf_carseats": "0", "ff_carseats": "0", "booster_seats": "0",
            "manual_vehicle": str(self.suv.id),
        })

    def test_mismatched_second_leg_is_multi_leg_not_round_trip(self):
        self._walk_to_legs()
        resp = self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg("MCO Airport", "Disney Pop Century", "14:30"),
            self._leg("Disney Pop Century", "Port Canaveral", "09:00", day_offset=3,
                       vehicle_override=self.van.id),
        ]))
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"), fetch_redirect_response=False
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["trip_type"], "multi_leg")

        resp = self.client.get(reverse("dispatcher_booking_pricing"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["suggested_price"], Decimal("375.00"))

    def test_genuine_return_trip_still_prices_as_one_round_trip_rate(self):
        """Same route, reversed, same vehicle -- still a real round trip."""
        self._walk_to_legs()
        self.client.post(reverse("dispatcher_booking_legs"), self._legs_payload([
            self._leg("MCO Airport", "Disney Pop Century", "14:30"),
            self._leg("Disney Pop Century", "MCO Airport", "09:00", day_offset=5),
        ]))
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(saved["trip_type"], "round_trip")

        resp = self.client.get(reverse("dispatcher_booking_pricing"))
        self.assertEqual(resp.context["suggested_price"], Decimal("275.00"))


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
