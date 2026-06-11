"""Schedule Sandbox tests — the leak invariant, the front door, the tripwire,
the draft lifecycle, and publish conflict detection.

Run with:  ./manage.py test dispatching.tests_sandbox

THE invariant under test: while a date is held by an active draft, nothing a
granted sandbox user does through any dispatch surface may change Leg.driver —
because Leg.driver IS what the driver portal renders. Drivers see changes only
at publish.
"""
import json
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.assignment import (
    SandboxLeakError,
    sanctioned_live_write,
    set_leg_driver,
)
from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import (
    Customer,
    DraftAssignment,
    Leg,
    Reservation,
    ScheduleDraft,
    ScheduleDraftEvent,
)

FUTURE = timezone.localdate() + timedelta(days=7)


def _make_driver(username):
    user = User.objects.create_user(username=username, first_name=username.title())
    return Driver.objects.create(profile=user, driver_type="inhouse")


def _grant_sandbox(user):
    user.user_permissions.add(
        Permission.objects.get(codename="use_schedule_sandbox")
    )
    return User.objects.get(pk=user.pk)  # fresh perms cache


class _SandboxFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567",
        )
        cls.driver_a = _make_driver("sb_driver_a")
        cls.driver_b = _make_driver("sb_driver_b")

        cls.granted = _grant_sandbox(
            User.objects.create_user("sb_granted", password="x", is_staff=True)
        )
        cls.plain = User.objects.create_user("sb_plain", password="x", is_staff=True)
        cls.manager = User.objects.create_superuser("sb_manager", password="x")

    def _leg(self, pickup_date=FUTURE, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )
        defaults = dict(
            reservation=res, pickup_date=pickup_date, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _hold(self, target_date=FUTURE, created_by=None):
        return ScheduleDraft.objects.create(
            schedule_date=target_date,
            state=ScheduleDraft.State.DRAFT,
            created_by=created_by or self.granted,
        )

    def _post_json(self, name, payload):
        return self.client.post(
            reverse(name), json.dumps(payload), content_type="application/json"
        )


class FrontDoorUnitTests(_SandboxFixtureMixin, TestCase):
    """set_leg_driver routing: staged vs live vs live_override mirror."""

    def test_staged_when_held_and_granted(self):
        leg = self._leg()
        draft = self._hold()
        mode, d = set_leg_driver(leg, self.driver_a, self.granted)
        self.assertEqual(mode, "staged")
        self.assertEqual(d.id, draft.id)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver)  # THE invariant: live untouched
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertEqual(da.proposed_driver_id, self.driver_a.id)

    def test_staged_unassign_is_distinct_from_no_opinion(self):
        leg = self._leg()
        with sanctioned_live_write():
            leg.driver = self.driver_a
            leg.save(update_fields=["driver"])
        draft = self._hold()
        mode, _ = set_leg_driver(leg, None, self.granted)
        self.assertEqual(mode, "staged")
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)  # live keeps driver
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertIsNone(da.proposed_driver_id)  # row says "unassigned"

    def test_live_when_no_draft(self):
        leg = self._leg()
        mode, d = set_leg_driver(leg, self.driver_a, self.granted)
        self.assertEqual(mode, "live")
        self.assertIsNone(d)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)
        self.assertEqual(leg.driver_assigned_by, self.granted)

    def test_non_granted_writes_live_even_when_held(self):
        leg = self._leg()
        self._hold()
        mode, _ = set_leg_driver(leg, self.driver_a, self.plain)
        self.assertEqual(mode, "live")
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)

    def test_live_override_writes_live_and_mirrors_into_overlay(self):
        leg = self._leg()
        draft = self._hold()
        mode, d = set_leg_driver(leg, self.driver_a, self.granted, live_override=True)
        self.assertEqual(mode, "live")
        self.assertEqual(d.id, draft.id)
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)
        da = DraftAssignment.objects.get(draft=draft, leg=leg)
        self.assertEqual(da.proposed_driver_id, self.driver_a.id)  # mirror


class TripwireTests(_SandboxFixtureMixin, TestCase):
    """Direct Leg.driver writes on a held day must fail loudly in tests."""

    def test_direct_write_on_held_day_raises(self):
        leg = self._leg()
        self._hold()
        leg.driver = self.driver_a
        with self.assertRaises(SandboxLeakError):
            leg.save(update_fields=["driver"])

    def test_full_save_with_driver_change_on_held_day_raises(self):
        leg = self._leg()
        self._hold()
        leg.driver = self.driver_a
        with self.assertRaises(SandboxLeakError):
            leg.save()

    def test_unrelated_update_fields_skip_the_wire(self):
        leg = self._leg()
        self._hold()
        leg.private_notes = "gate code 1234"
        leg.save(update_fields=["private_notes"])  # no raise

    def test_full_save_without_driver_change_passes(self):
        leg = self._leg()
        self._hold()
        leg.passenger_count = 3
        leg.save()  # driver unchanged -> no raise

    def test_sanctioned_block_allows_live_write(self):
        leg = self._leg()
        self._hold()
        with sanctioned_live_write():
            leg.driver = self.driver_a
            leg.save(update_fields=["driver"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)

    def test_no_draft_no_trip(self):
        leg = self._leg()
        leg.driver = self.driver_a
        leg.save(update_fields=["driver"])  # no raise

    def test_past_dates_never_trip(self):
        past = timezone.localdate() - timedelta(days=3)
        leg = self._leg(pickup_date=past)
        self._hold(target_date=past)
        leg.driver = self.driver_a
        leg.save(update_fields=["driver"])  # historical edits are exempt


class EndpointLeakTests(_SandboxFixtureMixin, TestCase):
    """Every dispatch surface stages (not writes) on a held day for a granted user."""

    def setUp(self):
        self.client.force_login(self.granted)

    def test_manual_assign_stages(self):
        leg = self._leg()
        draft = self._hold()
        r = self._post_json("update_leg_assignment",
                            {"leg_id": leg.id, "field": "driver", "value": self.driver_a.id})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["held"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver)
        self.assertTrue(DraftAssignment.objects.filter(draft=draft, leg=leg).exists())
        # The driver portal's source of truth is untouched:
        self.assertEqual(Leg.objects.filter(driver=self.driver_a).count(), 0)

    def test_manual_assign_live_override_reaches_driver_and_mirrors(self):
        leg = self._leg()
        draft = self._hold()
        r = self._post_json("update_leg_assignment",
                            {"leg_id": leg.id, "field": "driver",
                             "value": self.driver_a.id, "live_override": True})
        self.assertTrue(r.json()["success"])
        self.assertFalse(r.json()["held"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)
        self.assertTrue(DraftAssignment.objects.filter(draft=draft, leg=leg).exists())

    def test_plain_dispatcher_writes_live_on_held_day(self):
        self.client.force_login(self.plain)
        leg = self._leg()
        self._hold()
        r = self._post_json("update_leg_assignment",
                            {"leg_id": leg.id, "field": "driver", "value": self.driver_a.id})
        self.assertTrue(r.json()["success"])
        self.assertFalse(r.json()["held"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)

    def test_takeback_stages(self):
        leg = self._leg()
        self._hold()
        r = self._post_json("execute_takeback",
                            {"leg_id": leg.id, "driver_id": self.driver_a.id,
                             "date": FUTURE.isoformat()})
        self.assertTrue(r.json()["success"])
        self.assertTrue(r.json()["held"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver)

    def test_swap_stages_all_moves(self):
        leg1, leg2 = self._leg(), self._leg()
        draft = self._hold()
        r = self._post_json("execute_swap",
                            {"date": FUTURE.isoformat(),
                             "moves": [
                                 {"leg_id": leg1.id, "to_driver_id": self.driver_a.id},
                                 {"leg_id": leg2.id, "to_driver_id": self.driver_b.id},
                             ]})
        body = r.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["held"])
        self.assertEqual(body["applied"], 2)
        leg1.refresh_from_db(); leg2.refresh_from_db()
        self.assertIsNone(leg1.driver)
        self.assertIsNone(leg2.driver)
        self.assertEqual(
            DraftAssignment.objects.filter(draft=draft).count(), 2
        )


class LifecycleTests(_SandboxFixtureMixin, TestCase):
    """hold → stage → submit → request changes → resubmit → publish."""

    def test_full_lifecycle(self):
        leg = self._leg()

        # Dispatcher holds the day + stages an assignment
        self.client.force_login(self.granted)
        r = self._post_json("open_draft", {"date": FUTURE.isoformat()})
        self.assertTrue(r.json()["success"])
        draft_id = r.json()["draft_id"]
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "driver", "value": self.driver_a.id})
        leg.refresh_from_db()
        self.assertIsNone(leg.driver)  # invisible until publish

        # Submit for review
        r = self._post_json("submit_draft", {"draft_id": draft_id, "note": "first pass"})
        self.assertTrue(r.json()["success"])
        self.assertEqual(ScheduleDraft.objects.get(id=draft_id).state,
                         ScheduleDraft.State.IN_REVIEW)

        # Manager requests changes (note required)
        self.client.force_login(self.manager)
        r = self._post_json("reject_draft", {"draft_id": draft_id, "note": "swap A for B"})
        self.assertTrue(r.json()["success"])
        self.assertEqual(ScheduleDraft.objects.get(id=draft_id).state,
                         ScheduleDraft.State.CHANGES_REQUESTED)

        # Dispatcher resubmits, manager publishes
        self.client.force_login(self.granted)
        self._post_json("submit_draft", {"draft_id": draft_id})
        self.client.force_login(self.manager)
        r = self._post_json("publish_draft", {"draft_id": draft_id})
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["applied"], 1)

        # NOW the driver sees it
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)
        draft = ScheduleDraft.objects.get(id=draft_id)
        self.assertEqual(draft.state, ScheduleDraft.State.PUBLISHED)
        kinds = list(draft.events.values_list("event_type", flat=True))
        for expected in ("created", "edited", "submitted", "rejected", "published"):
            self.assertIn(expected, kinds)

    def test_discard_leaves_live_untouched(self):
        leg = self._leg()
        self.client.force_login(self.granted)
        r = self._post_json("open_draft", {"date": FUTURE.isoformat()})
        draft_id = r.json()["draft_id"]
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "driver", "value": self.driver_a.id})
        r = self._post_json("discard_draft", {"draft_id": draft_id})
        self.assertTrue(r.json()["success"])
        leg.refresh_from_db()
        self.assertIsNone(leg.driver)
        self.assertEqual(ScheduleDraft.objects.get(id=draft_id).state,
                         ScheduleDraft.State.DISCARDED)


class PublishConflictTests(_SandboxFixtureMixin, TestCase):
    """Live changes under the draft block publish (409) unless forced."""

    def test_conflict_blocks_then_force_overwrites(self):
        leg = self._leg()

        self.client.force_login(self.granted)
        r = self._post_json("open_draft", {"date": FUTURE.isoformat()})
        draft_id = r.json()["draft_id"]
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "driver", "value": self.driver_a.id})

        # A non-granted dispatcher changes the same leg LIVE under the draft
        self.client.force_login(self.plain)
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "driver", "value": self.driver_b.id})
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_b.id)

        # Publish blocks with a named conflict
        self.client.force_login(self.manager)
        r = self._post_json("publish_draft", {"draft_id": draft_id})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["conflicts"])

        # Force overwrites with the staged value
        r = self._post_json("publish_draft", {"draft_id": draft_id, "force": True})
        self.assertTrue(r.json()["success"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.driver_a.id)
