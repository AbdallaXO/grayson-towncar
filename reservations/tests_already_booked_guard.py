"""Tests for the "this person has already booked" guard.

Run with:  ./manage.py test reservations.tests_already_booked_guard

Conversion marks exactly one Lead per Reservation, so anything that decides
whether to message someone by reading ``lead.converted`` alone keeps selling a
ride the person has already paid for. These cover the guard itself and the two
senders that consult it.
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.utils import timezone

from ghl_integration.models import FollowUpTask
from rates.models import Location, Rate, Route, Vehicle
from reservations.lead_matching import already_booked_reservation
from reservations.models import Customer, Lead, Leg, Reservation
from reservations.signals import (
    reservation_saved, sync_lead_status_to_ghl, sync_lead_to_ghl_on_create,
)

PICKUP = date(2026, 11, 4)


class _BookingFixture:
    """Reservation.rate is NOT NULL, so every booking needs a priced route."""

    def mute_background_signals(self):
        """The Lead/Reservation post_save handlers sync to GHL on worker threads,
        which touch the test DB from outside the test's connection and lock it.
        Same idiom as reservations/tests.py."""
        for handler, sender in (
            (reservation_saved, Reservation),
            (sync_lead_to_ghl_on_create, Lead),
            (sync_lead_status_to_ghl, Lead),
        ):
            post_save.disconnect(handler, sender=sender)
            self.addCleanup(
                lambda h=handler, s=sender: post_save.connect(h, sender=s)
            )

    def make_rate(self):
        vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"),
        )
        return Rate.objects.create(
            vehicle=vehicle, route=route,
            oneway_price=Decimal("195.00"), round_trip_price=Decimal("350.00"),
        )


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class AlreadyBookedGuardTests(_BookingFixture, TestCase):
    """already_booked_reservation(): identity match, scoped to the trip date."""

    def setUp(self):
        self.mute_background_signals()
        self.customer = Customer.objects.create(
            first_name="Kate", last_name="Zabel",
            email="kate@example.com", phone_number="(407) 555-0142",
        )
        self.reservation = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.make_rate(),
            base_price=Decimal("195.00"), total_price=Decimal("195.00"),
            status="confirmed",
        )
        Leg.objects.create(
            reservation=self.reservation, pickup_date=PICKUP, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", status="confirmed",
        )

    def _lead(self, **kw):
        defaults = dict(
            first_name="Kate", last_name="Zabel", email="kate@example.com",
            phone="4075550142", normalized_phone="4075550142",
            pickup_date=PICKUP, pickup_location="MCO", dropoff_location="Disney",
        )
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def test_matches_on_email(self):
        lead = self._lead(phone="", normalized_phone="")
        self.assertEqual(already_booked_reservation(lead), self.reservation)

    def test_matches_on_phone_when_email_differs(self):
        """The spouse/travel-agent case: booked under another email, same phone."""
        lead = self._lead(email="someone.else@example.com")
        self.assertEqual(already_booked_reservation(lead), self.reservation)

    def test_matches_phone_despite_different_formatting(self):
        """Customer.phone_number is unnormalised; the lead side is last-10."""
        self.customer.phone_number = "+1 407-555-0142"
        self.customer.save(update_fields=["phone_number"])
        lead = self._lead(email="nomatch@example.com")
        self.assertEqual(already_booked_reservation(lead), self.reservation)

    def test_unconverted_lead_still_matches(self):
        """The whole point: the Lead is NOT marked converted."""
        lead = self._lead()
        self.assertFalse(lead.converted)
        self.assertIsNotNone(already_booked_reservation(lead))

    def test_different_pickup_date_does_not_match(self):
        """A genuine new enquiry from a past customer must not be silenced."""
        lead = self._lead(pickup_date=PICKUP + timedelta(days=60))
        self.assertIsNone(already_booked_reservation(lead))

    def test_cancelled_reservation_does_not_match(self):
        self.reservation.status = "cancelled"
        self.reservation.save(update_fields=["status"])
        self.assertIsNone(already_booked_reservation(self._lead()))

    def test_american_spelling_of_cancelled_also_ignored(self):
        """Both spellings exist in the data."""
        self.reservation.status = "canceled"
        self.reservation.save(update_fields=["status"])
        self.assertIsNone(already_booked_reservation(self._lead()))

    def test_lead_without_pickup_date_is_left_alone(self):
        self.assertIsNone(already_booked_reservation(self._lead(pickup_date=None)))

    def test_stranger_does_not_match(self):
        lead = self._lead(
            email="stranger@example.com", phone="4075559999",
            normalized_phone="4075559999",
        )
        self.assertIsNone(already_booked_reservation(lead))

    def test_no_identifiers_does_not_match(self):
        lead = self._lead(email="", phone="", normalized_phone="")
        self.assertIsNone(already_booked_reservation(lead))


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class FollowUpSenderStopsForBookedCustomerTests(_BookingFixture, TestCase):
    """process_follow_up_batch cancels rather than sends when the person booked."""

    def setUp(self):
        self.mute_background_signals()
        self.customer = Customer.objects.create(
            first_name="Kate", last_name="Zabel",
            email="kate@example.com", phone_number="4075550142",
        )
        self.reservation = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.make_rate(),
            base_price=Decimal("195.00"), total_price=Decimal("195.00"),
            status="confirmed",
        )
        Leg.objects.create(
            reservation=self.reservation, pickup_date=PICKUP, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", status="confirmed",
        )
        self.lead = Lead.objects.create(
            first_name="Kate", last_name="Zabel", email="kate@example.com",
            phone="4075550142", normalized_phone="4075550142",
            pickup_date=PICKUP, pickup_location="MCO", dropoff_location="Disney",
        )
        self.task = FollowUpTask.objects.create(
            lead=self.lead, step_number=3, segment="airport_transfer",
            status=FollowUpTask.StatusChoices.PENDING,
            scheduled_at=timezone.now() - timedelta(minutes=5),
        )

    @patch("ghl_integration.timing.is_within_send_window", return_value=True)
    @patch("ghl_integration.services.GoHighLevelService.send_sms")
    def test_booked_customer_task_is_cancelled_not_sent(self, send_sms, _window):
        from ghl_integration.tasks import process_follow_up_batch

        process_follow_up_batch()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, FollowUpTask.StatusChoices.CANCELLED)
        self.assertEqual(self.task.cancel_reason, "already_booked")
        send_sms.assert_not_called()

    @patch("ghl_integration.timing.is_within_send_window", return_value=True)
    @patch("ghl_integration.services.GoHighLevelService.send_sms")
    def test_remaining_steps_are_cancelled_too(self, _send_sms, _window):
        """One booking should silence the whole sequence, not just today's step."""
        from ghl_integration.tasks import process_follow_up_batch

        later = FollowUpTask.objects.create(
            lead=self.lead, step_number=4, segment="airport_transfer",
            status=FollowUpTask.StatusChoices.PENDING,
            scheduled_at=timezone.now() + timedelta(days=1),
        )

        process_follow_up_batch()

        later.refresh_from_db()
        self.assertEqual(later.status, FollowUpTask.StatusChoices.CANCELLED)
