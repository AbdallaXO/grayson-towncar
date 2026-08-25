"""
The "What to say" script on an unpaid task.

It is spoken to a guest, so the wording carries real constraints: it opens as a
confirmation call rather than a chase, it names the dispatcher who is actually
making the call, and it stops after asking for the card so they can pause and
listen. The stage direction that says so lives in the template, never in the
spoken text.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ops.models import OperationalTask
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

User = get_user_model()


class PaymentChaseCallScriptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4,
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=Route.objects.create(
                origin=Location.objects.create(name="Script Pickup"),
                destination=Location.objects.create(name="Script Dropoff"),
            ),
            oneway_price=Decimal("100.00"),
            round_trip_price=Decimal("180.00"),
        )

    def _task(self, *, pickup_in=timedelta(days=2), with_leg=True):
        customer = Customer.objects.create(
            first_name="Emily", last_name="Guest",
            email="emily@example.com", phone_number="555-000-1111",
            zipcode="32801",
        )
        res = Reservation.objects.create(
            customer=customer, rate=self.rate, vehicle=self.vehicle,
            trip_type="one_way",
            base_price=Decimal("290.00"), total_price=Decimal("290.00"),
            status="confirmed",
        )
        if with_leg:
            pickup = timezone.localtime(timezone.now() + pickup_in)
            Leg.objects.create(
                reservation=res,
                pickup_date=pickup.date(), pickup_time=pickup.time(),
                pickup_location="Script Pickup", dropoff_location="Script Dropoff",
                status="confirmed",
            )
        return OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title="Unpaid balance", reservation=res,
            due_at=timezone.now() + timedelta(hours=4),
        )

    def _script_for(self, user, **kwargs):
        task = self._task(**kwargs)
        self.client.force_login(user)
        resp = self.client.get(reverse("task_detail", args=[task.id]))
        self.assertEqual(resp.status_code, 200)
        return resp, resp.context["ladder_ctx"]["call_script"]

    def test_names_the_dispatcher_making_the_call(self):
        user = User.objects.create_user(
            "jrivera", password="x", is_staff=True, first_name="Joseph",
        )
        _, script = self._script_for(user)
        self.assertIn("this is Joseph calling from Grayson Towncar", script)
        self.assertNotIn("[your name]", script)

    def test_falls_back_to_a_name_shaped_username(self):
        """Some staff have no first name set, but their handle is their name."""
        user = User.objects.create_user("luis", password="x", is_staff=True)
        _, script = self._script_for(user)
        self.assertIn("this is Luis calling from Grayson Towncar", script)

    def test_keeps_the_placeholder_rather_than_read_out_a_handle(self):
        """Nobody introduces themselves to a guest as 'dispatcher1'."""
        user = User.objects.create_user("dispatcher1", password="x", is_staff=True)
        _, script = self._script_for(user)
        self.assertIn("this is [your name] calling", script)
        self.assertNotIn("dispatcher1", script)

    def test_opens_as_a_confirmation_not_a_chase(self):
        user = User.objects.create_user(
            "iris", password="x", is_staff=True, first_name="Iris",
        )
        _, script = self._script_for(user)
        self.assertIn("confirm your transportation", script)
        self.assertIn("coming up in 2 days", script)
        self.assertIn("email confirmation", script)
        # Leading with the balance turns it back into a collections call.
        self.assertNotIn("$", script)

    def test_says_tomorrow_rather_than_in_1_days(self):
        user = User.objects.create_user(
            "bryan", password="x", is_staff=True, first_name="Bryan",
        )
        _, script = self._script_for(user, pickup_in=timedelta(days=1, hours=2))
        self.assertIn("coming up tomorrow", script)
        self.assertNotIn("in 1 days", script)

    def test_degrades_when_there_is_no_upcoming_leg(self):
        user = User.objects.create_user(
            "tadashi", password="x", is_staff=True,
        )
        _, script = self._script_for(user, with_leg=False)
        self.assertIn("your upcoming transportation", script)
        self.assertIn("still showing as pending", script)

    def test_the_pause_cue_is_never_part_of_the_spoken_text(self):
        """It's a direction to the dispatcher — on the page, out of the script."""
        user = User.objects.create_user(
            "rayyan", password="x", is_staff=True, first_name="Rayyan",
        )
        resp, script = self._script_for(user)
        self.assertNotIn("let them answer", script)
        self.assertContains(resp, "Then stop and let them answer.")
