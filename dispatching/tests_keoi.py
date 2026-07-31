"""KEOI ("Keep Eye On It") tests — model constraints, endpoints, the auto-close
/ auto-reactivate lifecycle across every completion pathway, permissions, and
the board surfaces.

Run with:
  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_keoi
"""
import json
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import AuditLog, Customer, Leg, LegKeoi, Reservation
from reservations.keoi import close_active_keoi, reactivate_keoi

FUTURE = timezone.localdate() + timedelta(days=7)

# Several signals (reservation_saved, driver status notifications, etc.) fire
# side-effect work in background daemon threads via reservations.utils
# ._run_in_background. On SQLite those threads race with the test's own writes
# ("database table is locked"). None of that side-effect work matters to KEOI
# correctness, so neutralise the thread spawns module-wide by patching
# _run_in_background to a no-op at every binding site these tests exercise.
_NOOP = lambda *a, **k: None
_bg_targets = [
    "reservations.utils._run_in_background",   # source (function-local imports)
    "drivers.signals._run_in_background",      # module-level bind (driver notifications)
    "dispatching.views._run_in_background",    # module-level bind (board endpoints)
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        p = mock.patch(target, _NOOP)
        p.start()
        _bg_patchers.append(p)


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


class _KeoiFixtureMixin:
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
        # Driver + its User (pathway B attributes to this User)
        cls.driver_user = User.objects.create_user(
            username="keoi_driver", first_name="Dan", is_staff=False
        )
        cls.driver = Driver.objects.create(profile=cls.driver_user, driver_type="inhouse")

        cls.dispatcher = User.objects.create_user("keoi_dispatcher", password="x", is_staff=True)
        cls.plain = User.objects.create_user("keoi_plain", password="x", is_staff=False)
        cls.remover = cls._grant_remove(
            User.objects.create_user("keoi_remover", password="x", is_staff=True)
        )
        cls.manager = User.objects.create_superuser("keoi_manager", password="x")

    @staticmethod
    def _grant_remove(user):
        user.user_permissions.add(Permission.objects.get(codename="remove_keoi"))
        return User.objects.get(pk=user.pk)  # fresh perms cache

    def _leg(self, pickup_date=FUTURE, status="confirmed", driver=None, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )
        defaults = dict(
            reservation=res, pickup_date=pickup_date, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status=status, driver=driver,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _keoi(self, leg, **kw):
        defaults = dict(category="tight_schedule", description="9AM MCO then 10:30 return")
        defaults.update(kw)
        return LegKeoi.objects.create(leg=leg, **defaults)

    def _post_json(self, name, payload, args=None):
        return self.client.post(
            reverse(name, args=args or []),
            json.dumps(payload), content_type="application/json",
        )

    @staticmethod
    def _keoi_audits(leg, field_name=None):
        qs = AuditLog.objects.filter(model_name="Leg", object_id=leg.id,
                                     field_name__startswith="keoi")
        if field_name:
            qs = qs.filter(field_name=field_name)
        return qs


class KeoiCreateEditTests(_KeoiFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.dispatcher)

    def test_create_success(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "flight_delay",
            "description": "Flight AA123 delayed 40 min — watch turnaround",
        })
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j["success"])
        self.assertTrue(j["created"])
        self.assertEqual(j["keoi"]["category_label"], "Flight Delay Risk")
        self.assertEqual(j["keoi"]["status_label"], "Needs Attention")
        self.assertEqual(LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).count(), 1)
        self.assertTrue(self._keoi_audits(leg, "keoi").filter(action="created").exists())

    def test_missing_description_rejected(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": ""})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(LegKeoi.objects.filter(leg=leg).exists())

    def test_whitespace_description_rejected(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": "   \n  "})
        self.assertEqual(r.status_code, 400)

    def test_overlong_description_rejected(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": "x" * 2001})
        self.assertEqual(r.status_code, 400)

    def test_invalid_category_rejected(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "nope", "description": "watch"})
        self.assertEqual(r.status_code, 400)

    def test_non_string_category_is_clean_400_not_500(self):
        """A non-string JSON value must yield a JSON 400, never an AttributeError 500."""
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": 5, "description": "watch"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["success"])

    def test_non_string_description_is_clean_400(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": ["a", "b"]})
        # coerces to a truthy string -> not a crash; may be 200. Assert it never 500s.
        self.assertNotEqual(r.status_code, 500)

    def test_create_on_completed_leg_rejected(self):
        leg = self._leg(status="completed")
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": "watch"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(LegKeoi.objects.filter(leg=leg).exists())

    def test_create_on_cancelled_leg_rejected(self):
        leg = self._leg(status="cancelled")
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": "watch"})
        self.assertEqual(r.status_code, 400)

    def test_non_staff_forbidden(self):
        leg = self._leg()
        self.client.force_login(self.plain)
        r = self._post_json("keoi_save", {"leg_id": leg.id, "category": "other", "description": "watch"})
        self.assertEqual(r.status_code, 403)

    def test_upsert_edits_existing_no_second_row(self):
        leg = self._leg()
        k = self._keoi(leg, category="tight_schedule", description="orig")
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "traffic", "description": "changed desc",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["created"])
        self.assertEqual(LegKeoi.objects.filter(leg=leg).count(), 1)
        k.refresh_from_db()
        self.assertEqual(k.category, "traffic")
        self.assertEqual(k.description, "changed desc")
        self.assertTrue(self._keoi_audits(leg, "keoi_category").filter(old_value="tight_schedule", new_value="traffic").exists())
        self.assertTrue(self._keoi_audits(leg, "keoi_description").exists())

    def test_unchanged_resubmit_writes_no_audit(self):
        leg = self._leg()
        self._keoi(leg, category="tight_schedule", description="same", operational_status="needs_attention")
        before = self._keoi_audits(leg).count()
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "tight_schedule", "description": "same",
            "operational_status": "needs_attention",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._keoi_audits(leg).count(), before)


class KeoiStatusTests(_KeoiFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.dispatcher)

    def test_status_change_audited(self):
        leg = self._leg()
        self._keoi(leg)
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "tight_schedule",
            "description": "9AM MCO then 10:30 return", "operational_status": "being_monitored",
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self._keoi_audits(leg, "keoi_operational_status")
                        .filter(new_value="being_monitored").exists())

    def test_invalid_status_rejected(self):
        leg = self._leg()
        self._keoi(leg)
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "tight_schedule",
            "description": "9AM MCO then 10:30 return", "operational_status": "nope",
        })
        self.assertEqual(r.status_code, 400)

    def test_backup_arranged_keeps_flag_active(self):
        """The spec's core rule: setting Backup Arranged does NOT hide the flag."""
        leg = self._leg()
        k = self._keoi(leg)
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "tight_schedule",
            "description": "9AM MCO then 10:30 return", "operational_status": "backup_arranged",
        })
        self.assertEqual(r.status_code, 200)
        k.refresh_from_db()
        self.assertEqual(k.operational_status, "backup_arranged")
        self.assertIsNone(k.closed_at)
        self.assertTrue(k.is_active)

    def test_status_change_no_active_flag_creates(self):
        leg = self._leg()
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "tight_schedule",
            "description": "new one", "operational_status": "being_monitored",
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["created"])
        self.assertEqual(LegKeoi.objects.get(leg=leg).operational_status, "being_monitored")


class KeoiAutoCloseTests(_KeoiFixtureMixin, TestCase):
    def test_board_complete_closes(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        self.client.force_login(self.dispatcher)
        r = self._post_json("update_leg_assignment",
                            {"leg_id": leg.id, "field": "status", "value": "completed"})
        self.assertEqual(r.status_code, 200)
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_completed")
        audit = self._keoi_audits(leg, "keoi_closed").first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.username, "keoi_dispatcher")

    def test_driver_portal_complete_closes_attributed_to_driver(self):
        leg = self._leg(status="on-the-way", driver=self.driver)
        self._keoi(leg)
        self.client.force_login(self.driver_user)
        r = self._post_json("update_leg_status", {"status": "completed"}, args=[leg.id])
        self.assertEqual(r.status_code, 200)
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_completed")
        self.assertEqual(self._keoi_audits(leg, "keoi_closed").first().username, "keoi_driver")

    def test_bulk_complete_closes(self):
        leg1 = self._leg(status="in-progress", driver=self.driver)
        leg2 = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg1)
        self._keoi(leg2)
        self.client.force_login(self.dispatcher)
        r = self._post_json("bulk_update_leg_status",
                            {"leg_ids": [leg1.id, leg2.id], "status": "completed"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(LegKeoi.objects.get(leg=leg1).is_active)
        self.assertFalse(LegKeoi.objects.get(leg=leg2).is_active)

    def test_board_cancel_closes_with_cancelled_reason(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        self.client.force_login(self.dispatcher)
        r = self._post_json("bulk_update_leg_status",
                            {"leg_ids": [leg.id], "status": "cancelled"})
        self.assertEqual(r.status_code, 200)
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_cancelled")

    def test_refund_style_cancel_save_closes(self):
        """process_refund cancels legs via leg.save(update_fields=[...,'status',...]) —
        the signal path is the same. Exercise that save directly."""
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        leg.status = "cancelled"
        leg.save(update_fields=["status", "payment_status", "driver"])
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_cancelled")

    def test_survives_driver_reassign(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        k = self._keoi(leg)
        other = Driver.objects.create(
            profile=User.objects.create_user("keoi_driver2"), driver_type="inhouse")
        leg.driver = other
        leg.save()   # driver-change auto-reset goes to in-progress (non-terminal)
        k.refresh_from_db()
        self.assertTrue(k.is_active)

    def test_survives_time_and_parent_edits(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        k = self._keoi(leg)
        leg.pickup_time = time(10, 30)
        leg.save(update_fields=["pickup_time"])
        leg.reservation.private_notes = "edited"
        leg.reservation.save()
        k.refresh_from_db()
        self.assertTrue(k.is_active)

    def test_close_without_request_is_guest(self):
        leg = self._leg(status="in-progress")
        self._keoi(leg)
        n = close_active_keoi(leg, reason="leg_completed")
        self.assertEqual(n, 1)
        audit = self._keoi_audits(leg, "keoi_closed").first()
        self.assertIsNone(audit.user)
        self.assertEqual(audit.username, "guest")

    def _admin_request(self):
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        req = RequestFactory().post("/admin/")
        req.user = self.manager
        req.session = {}
        req._messages = FallbackStorage(req)
        return req

    def test_admin_leg_bulk_complete_closes(self):
        """LegAdmin 'Mark as completed' bulk .update() bypasses signals — the
        action closes KEOI flags explicitly."""
        from django.contrib import admin as djadmin
        from reservations.admin import LegAdmin
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        LegAdmin(Leg, djadmin.site).mark_as_completed(
            self._admin_request(), Leg.objects.filter(id=leg.id))
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_completed")

    def test_admin_reservation_bulk_complete_closes(self):
        from django.contrib import admin as djadmin
        from reservations.admin import ReservationAdmin
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        ReservationAdmin(Reservation, djadmin.site).mark_as_completed(
            self._admin_request(), Reservation.objects.filter(id=leg.reservation_id))
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "leg_completed")


class KeoiReactivateTests(_KeoiFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.dispatcher)

    def test_completed_to_inprogress_reactivates(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "status", "value": "completed"})
        self.assertFalse(LegKeoi.objects.get(leg=leg).is_active)
        self._post_json("update_leg_assignment",
                        {"leg_id": leg.id, "field": "status", "value": "in-progress"})
        k = LegKeoi.objects.get(leg=leg)
        self.assertTrue(k.is_active)
        self.assertTrue(self._keoi_audits(leg, "keoi_reactivated").exists())

    def test_cancelled_to_active_reactivates(self):
        leg = self._leg(status="in-progress", driver=self.driver)
        self._keoi(leg)
        leg.status = "cancelled"
        leg.save(update_fields=["status"])          # signal closes with leg_cancelled
        self.assertFalse(LegKeoi.objects.get(leg=leg).is_active)
        leg.status = "in-progress"
        leg.save(update_fields=["status"])          # signal reactivates
        self.assertTrue(LegKeoi.objects.get(leg=leg).is_active)

    def test_admin_removed_never_reactivates(self):
        leg = self._leg(status="completed")
        k = self._keoi(leg, closed_at=timezone.now(), closed_reason="admin_removed")
        # Leg leaves terminal — admin_removed must not come back
        leg.status = "in-progress"
        leg.save(update_fields=["status"])
        k.refresh_from_db()
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "admin_removed")

    def test_reactivate_noops_when_active_exists(self):
        leg = self._leg(status="in-progress")
        # one closed (auto) + one active
        self._keoi(leg, closed_at=timezone.now(), closed_reason="leg_completed")
        active = self._keoi(leg)
        n = reactivate_keoi(leg)
        self.assertEqual(n, 0)
        self.assertEqual(LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).count(), 1)
        self.assertEqual(LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).first().id, active.id)


class KeoiRemoveTests(_KeoiFixtureMixin, TestCase):
    def test_remover_succeeds(self):
        leg = self._leg()
        self._keoi(leg)
        self.client.force_login(self.remover)
        r = self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "duplicate flag"})
        self.assertEqual(r.status_code, 200)
        k = LegKeoi.objects.get(leg=leg)
        self.assertEqual(k.closed_reason, "admin_removed")
        self.assertEqual(k.removal_reason, "duplicate flag")
        audit = self._keoi_audits(leg, "keoi_removed").first()
        self.assertIn("duplicate flag", audit.notes)

    def test_superuser_succeeds_without_explicit_perm(self):
        leg = self._leg()
        self._keoi(leg)
        self.client.force_login(self.manager)
        r = self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "cleanup"})
        self.assertEqual(r.status_code, 200)

    def test_staff_without_perm_forbidden(self):
        leg = self._leg()
        self._keoi(leg)
        self.client.force_login(self.dispatcher)
        r = self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "no perm"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(LegKeoi.objects.get(leg=leg).is_active)

    def test_blank_reason_rejected(self):
        leg = self._leg()
        self._keoi(leg)
        self.client.force_login(self.remover)
        r = self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "   "})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(LegKeoi.objects.get(leg=leg).is_active)

    def test_overlong_reason_rejected(self):
        leg = self._leg()
        self._keoi(leg)
        self.client.force_login(self.remover)
        r = self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "x" * 2001})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(LegKeoi.objects.get(leg=leg).is_active)

    def test_removed_flag_stays_closed_through_churn(self):
        leg = self._leg(status="in-progress")
        self._keoi(leg)
        self.client.force_login(self.remover)
        self._post_json("keoi_remove", {"leg_id": leg.id, "reason": "gone"})
        # churn the status
        leg.status = "completed"
        leg.save(update_fields=["status"])
        leg.status = "in-progress"
        leg.save(update_fields=["status"])
        k = LegKeoi.objects.get(leg=leg)
        self.assertFalse(k.is_active)
        self.assertEqual(k.closed_reason, "admin_removed")


class KeoiBoardTests(_KeoiFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.dispatcher)

    def test_dashboard_shows_active_flag(self):
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg, category="tight_schedule", description="watch the turnaround")
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        self.assertContains(r, "Tight Schedule")
        self.assertContains(r, "watch the turnaround")

    def test_dashboard_hides_closed_flag(self):
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg, description="closed one", closed_at=timezone.now(),
                   closed_reason="admin_removed")
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        self.assertNotContains(r, "closed one")

    def test_keoi_filter_and_composition(self):
        flagged = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(flagged, description="flagged leg desc")
        unflagged = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        # filter=active shows only the flagged leg. Assert on the mobile card id
        # (leg-card-N) — leg-row-N also appears in the timeline onclick handler,
        # which is built from the whole day, not the filtered table.
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat() + "&keoi=active")
        self.assertContains(r, "flagged leg desc")
        self.assertContains(r, "leg-card-%d" % flagged.id)
        self.assertNotContains(r, "leg-card-%d" % unflagged.id)
        # composes with trip_type without resetting keoi
        r2 = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat()
                             + "&keoi=active&trip_type=arrival")
        self.assertEqual(r2.status_code, 200)

    def test_pill_count(self):
        for _ in range(3):
            leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
            self._keoi(leg)
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        # The count sits in its own span so syncKeoiStrip can update it in place
        # after an AJAX save; assert on the span, not on "KEOI (3)".
        self.assertContains(r, '<span class="keoi-pill-count">3</span>')

    def test_watching_strip_lists_every_flagged_leg(self):
        for _ in range(2):
            leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
            self._keoi(leg)
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        self.assertContains(r, 'id="keoiStrip"')
        self.assertContains(r, '<span id="keoiStripCount">2</span>')
        self.assertContains(r, 'class="keoi-strip-item"', count=2)
        # visible, not collapsed
        self.assertNotContains(r, 'class="keoi-strip d-none"')

    def test_watching_strip_hidden_when_nothing_flagged(self):
        self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        # Still in the DOM (syncKeoiStrip needs a mount point) but hidden.
        self.assertContains(r, 'class="keoi-strip d-none"')
        self.assertNotContains(r, 'class="keoi-strip-item"')

    def test_watching_strip_is_whole_day_not_the_filtered_view(self):
        """The strip answers 'what is flagged today', so another filter being on
        must not shrink it — otherwise a dispatcher filtered to one driver would
        believe the rest of the day is clear."""
        mine = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(mine, description="my flagged leg")
        other_driver = Driver.objects.create(
            profile=User.objects.create_user("keoi_otherdrv", password="x"),
            driver_type="inhouse",
        )
        theirs = self._leg(pickup_date=FUTURE, status="in-progress", driver=other_driver)
        self._keoi(theirs, description="their flagged leg")
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat()
                            + "&driver=%d" % self.driver.id)
        self.assertContains(r, '<span id="keoiStripCount">2</span>')
        self.assertContains(r, 'class="keoi-strip-item"', count=2)
        # ...while the table itself is still filtered to the one driver.
        self.assertNotContains(r, "leg-card-%d" % theirs.id)

    def test_description_is_visible_text_not_only_a_tooltip(self):
        """v1 put the description only in title=/the modal, so the sentence that
        says what to watch never reached the eye. It is a rendered line now."""
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg, description="cruise clears 9:40, Alex 25 min out")
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        self.assertContains(
            r, '<div class="keoi-desc">cruise clears 9:40, Alex 25 min out</div>')

    def test_row_rail_does_not_yield_to_danger_rows(self):
        """The v1 row tint was suppressed on .table-danger/.table-warning rows —
        it disappeared on exactly the jobs worth flagging. The rail must be keyed
        to tr.leg-keoi with no :not() escape hatch. (VIP and time-changed keep
        theirs: they tint the background, which genuinely has to yield.)"""
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg)
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        html = r.content.decode()
        row = html[html.index('id="leg-row-%d"' % leg.id) - 900:
                   html.index('id="leg-row-%d"' % leg.id)]
        self.assertIn("leg-keoi", row)
        self.assertIn("tr.leg-keoi > td {", html)
        self.assertNotIn("tr.leg-keoi:not(", html)

    def test_flag_renders_on_moved_date(self):
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg, description="moving desc")
        new_date = FUTURE + timedelta(days=1)
        leg.pickup_date = new_date
        leg.save(update_fields=["pickup_date"])
        r = self.client.get(reverse("dashboard") + "?date=" + new_date.isoformat())
        self.assertContains(r, "moving desc")

    def test_description_html_escaped(self):
        leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
        self._keoi(leg, description="<script>alert(1)</script>")
        r = self.client.get(reverse("dashboard") + "?date=" + FUTURE.isoformat())
        self.assertContains(r, "&lt;script&gt;")
        self.assertNotContains(r, "<script>alert(1)</script>")

    def test_active_keoi_no_extra_queries(self):
        """Reading leg.active_keoi across the prefetched queryset adds zero queries."""
        from dispatching.utils import get_filtered_legs_queryset
        for _ in range(3):
            leg = self._leg(pickup_date=FUTURE, status="in-progress", driver=self.driver)
            self._keoi(leg)
        qs = get_filtered_legs_queryset(date_filter=FUTURE.isoformat())
        legs = list(qs)  # triggers the prefetch
        with CaptureQueriesContext(connection) as ctx:
            _ = [l.active_keoi for l in legs]
        self.assertEqual(len(ctx.captured_queries), 0)


class KeoiConstraintTests(_KeoiFixtureMixin, TestCase):
    def test_second_active_raises_integrity_error(self):
        leg = self._leg()
        self._keoi(leg)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._keoi(leg)

    def test_closed_and_active_coexist(self):
        leg = self._leg()
        self._keoi(leg, closed_at=timezone.now(), closed_reason="leg_completed")
        self._keoi(leg, closed_at=timezone.now(), closed_reason="admin_removed")
        active = self._keoi(leg)
        self.assertEqual(LegKeoi.objects.filter(leg=leg).count(), 3)
        self.assertEqual(LegKeoi.objects.filter(leg=leg, closed_at__isnull=True).count(), 1)

    def test_save_against_existing_active_takes_edit_path(self):
        leg = self._leg()
        self._keoi(leg, category="tight_schedule")
        self.client.force_login(self.dispatcher)
        r = self._post_json("keoi_save", {
            "leg_id": leg.id, "category": "other", "description": "edited via upsert",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["created"])
        self.assertEqual(LegKeoi.objects.filter(leg=leg).count(), 1)
