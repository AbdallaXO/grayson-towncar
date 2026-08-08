"""Reservation history — the panel that used to be empty on every booking.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_reservation_history

"View reservation history" opened a modal that rendered `reservation_history_records`
— a context variable no view ever set — so every reservation, forever, reported
"No history recorded." Two things are pinned below:

  * the modal endpoint exists and returns the real timeline;
  * the timeline answers "created, when, by who" and shows leg edits, because a
    reservation row barely ever changes while its legs change constantly.
"""

from datetime import date, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


class ReservationHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            username="dispatcher", password="x", is_staff=True,
            first_name="Dana", last_name="Ruiz",
        )
        cls.other = User.objects.create_user(username="guest", password="x")
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=3, luggage_capacity=3,
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Grand Floridian"),
            inhouse_base_pay=Decimal("50.00"),
        )
        cls.rate = Rate.objects.create(
            route=route, vehicle=cls.vehicle,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="George", last_name="Miller",
            email="george@example.com", phone_number="4075550000",
        )

    def _reservation(self, **kwargs):
        defaults = dict(
            customer=self.customer, vehicle=self.vehicle, rate=self.rate,
            trip_type="one-way",
            base_price=Decimal("100.00"), total_price=Decimal("100.00"),
        )
        defaults.update(kwargs)
        return Reservation.objects.create(**defaults)

    def _leg(self, reservation, **kwargs):
        defaults = dict(
            reservation=reservation,
            pickup_date=date(2026, 8, 8), pickup_time=time(14, 30),
            pickup_location="MCO - Orlando International Airport",
            dropoff_location="Disney Grand Floridian",
        )
        defaults.update(kwargs)
        return Leg.objects.create(**defaults)

    # -- the endpoint the modal calls ------------------------------------

    def test_modal_endpoint_returns_the_timeline_for_staff(self):
        res = self._reservation()
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("reservation_history_partial", args=[res.uuid])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "No history recorded")

    def test_modal_endpoint_is_staff_only(self):
        res = self._reservation()
        self.client.force_login(self.other)
        resp = self.client.get(
            reverse("reservation_history_partial", args=[res.uuid])
        )
        self.assertEqual(resp.status_code, 403)

    def test_reservation_page_wires_the_button_to_that_endpoint(self):
        # The original bug: the modal rendered a context variable no view set,
        # so it was hardcoded-empty. Pin the page to the fetch URL instead.
        res = self._reservation()
        self._leg(res)
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("reservation_details", args=[res.uuid]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "reservationHistoryModalBody")
        self.assertContains(
            resp, reverse("reservation_history_partial", args=[res.uuid])
        )

    # -- what the timeline actually says ---------------------------------

    def test_creation_is_always_the_first_entry_with_who_and_when(self):
        from dispatching.views import _reservation_timeline

        res = self._reservation(created_by=self.staff)
        entries, _ = _reservation_timeline(res)
        created = entries[-1]
        self.assertEqual(created["scope"], "Reservation")
        self.assertEqual(created["action"], "Created")
        self.assertEqual(created["actor"], "Dana Ruiz")
        self.assertIsNotNone(created["when"])

    def test_online_booking_names_the_customer_not_system(self):
        # created_by is only set for back-office bookings, so an online booking
        # would otherwise be attributed to nobody. The customer booked it.
        from dispatching.views import _reservation_timeline

        res = self._reservation(booking_source="google_ads")
        entries, _ = _reservation_timeline(res)
        created = [e for e in entries if e["action"] == "Created"
                   and e["scope"] == "Reservation"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["actor"], "George Miller")
        self.assertEqual(
            created[0]["actor_note"], "Customer — booked online via Google Ads"
        )

    def test_dispatcher_booking_says_dispatcher(self):
        from dispatching.views import _reservation_timeline

        res = self._reservation(created_by=self.staff)
        entries, _ = _reservation_timeline(res)
        created = [e for e in entries if e["action"] == "Created"
                   and e["scope"] == "Reservation"][0]
        self.assertEqual(created["actor"], "Dana Ruiz")
        self.assertEqual(created["actor_note"], "Dispatcher")

    def test_travel_agent_booking_names_the_agency(self):
        from users.models import TravelAgent
        from dispatching.views import _reservation_timeline

        User = get_user_model()
        agent_user = User.objects.create_user(username="agent", password="x")
        # commission_rate default is a float literal on a DecimalField, and
        # Reservation.save() divides it by a Decimal — pass a Decimal so this
        # test exercises history, not that unrelated model quirk.
        agent = TravelAgent.objects.create(
            user=agent_user, agency_name="Sunshine Travel",
            commission_rate=Decimal("10.00"),
        )
        res = self._reservation(travel_agent=agent)
        entries, _ = _reservation_timeline(res)
        created = [e for e in entries if e["action"] == "Created"
                   and e["scope"] == "Reservation"][0]
        self.assertEqual(created["actor"], "Sunshine Travel")
        self.assertEqual(created["actor_note"], "Travel agent")

    def test_who_created_it_is_repeated_in_the_header(self):
        # The table is capped at 300 entries, so a busy booking could scroll the
        # Created row off. The header must still answer "by who".
        res = self._reservation(created_by=self.staff)
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("reservation_history_partial", args=[res.uuid])
        )
        self.assertContains(resp, "Dana Ruiz")
        self.assertContains(resp, "Dispatcher")

    def test_leg_edits_show_up_on_the_reservation_timeline(self):
        # The reservation row never changed here — only the leg did. This is the
        # case that made the old reservation-only log look empty.
        from dispatching.views import _reservation_timeline

        res = self._reservation()
        leg = self._leg(res)
        leg.pickup_time = time(15, 45)
        leg.save()

        entries, _ = _reservation_timeline(res)
        leg_changes = [e for e in entries if e["scope"] == "Leg 1"
                       and e["action"] == "Changed"]
        self.assertTrue(leg_changes, "leg edit missing from reservation timeline")
        fields = {c["field"] for e in leg_changes for c in e["changes"]}
        self.assertIn("Pickup time", fields)
        self.assertEqual(
            leg_changes[0]["scope_detail"],
            "MCO - Orlando International Airport → Disney Grand Floridian",
        )

    def test_legs_are_numbered_by_pickup_order_not_insert_order(self):
        from dispatching.views import _reservation_timeline

        res = self._reservation()
        late = self._leg(res, pickup_time=time(20, 0))
        early = self._leg(res, pickup_time=time(6, 0),
                          pickup_location="Grand Floridian",
                          dropoff_location="MCO")
        for leg, new_time in ((late, time(21, 0)), (early, time(7, 0))):
            leg.pickup_time = new_time
            leg.save()

        entries, _ = _reservation_timeline(res)
        by_scope = {e["scope"]: e["scope_detail"] for e in entries
                    if e["scope"].startswith("Leg")}
        self.assertEqual(by_scope["Leg 1"], "Grand Floridian → MCO")
        self.assertEqual(by_scope["Leg 2"],
                         "MCO - Orlando International Airport → Disney Grand Floridian")

    def test_timeline_is_newest_first(self):
        from dispatching.views import _reservation_timeline

        res = self._reservation()
        leg = self._leg(res)
        leg.pickup_time = time(15, 45)
        leg.save()

        entries, _ = _reservation_timeline(res)
        whens = [e["when"] for e in entries]
        self.assertEqual(whens, sorted(whens, reverse=True))

    def test_long_histories_are_capped_and_say_so(self):
        from dispatching.views import _reservation_timeline

        res = self._reservation()
        leg = self._leg(res)
        for minute in range(6):
            leg.pickup_time = time(15, minute)
            leg.save()

        entries, truncated = _reservation_timeline(res, limit=3)
        self.assertEqual(len(entries), 3)
        self.assertGreater(truncated, 0)

    # -- full page keeps working -----------------------------------------

    def test_full_page_renders_the_same_timeline(self):
        res = self._reservation(created_by=self.staff)
        leg = self._leg(res)
        leg.pickup_time = time(15, 45)
        leg.save()

        self.client.force_login(self.staff)
        resp = self.client.get(reverse("reservation_history", args=[res.uuid]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Leg 1")
        self.assertContains(resp, "Pickup time")
