"""Tests for the driver portal: board-state auto-refresh fingerprint,
assigned-vehicle display, and the removal of phone-GPS location capture.

Run with:  ./manage.py test drivers.tests_driver_portal
"""
import json
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from drivers.models import (
    Driver, DriverPushSubscription, DriverVehicleAssignment, FleetVehicle,
)
from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg, DriverLocation


def _make_driver(username, first="First", last="Last", driver_type="inhouse"):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    return Driver.objects.create(profile=user, driver_type=driver_type)


def _bootstrap_reservation():
    vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
    origin = Location.objects.create(name="MCO")
    dest = Location.objects.create(name="Disney")
    route = Route.objects.create(origin=origin, destination=dest)
    rate = Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name="Cust", last_name="One", email="c@example.com", phone_number="555",
    )
    reservation = Reservation.objects.create(
        trip_type="one-way", customer=customer, rate=rate, vehicle=vehicle,
        base_price=Decimal("100"), total_price=Decimal("100"),
    )
    return reservation, vehicle


def _make_leg(reservation, driver, *, pickup_date, status="confirmed"):
    return Leg.objects.create(
        reservation=reservation, driver=driver,
        pickup_date=pickup_date, pickup_time=time(9, 0),
        pickup_location="MCO", dropoff_location="Disney",
        status=status,
    )


@override_settings(GOOGLE_MAPS_API_KEY="")
class BoardStateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("poll_driver")
        cls.reservation, _ = _bootstrap_reservation()
        cls.today = timezone.localdate()
        cls.leg = _make_leg(cls.reservation, cls.driver, pickup_date=cls.today)

    def _get_fp(self):
        url = reverse("driver_board_state")
        resp = self.client.get(url, {"start": self.today.isoformat(), "end": self.today.isoformat()})
        self.assertEqual(resp.status_code, 200)
        return resp.json()["fp"]

    def test_requires_login(self):
        resp = self.client.get(reverse("driver_board_state"))
        self.assertEqual(resp.status_code, 302)

    def test_fingerprint_stable_when_nothing_changes(self):
        self.client.force_login(self.driver.profile)
        self.assertEqual(self._get_fp(), self._get_fp())

    def test_fingerprint_changes_on_status_change(self):
        self.client.force_login(self.driver.profile)
        before = self._get_fp()
        self.leg.status = "on-the-way"
        self.leg.save(update_fields=["status"])
        self.assertNotEqual(before, self._get_fp())

    def test_fingerprint_changes_on_retime(self):
        self.client.force_login(self.driver.profile)
        before = self._get_fp()
        self.leg.pickup_time = time(10, 30)
        self.leg.save()
        self.assertNotEqual(before, self._get_fp())

    def test_fingerprint_changes_when_leg_reassigned_away(self):
        self.client.force_login(self.driver.profile)
        before = self._get_fp()
        other = _make_driver("other_driver")
        self.leg.driver = other
        self.leg.save()
        self.assertNotEqual(before, self._get_fp())

    def test_bad_dates_fall_back_to_today(self):
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("driver_board_state"), {"start": "garbage"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fp", resp.json())


@override_settings(GOOGLE_MAPS_API_KEY="")
class AssignedVehicleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("veh_driver")
        cls.reservation, cls.vehicle_type = _bootstrap_reservation()
        cls.today = timezone.localdate()
        _make_leg(cls.reservation, cls.driver, pickup_date=cls.today)
        cls.unit = FleetVehicle.objects.create(
            vehicle_number="009", vehicle_type=cls.vehicle_type,
            year=2022, make="Chevy", model="Suburban",
        )

    def test_inhouse_driver_sees_assigned_vehicle(self):
        DriverVehicleAssignment.objects.create(
            driver=self.driver, date=self.today, vehicle=self.unit,
        )
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Your Vehicle")
        self.assertContains(resp, "#009")
        # Year/make/model intentionally not shown — drivers know units by number
        self.assertNotContains(resp, "Suburban")

    def test_shared_car_window_rendered(self):
        DriverVehicleAssignment.objects.create(
            driver=self.driver, date=self.today, vehicle=self.unit,
            planned_start_hour=4, planned_end_hour=15,
        )
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Shared car")
        self.assertContains(resp, "4 AM – 3 PM")

    def test_no_assignment_renders_no_vehicle_card(self):
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertNotContains(resp, "Your Vehicle")

    def test_affiliate_driver_never_sees_vehicle_card(self):
        affiliate = _make_driver("aff_driver", driver_type="affiliate")
        DriverVehicleAssignment.objects.create(
            driver=affiliate, date=self.today, vehicle=self.unit,
        )
        self.client.force_login(affiliate.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertNotContains(resp, "Your Vehicle")

    def test_weekly_schedule_shows_vehicle_on_job_card(self):
        DriverVehicleAssignment.objects.create(
            driver=self.driver, date=self.today, vehicle=self.unit,
            planned_start_hour=4, planned_end_hour=15,
        )
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("schedule"))
        self.assertContains(resp, "#009")
        self.assertContains(resp, "shared 4 AM – 3 PM")

    def test_weekly_schedule_without_assignment_shows_no_vehicle(self):
        self.client.force_login(self.driver.profile)
        resp = self.client.get(reverse("schedule"))
        self.assertNotContains(resp, "#009")


@override_settings(GOOGLE_MAPS_API_KEY="")
class SharedCarPartnerTests(TestCase):
    """Two drivers on one unit the same day: each side of the handoff sees the
    other — the AM driver learns who takes the car after them (and their first
    pickup), the PM driver learns who has it first (and when it frees up)."""

    @classmethod
    def setUpTestData(cls):
        cls.am_driver = _make_driver("am_share_driver", first="Alex")
        cls.pm_driver = _make_driver("pm_share_driver", first="Sam")
        cls.reservation, cls.vehicle_type = _bootstrap_reservation()
        cls.today = timezone.localdate()
        cls.unit = FleetVehicle.objects.create(
            vehicle_number="007", vehicle_type=cls.vehicle_type,
            year=2023, make="Chevy", model="Suburban",
        )
        DriverVehicleAssignment.objects.create(
            driver=cls.am_driver, date=cls.today, vehicle=cls.unit,
            planned_start_hour=4, planned_end_hour=15,
        )
        DriverVehicleAssignment.objects.create(
            driver=cls.pm_driver, date=cls.today, vehicle=cls.unit,
            planned_start_hour=15, planned_end_hour=23,
        )
        _make_leg(cls.reservation, cls.am_driver, pickup_date=cls.today)  # 9:00 AM
        Leg.objects.create(
            reservation=cls.reservation, driver=cls.pm_driver,
            pickup_date=cls.today, pickup_time=time(16, 0),
            pickup_location="Disney", dropoff_location="MCO",
            status="confirmed",
        )

    def test_am_driver_sees_partner_taking_over(self):
        self.client.force_login(self.am_driver.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Sam")
        self.assertContains(resp, "takes the car after you")
        self.assertContains(resp, "their first pickup")
        self.assertContains(resp, "4:00 PM")
        # Their own split window still shows
        self.assertContains(resp, "Shared car")
        self.assertContains(resp, "4 AM – 3 PM")

    def test_pm_driver_sees_partner_holding_it_first(self):
        self.client.force_login(self.pm_driver.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Alex")
        self.assertContains(resp, "has the car before you")
        self.assertContains(resp, "3 PM – 11 PM")

    def test_weekly_schedule_names_partner_both_ways(self):
        self.client.force_login(self.am_driver.profile)
        resp = self.client.get(reverse("schedule"))
        self.assertContains(resp, "then Sam")
        self.client.force_login(self.pm_driver.profile)
        resp = self.client.get(reverse("schedule"))
        self.assertContains(resp, "Alex before you")

    def test_double_assignment_without_windows_still_names_partner(self):
        other_unit = FleetVehicle.objects.create(
            vehicle_number="011", vehicle_type=self.vehicle_type,
            year=2021, make="Ford", model="Transit",
        )
        d1 = _make_driver("nw_share1", first="Kim")
        d2 = _make_driver("nw_share2", first="Lee")
        DriverVehicleAssignment.objects.create(driver=d1, date=self.today, vehicle=other_unit)
        DriverVehicleAssignment.objects.create(driver=d2, date=self.today, vehicle=other_unit)
        self.client.force_login(d1.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Lee")
        self.assertContains(resp, "also drives this car")

    def test_no_windows_infers_handoff_order_from_pickups(self):
        """Without planned windows, actual first pickups decide who is
        'before' and who is 'after' — so the later driver still learns when
        the car should free up."""
        unit = FleetVehicle.objects.create(
            vehicle_number="013", vehicle_type=self.vehicle_type,
            year=2022, make="GMC", model="Yukon",
        )
        early = _make_driver("infer_early", first="Omar")
        late = _make_driver("infer_late", first="Rita")
        DriverVehicleAssignment.objects.create(driver=early, date=self.today, vehicle=unit)
        DriverVehicleAssignment.objects.create(driver=late, date=self.today, vehicle=unit)
        _make_leg(self.reservation, early, pickup_date=self.today)  # 9:00 AM
        Leg.objects.create(
            reservation=self.reservation, driver=late,
            pickup_date=self.today, pickup_time=time(18, 0),
            pickup_location="Disney", dropoff_location="MCO",
            status="confirmed",
        )
        self.client.force_login(early.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Rita")
        self.assertContains(resp, "takes the car after you")
        self.assertContains(resp, "6:00 PM")
        self.client.force_login(late.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "Omar")
        self.assertContains(resp, "has the car before you")

    def test_solo_assignment_shows_no_partner_line(self):
        solo_unit = FleetVehicle.objects.create(
            vehicle_number="012", vehicle_type=self.vehicle_type,
            year=2020, make="Lincoln", model="Navigator",
        )
        solo = _make_driver("solo_share_driver", first="Jo")
        DriverVehicleAssignment.objects.create(driver=solo, date=self.today, vehicle=solo_unit)
        self.client.force_login(solo.profile)
        resp = self.client.get(reverse("drivers_dashboard"))
        self.assertContains(resp, "#012")
        self.assertNotContains(resp, "takes the car after you")
        self.assertNotContains(resp, "has the car before you")
        self.assertNotContains(resp, "also drives this car")


@override_settings(GOOGLE_MAPS_API_KEY="")
class LocationRemovalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("gps_driver")
        cls.reservation, _ = _bootstrap_reservation()
        cls.leg = _make_leg(
            cls.reservation, cls.driver, pickup_date=timezone.localdate(),
        )

    def test_location_routes_are_gone(self):
        with self.assertRaises(NoReverseMatch):
            reverse("driver_report_location")
        with self.assertRaises(NoReverseMatch):
            reverse("get_driver_eta", args=[self.leg.id])

    def test_status_update_ignores_stale_gps_payload(self):
        """Old clients (cached JS) may still POST coords with the status —
        the update must succeed and no DriverLocation row may be written."""
        self.client.force_login(self.driver.profile)
        resp = self.client.post(
            reverse("update_leg_status", args=[self.leg.id]),
            data=json.dumps({
                "status": "on-the-way",
                "latitude": 28.5383, "longitude": -81.3792, "accuracy": 12,
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.status, "on-the-way")
        self.assertEqual(DriverLocation.objects.count(), 0)


@override_settings(GOOGLE_MAPS_API_KEY="")
class DriverChangeStatusResetTests(TestCase):
    """Founder rule (2026-06-11): any driver change — assign, reassign, or
    unassign — resets a non-terminal leg back to 'in-progress'."""

    @classmethod
    def setUpTestData(cls):
        cls.d1 = _make_driver("reset_d1")
        cls.d2 = _make_driver("reset_d2")
        cls.reservation, _ = _bootstrap_reservation()
        cls.tomorrow = timezone.localdate() + timedelta(days=1)

    def _leg(self, status):
        leg = _make_leg(self.reservation, self.d1,
                        pickup_date=self.tomorrow, status=status)
        return Leg.objects.get(pk=leg.pk)  # fresh instance, like the views use

    def test_reassign_resets_confirmed_to_in_progress(self):
        leg = self._leg("confirmed")
        leg.driver = self.d2
        leg.save()
        leg.refresh_from_db()
        self.assertEqual(leg.status, "in-progress")
        self.assertEqual(leg.driver, self.d2)

    def test_reassign_with_update_fields_persists_reset(self):
        """The auto-assign apply / swap paths save with update_fields —
        the reset must be widened in or it is silently dropped."""
        leg = self._leg("on-the-way")
        leg.driver = self.d2
        leg.save(update_fields=["driver", "driver_assigned_by", "driver_assigned_at"])
        leg.refresh_from_db()
        self.assertEqual(leg.status, "in-progress")

    def test_unassign_resets_status(self):
        leg = self._leg("on-location")
        leg.driver = None
        leg.save()
        leg.refresh_from_db()
        self.assertEqual(leg.status, "in-progress")
        self.assertIsNone(leg.driver)

    def test_completed_leg_keeps_status_on_reassign(self):
        """Payroll-correction reassignment of a finished trip must not
        resurrect it."""
        leg = self._leg("completed")
        leg.driver = self.d2
        leg.save()
        leg.refresh_from_db()
        self.assertEqual(leg.status, "completed")
        self.assertEqual(leg.driver, self.d2)


PUSH_SETTINGS = dict(
    GOOGLE_MAPS_API_KEY="",
    WEBPUSH_VAPID_PRIVATE_KEY="test-private",
    WEBPUSH_VAPID_PUBLIC_KEY="test-public",
    WEBPUSH_AUTO_NOTICES=True,
)

FAKE_SUB = {
    "endpoint": "https://push.example.com/sub/abc123",
    "keys": {"p256dh": "p256dh-key", "auth": "auth-key"},
}


@override_settings(**PUSH_SETTINGS)
class PushSubscriptionEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("push_driver")

    def _subscribe(self):
        return self.client.post(
            reverse("driver_push_subscribe"),
            data=json.dumps({"subscription": FAKE_SUB}),
            content_type="application/json",
        )

    def test_subscribe_stores_subscription(self):
        self.client.force_login(self.driver.profile)
        resp = self._subscribe()
        self.assertEqual(resp.status_code, 200)
        sub = DriverPushSubscription.objects.get()
        self.assertEqual(sub.driver, self.driver)
        self.assertEqual(sub.endpoint, FAKE_SUB["endpoint"])

    def test_subscribe_is_idempotent_per_endpoint(self):
        self.client.force_login(self.driver.profile)
        self._subscribe()
        self._subscribe()
        self.assertEqual(DriverPushSubscription.objects.count(), 1)

    def test_subscribe_rejects_bad_payload(self):
        self.client.force_login(self.driver.profile)
        resp = self.client.post(
            reverse("driver_push_subscribe"),
            data=json.dumps({"subscription": {"endpoint": ""}}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY="", WEBPUSH_VAPID_PUBLIC_KEY="")
    def test_subscribe_400_when_not_configured(self):
        self.client.force_login(self.driver.profile)
        self.assertEqual(self._subscribe().status_code, 400)

    def test_unsubscribe_removes_subscription(self):
        self.client.force_login(self.driver.profile)
        self._subscribe()
        resp = self.client.post(
            reverse("driver_push_unsubscribe"),
            data=json.dumps({"endpoint": FAKE_SUB["endpoint"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(DriverPushSubscription.objects.count(), 0)

    def test_push_test_requires_subscription(self):
        self.client.force_login(self.driver.profile)
        resp = self.client.post(reverse("driver_push_test"))
        self.assertEqual(resp.status_code, 400)


@override_settings(**PUSH_SETTINGS)
class PushSendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("send_driver")
        cls.sub = DriverPushSubscription.objects.create(
            driver=cls.driver,
            endpoint=FAKE_SUB["endpoint"],
            p256dh="p256dh-key", auth="auth-key",
        )

    def test_send_delivers_and_stamps_success(self):
        from drivers.push import send_push_to_driver
        with mock.patch("pywebpush.webpush") as wp:
            sent = send_push_to_driver(self.driver.id, "Title", "Body")
        self.assertEqual(sent, 1)
        self.assertEqual(wp.call_count, 1)
        payload = json.loads(wp.call_args.kwargs["data"])
        self.assertEqual(payload["title"], "Title")
        self.sub.refresh_from_db()
        self.assertIsNotNone(self.sub.last_success_at)

    def test_dead_subscription_is_pruned(self):
        from pywebpush import WebPushException
        from drivers.push import send_push_to_driver
        exc = WebPushException("gone", response=mock.Mock(status_code=410))
        with mock.patch("pywebpush.webpush", side_effect=exc):
            sent = send_push_to_driver(self.driver.id, "Title", "Body")
        self.assertEqual(sent, 0)
        self.assertEqual(DriverPushSubscription.objects.count(), 0)

    def test_compose_single_and_multi(self):
        from drivers.push import compose_notice
        single = [{
            "kind": "new", "date_iso": "2026-06-12",
            "date_label": "Fri, Jun 12", "time_label": "9:15 AM",
            "pickup": "MCO",
        }]
        title, body, url = compose_notice(single)
        self.assertEqual(title, "New trip assigned")
        self.assertIn("9:15 AM", body)
        self.assertIn("date=2026-06-12", url)

        multi = single + [{
            "kind": "retimed", "date_iso": "2026-06-13",
            "date_label": "Sat, Jun 13", "time_label": "2:00 PM",
            "pickup": "Disney",
        }]
        title, body, url = compose_notice(multi)
        self.assertEqual(title, "Schedule updated")
        self.assertIn("2 changes", body)
        self.assertIn("date=2026-06-12", url)


@override_settings(**PUSH_SETTINGS)
class PushSignalTests(TestCase):
    """The Leg post_save hook queues the right notices — and never for a
    driver's own status taps."""

    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("sig_driver")
        cls.other = _make_driver("sig_other")
        cls.reservation, _ = _bootstrap_reservation()
        # Both drivers need a subscription or queue_schedule_notice no-ops
        for i, d in enumerate([cls.driver, cls.other]):
            DriverPushSubscription.objects.create(
                driver=d, endpoint=f"https://push.example.com/sub/{i}",
                p256dh="k", auth="a",
            )
        cls.tomorrow = timezone.localdate() + timedelta(days=1)

    def _leg(self, **kw):
        # Suppress the 'new trip' notice fired by creation itself — these
        # fixtures exist to test the LATER change, not the insert.
        with mock.patch("drivers.push.queue_schedule_notice"):
            return _make_leg(self.reservation, self.driver,
                             pickup_date=kw.pop("pickup_date", self.tomorrow), **kw)

    def test_new_leg_queues_new_notice(self):
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg = _make_leg(self.reservation, self.driver, pickup_date=self.tomorrow)
        q.assert_called_once_with(self.driver.id, "new", leg)

    def test_reassignment_notifies_both_drivers(self):
        leg = self._leg()
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg.driver = self.other
            leg.save()
        kinds = {(c.args[0], c.args[1]) for c in q.call_args_list}
        self.assertIn((self.driver.id, "removed"), kinds)
        self.assertIn((self.other.id, "new"), kinds)

    def test_retime_queues_retimed(self):
        leg = self._leg()
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg.pickup_time = time(15, 30)
            leg.save()
        q.assert_called_once_with(self.driver.id, "retimed", leg)

    def test_cancellation_queues_cancelled(self):
        leg = self._leg()
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg.status = "cancelled"
            leg.save()
        q.assert_called_once_with(self.driver.id, "cancelled", leg)

    def test_drivers_own_status_tap_is_silent(self):
        leg = self._leg()
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg.status = "on-the-way"
            leg.save(update_fields=["status"])
        q.assert_not_called()

    def test_past_trip_changes_are_silent(self):
        leg = self._leg(pickup_date=timezone.localdate() - timedelta(days=2))
        with mock.patch("drivers.push.queue_schedule_notice") as q:
            leg.pickup_time = time(16, 0)
            leg.save()
        q.assert_not_called()


@override_settings(**{**PUSH_SETTINGS, "WEBPUSH_AUTO_NOTICES": False})
class PushPausedTests(TestCase):
    """WEBPUSH_AUTO_NOTICES=False (the default): subscriptions and the test
    button keep working, but schedule changes queue NOTHING."""

    @classmethod
    def setUpTestData(cls):
        cls.driver = _make_driver("paused_driver")
        DriverPushSubscription.objects.create(
            driver=cls.driver, endpoint="https://push.example.com/sub/paused",
            p256dh="k", auth="a",
        )
        cls.reservation, _ = _bootstrap_reservation()

    def test_schedule_changes_queue_nothing_while_paused(self):
        from drivers import push
        _make_leg(self.reservation, self.driver,
                  pickup_date=timezone.localdate() + timedelta(days=1))
        self.assertEqual(push._pending, {})

    def test_direct_sends_still_work_while_paused(self):
        from drivers.push import send_push_to_driver
        with mock.patch("pywebpush.webpush") as wp:
            sent = send_push_to_driver(self.driver.id, "Test", "Body")
        self.assertEqual(sent, 1)
        self.assertEqual(wp.call_count, 1)
