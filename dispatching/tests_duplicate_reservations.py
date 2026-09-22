"""
Duplicate Reservations page: the safe / check first / hold verdicts, and the
delete endpoint's live re-check.

The verdict rules are the whole point — a superuser is going to select every
"safe" row and delete them in one go, so each thing that makes a booking NOT
safe has a test that proves it is held.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from ops.models import CommunicationAttempt, OperationalTask
from ops.services import create_task
from payment.models import Payment
from rates.models import Location, Rate, Route, Vehicle
from reservations.duplicates import HOLD, REVIEW, SAFE, build_groups, verdict_for
from reservations.models import Customer, Leg, Reservation


def _aware(dt):
    return timezone.make_aware(dt, timezone.get_current_timezone())


class _DupeFixture:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.origin = Location.objects.create(name="MCO")
        cls.dest = Location.objects.create(name="Pop Century")
        cls.route = Route.objects.create(origin=cls.origin, destination=cls.dest)
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("140.00"), round_trip_price=Decimal("275.00"),
        )
        cls.suv_rate = Rate.objects.create(
            vehicle=cls.suv, route=cls.route,
            oneway_price=Decimal("168.00"), round_trip_price=Decimal("330.00"),
        )
        cls.now = _aware(datetime(2026, 9, 22, 12, 0))
        cls.trip_day = date(2026, 10, 10)
        cls.staff = User.objects.create_user(username="dupe_staff", is_staff=True)

    def _customer(self, **overrides):
        defaults = {
            "first_name": "Jeff", "last_name": "Hund",
            "email": "jeff@example.com", "phone_number": "(407) 555-0142",
            "zipcode": "32801",
        }
        defaults.update(overrides)
        return Customer.objects.create(**defaults)

    def _reservation(self, customer, *, created_at, dates=None, rate=None, paid=False,
                     created_by=None, total=Decimal("275.00"), status="confirmed"):
        res = Reservation.objects.create(
            customer=customer, rate=rate or self.rate, vehicle=(rate or self.rate).vehicle,
            trip_type="round_trip", base_price=total, total_price=total,
            status=status, created_by=created_by,
        )
        Reservation.objects.filter(pk=res.pk).update(created_at=created_at)
        res.refresh_from_db()
        for d in dates or [self.trip_day, self.trip_day + timedelta(days=7)]:
            Leg.objects.create(
                reservation=res, pickup_date=d, pickup_time=time(11, 15),
                pickup_location="MCO", dropoff_location="Pop Century", status="confirmed",
            )
        if paid:
            Payment.objects.create(
                reservation=res, customer=customer, amount=total,
                status="paid", payment_type="pay_now",
            )
        return res

    def _pair(self, customer=None, **unpaid_overrides):
        """A paid booking created 2 days ago and an unpaid twin from 3 days ago,
        both on the same customer."""
        cust = customer or self._customer()
        paid = self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        unpaid_kwargs = {"created_at": self.now - timedelta(days=3)}
        unpaid_kwargs.update(unpaid_overrides)
        unpaid = self._reservation(cust, **unpaid_kwargs)
        return paid, unpaid

    def _verdict(self, unpaid):
        groups = build_groups(now=self.now)
        for g in groups:
            for r in g.unpaid:
                if r.pk == unpaid.pk:
                    return r.dupe_verdict
        return None


class VerdictTests(_DupeFixture, TestCase):

    def test_identical_twin_is_safe(self):
        _paid, unpaid = self._pair()
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, SAFE)
        self.assertEqual(v.reasons, [])

    def test_time_and_price_tweaks_still_safe(self):
        """Same dates and route, but a different pickup time and price — that is
        the customer fixing their booking, the classic duplicate."""
        _paid, unpaid = self._pair(total=Decimal("330.00"))
        Leg.objects.filter(reservation=unpaid).update(pickup_time=time(10, 20))
        self.assertEqual(self._verdict(unpaid).tier, SAFE)

    def test_different_route_is_review(self):
        _paid, unpaid = self._pair(rate=self.suv_rate, total=Decimal("330.00"))
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, REVIEW)
        self.assertIn("Different route or vehicle", v.reasons[0])

    def test_extra_pickup_date_is_hold(self):
        cust = self._customer()
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True,
                          dates=[self.trip_day])
        unpaid = self._reservation(cust, created_at=self.now - timedelta(days=3),
                                   dates=[self.trip_day, self.trip_day + timedelta(days=5)])
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertIn("Oct 15", v.reasons[0])

    def test_created_after_paid_is_hold(self):
        _paid, unpaid = self._pair(created_at=self.now - timedelta(days=1))
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("Started after the paid booking" in r for r in v.reasons))

    def test_staff_created_is_hold(self):
        _paid, unpaid = self._pair(created_by=self.staff)
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("dupe_staff" in r for r in v.reasons))

    def test_younger_than_a_day_is_hold(self):
        _paid, unpaid = self._pair(created_at=self.now - timedelta(hours=5))
        # Also started after the paid one; the age reason must be there regardless.
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("less than 24 hours" in r for r in v.reasons))

    def test_payment_attempt_is_hold(self):
        _paid, unpaid = self._pair()
        Payment.objects.create(reservation=unpaid, customer=unpaid.customer,
                               amount=Decimal("275.00"), status="pending", payment_type="pay_now")
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("checkout or refund" in r for r in v.reasons))

    def test_driver_assigned_is_hold(self):
        _paid, unpaid = self._pair()
        driver = Driver.objects.create(
            profile=User.objects.create_user(username="dupe_driver"), driver_type="inhouse")
        Leg.objects.filter(reservation=unpaid).update(driver=driver)
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("driver is already assigned" in r for r in v.reasons))

    def test_staff_contact_is_hold(self):
        _paid, unpaid = self._pair()
        task = create_task(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE, title="Unpaid",
            reservation=unpaid, due_at=self.now,
        )
        CommunicationAttempt.objects.create(
            task=task, channel="sms", outcome="sent", staff_user=self.staff)
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("already reached out" in r for r in v.reasons))

    def test_family_member_sharing_phone_is_hold(self):
        cust = self._customer(first_name="Kathleen", last_name="Carlson")
        spouse = self._customer(first_name="Jerry", last_name="Carlson", email="jerry@example.com")
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        unpaid = self._reservation(spouse, created_at=self.now - timedelta(days=3))
        v = self._verdict(unpaid)
        self.assertEqual(v.tier, HOLD)
        self.assertTrue(any("Different first name" in r for r in v.reasons))

    def test_nickname_on_second_customer_row_is_safe(self):
        cust = self._customer(first_name="Jeffery")
        twin = self._customer(first_name="Jeff", email="jeff2@example.com")
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        unpaid = self._reservation(twin, created_at=self.now - timedelta(days=3))
        self.assertEqual(self._verdict(unpaid).tier, SAFE)

    def test_card_saved_counts_as_paid_twin(self):
        cust = self._customer()
        kept = self._reservation(cust, created_at=self.now - timedelta(days=2))
        Payment.objects.create(reservation=kept, customer=cust, amount=Decimal("275.00"),
                               status="card_saved", payment_type="pay_later")
        unpaid = self._reservation(cust, created_at=self.now - timedelta(days=3))
        groups = build_groups(now=self.now)
        self.assertEqual(len(groups), 1)
        self.assertEqual([r.pk for r in groups[0].paid], [kept.pk])
        self.assertEqual(groups[0].unpaid[0].dupe_verdict.tier, SAFE)

    def test_two_paid_bookings_are_not_a_group(self):
        cust = self._customer()
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        self._reservation(cust, created_at=self.now - timedelta(days=3), paid=True)
        self.assertEqual(build_groups(now=self.now), [])

    def test_cancelled_twin_is_ignored(self):
        cust = self._customer()
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        self._reservation(cust, created_at=self.now - timedelta(days=3), status="cancelled")
        self.assertEqual(build_groups(now=self.now), [])

    def test_unpaid_rows_sorted_safe_first(self):
        cust = self._customer()
        self._reservation(cust, created_at=self.now - timedelta(days=2), paid=True)
        held = self._reservation(cust, created_at=self.now - timedelta(days=4), created_by=self.staff)
        safe = self._reservation(cust, created_at=self.now - timedelta(days=3))
        group = build_groups(now=self.now)[0]
        self.assertEqual([r.pk for r in group.unpaid], [safe.pk, held.pk])
        self.assertEqual(group.safe_count, 1)


class PageAndDeleteTests(_DupeFixture, TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="dupe_admin", email="a@example.com", password="x")
        self.client.force_login(self.admin)

    def test_page_shows_tier_counts(self):
        _paid, unpaid = self._pair()
        self._pair(rate=self.suv_rate, total=Decimal("330.00"),
                   customer=self._customer(last_name="Other", phone_number="4075550199"))
        resp = self.client.get(reverse("duplicate_reservations"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["safe_count"], 1)
        self.assertEqual(resp.context["review_count"], 1)
        self.assertEqual(resp.context["hold_count"], 0)
        self.assertContains(resp, "Safe to delete")
        self.assertContains(resp, f'data-uuid="{unpaid.uuid}"')

    def _delete(self, res):
        return self.client.post(
            reverse("cancel_duplicate_reservation"),
            data=json.dumps({"reservation_uuid": str(res.uuid)}),
            content_type="application/json",
        )

    def test_deletes_a_safe_twin(self):
        _paid, unpaid = self._pair()
        resp = self._delete(unpaid)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["tier"], SAFE)
        self.assertFalse(Reservation.objects.filter(pk=unpaid.pk).exists())

    def test_refuses_paid_and_card_saved(self):
        paid, _unpaid = self._pair()
        self.assertEqual(self._delete(paid).status_code, 400)
        cust = self._customer(phone_number="4075550111")
        saved = self._reservation(cust, created_at=self.now - timedelta(days=2))
        Payment.objects.create(reservation=saved, customer=cust, amount=Decimal("1.00"),
                               status="card_saved", payment_type="pay_later")
        self.assertEqual(self._delete(saved).status_code, 400)
        self.assertTrue(Reservation.objects.filter(pk=saved.pk).exists())

    def test_refuses_when_paid_twin_is_gone(self):
        """A stale tab: the paid booking was cancelled after the page loaded."""
        paid, unpaid = self._pair()
        paid.status = "cancelled"
        paid.save(update_fields=["status"])
        resp = self._delete(unpaid)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(Reservation.objects.filter(pk=unpaid.pk).exists())

    def test_refuses_lone_unpaid_booking(self):
        cust = self._customer(phone_number="4075550122")
        lone = self._reservation(cust, created_at=self.now - timedelta(days=3))
        self.assertEqual(self._delete(lone).status_code, 409)

    def test_non_superuser_is_refused(self):
        self.client.force_login(self.staff)
        _paid, unpaid = self._pair()
        self.assertEqual(self._delete(unpaid).status_code, 403)
        self.assertTrue(Reservation.objects.filter(pk=unpaid.pk).exists())

    def test_verdict_for_matches_page(self):
        _paid, unpaid = self._pair(created_by=self.staff)
        self.assertEqual(verdict_for(unpaid, now=self.now).tier, HOLD)
