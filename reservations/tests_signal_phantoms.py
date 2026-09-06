"""The assignment audit trail must record only real driver changes.

Regression tests for the signals.py skip-guard bug (scheduling redesign, 00
§A4.6 / 04 §2 Build 1b): a ``save(update_fields=...)`` naming neither 'driver'
nor 'status' skips the pre-save snapshot, and the post-save logger then compared
the live driver against an EMPTY dict — fabricating a ``driver_assigned`` row
with old=NULL on every such save. The nightly confirmation-SMS job (one
``update_fields=["confirmation_sms_sent_at"]`` save per sent leg) was the
biggest writer: 30.8% of all assignment rows in the audit log were phantoms.

Run with:  ./manage.py test reservations.tests_signal_phantoms
"""
from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import AuditLog, Customer, Leg, Reservation

TD = date(2026, 6, 1)

ASSIGN_ACTIONS = ("driver_assigned", "driver_unassigned")


def assignment_rows():
    return AuditLog.objects.filter(model_name="Leg", action__in=ASSIGN_ACTIONS)


class PhantomAssignmentRowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        # Reservation post_save spawns a real email thread; keep it out of tests.
        super().setUpClass()
        patcher = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls):
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            route=cls.route, vehicle=cls.vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user(username="alex", first_name="Alex"),
            driver_type="inhouse")
        cls.other_driver = Driver.objects.create(
            profile=User.objects.create_user(username="sam", first_name="Sam"),
            driver_type="inhouse")
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat@example.com",
            phone_number="5550001111")
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, vehicle=cls.vtype,
            rate=cls.rate, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )
        cls.leg = Leg.objects.create(
            reservation=cls.reservation, pickup_date=TD, pickup_time=time(9, 0),
            pickup_location="Disney's Grand Floridian Resort, Lake Buena Vista, FL",
            dropoff_location="Orlando International Airport (MCO)",
            driver=cls.driver, route=cls.route, status="confirmed",
        )

    # ── the bug: unrelated update_fields saves must write NOTHING ──

    def test_confirmation_sms_run_writes_no_assignment_rows(self):
        """Re-run the nightly confirmation-SMS path; the assignment trail must
        not move. Before the fix, every sent leg with a driver minted one
        phantom driver_assigned row (old=NULL)."""
        from dispatching import confirmation_sms

        before = assignment_rows().count()
        with patch.object(confirmation_sms, "send_confirmation_via_twilio",
                          return_value=(True, None)):
            result = confirmation_sms.send_confirmations_for_date(TD)
        self.assertEqual(result["sent"], 1, result)   # the path really ran
        self.leg.refresh_from_db()
        self.assertIsNotNone(self.leg.confirmation_sms_sent_at)
        self.assertEqual(assignment_rows().count(), before)

    def test_unrelated_update_fields_save_writes_no_assignment_rows(self):
        before = assignment_rows().count()
        self.leg.private_notes = "call on arrival"
        self.leg.save(update_fields=["private_notes"])
        self.assertEqual(assignment_rows().count(), before)

    # ── the control: real driver changes must still be logged ──

    def test_real_assignment_is_still_logged(self):
        leg = Leg.objects.create(
            reservation=self.reservation, pickup_date=TD, pickup_time=time(12, 0),
            pickup_location="Orlando International Airport (MCO)",
            dropoff_location="Disney's Polynesian Village Resort",
            route=self.route, status="confirmed",
        )
        self.addCleanup(leg.delete)
        before = assignment_rows().count()
        leg.driver = self.driver
        leg.save()
        rows = assignment_rows().order_by("-id")
        self.assertEqual(rows.count(), before + 1)
        newest = rows.first()
        self.assertEqual(newest.action, "driver_assigned")
        self.assertEqual(newest.object_id, leg.id)
        self.assertIsNone(newest.old_value)
        self.assertEqual(newest.new_value, str(self.driver.id))

    def test_reassignment_via_update_fields_driver_is_still_logged(self):
        before = assignment_rows().count()
        self.leg.driver = self.other_driver
        self.leg.save(update_fields=["driver"])
        rows = assignment_rows().order_by("-id")
        self.assertEqual(rows.count(), before + 1)
        newest = rows.first()
        self.assertEqual(newest.action, "driver_assigned")
        self.assertEqual(newest.old_value, str(self.driver.id))
        self.assertEqual(newest.new_value, str(self.other_driver.id))

    def test_unassignment_is_still_logged(self):
        before = assignment_rows().count()
        self.leg.driver = None
        self.leg.save()
        rows = assignment_rows().order_by("-id")
        self.assertEqual(rows.count(), before + 1)
        self.assertEqual(rows.first().action, "driver_unassigned")

    def test_no_op_driver_save_writes_nothing(self):
        """A save that does not change the driver logs nothing, snapshot or not."""
        before = assignment_rows().count()
        self.leg.save()
        self.assertEqual(assignment_rows().count(), before)
