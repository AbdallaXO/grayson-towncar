"""Tests for the Phase 1 Samsara integration (read-only live vehicle visibility).

Covers:
  * SamsaraService — network status-dict shapes, pagination, never-raises
  * parse_gps_record — pure record -> field mapping
  * sync_vehicles (poller body) — inert without token, populates mapped cars only
  * FleetVehicle samsara_* helpers — enabled / freshness / age display
  * resolve_assigned_fleet_vehicle — driver+date -> physical car
  * Render contract — reservation page shows live line only for a mapped+fresh car

Run with:  ./manage.py test dispatching.tests_samsara
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg
from drivers.models import Driver, FleetVehicle, DriverVehicleAssignment
from dispatching.samsara_service import (
    SamsaraService, parse_gps_record, resolve_assigned_fleet_vehicle,
)
from dispatching.samsara_scheduler import sync_vehicles, sweep_eta
from dispatching import samsara_risk
from dispatching.samsara_risk import (
    choose_active_target, evaluate, evaluate_driver, build_panel_context,
)

TD = date(2026, 6, 1)


class FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@override_settings(SAMSARA_API_TOKEN="test-token")
class SamsaraServiceTests(TestCase):
    def test_success_single_page(self):
        svc = SamsaraService()
        page = {"data": [{"id": "v1"}, {"id": "v2"}], "pagination": {"hasNextPage": False}}
        with patch.object(svc.session, "get", return_value=FakeResp(200, page)) as m:
            result = svc.get_vehicle_stats()
        m.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["data"]), 2)

    def test_pagination_follows_cursor(self):
        svc = SamsaraService()
        page1 = {"data": [{"id": "v1"}], "pagination": {"hasNextPage": True, "endCursor": "c2"}}
        page2 = {"data": [{"id": "v2"}], "pagination": {"hasNextPage": False}}
        with patch.object(svc.session, "get", side_effect=[FakeResp(200, page1), FakeResp(200, page2)]) as m:
            result = svc.get_vehicle_stats()
        self.assertEqual(m.call_count, 2)
        # second call must carry the cursor
        self.assertEqual(m.call_args_list[1].kwargs["params"].get("after"), "c2")
        self.assertEqual([d["id"] for d in result["data"]], ["v1", "v2"])

    def test_rate_limited(self):
        svc = SamsaraService()
        with patch.object(svc.session, "get", return_value=FakeResp(429, {}, {"Retry-After": "42"})):
            result = svc.get_vehicle_stats()
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["retry_after"], 42)

    def test_not_found(self):
        svc = SamsaraService()
        with patch.object(svc.session, "get", return_value=FakeResp(404, {})):
            result = svc.list_vehicles()
        self.assertEqual(result["status"], "not_found")

    def test_auth_error(self):
        svc = SamsaraService()
        with patch.object(svc.session, "get", return_value=FakeResp(403, {})):
            result = svc.get_vehicle_stats()
        self.assertEqual(result["status"], "error")
        self.assertIn("Auth failed", result["error"])

    def test_network_error_never_raises(self):
        svc = SamsaraService()
        with patch.object(svc.session, "get", side_effect=requests.ConnectionError("boom")):
            result = svc.get_vehicle_stats()  # must not raise
        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["error"])


@override_settings(SAMSARA_API_TOKEN="")
class SamsaraServiceNotConfiguredTests(TestCase):
    def test_inert_without_token(self):
        # Force no token regardless of the dev .env.
        svc = SamsaraService()
        self.assertFalse(svc.is_configured())
        with patch.object(svc.session, "get") as m:
            result = svc.get_vehicle_stats()
        m.assert_not_called()
        self.assertEqual(result["status"], "error")


class ParseGpsRecordTests(TestCase):
    def _rec(self, **gps):
        return {"id": "x", "gps": gps}

    def test_driving_when_moving(self):
        out = parse_gps_record(self._rec(
            latitude=28.4, longitude=-81.3, time="2026-06-05T12:00:00Z",
            speedMilesPerHour=35.0, reverseGeo={"formattedLocation": "near MCO"}))
        self.assertEqual(out["samsara_movement_status"], "driving")
        self.assertEqual(out["samsara_last_location_label"], "near MCO")
        self.assertEqual(float(out["samsara_last_latitude"]), 28.4)
        self.assertIsNotNone(out["samsara_last_seen_at"])

    def test_idle_when_stopped(self):
        out = parse_gps_record(self._rec(latitude=28.4, longitude=-81.3, speedMilesPerHour=0.0))
        self.assertEqual(out["samsara_movement_status"], "idle")

    def test_no_speed_leaves_movement_blank(self):
        out = parse_gps_record(self._rec(latitude=28.4, longitude=-81.3))
        self.assertEqual(out["samsara_movement_status"], "")

    def test_missing_gps_returns_none(self):
        self.assertIsNone(parse_gps_record({"id": "x"}))

    def test_missing_coords_returns_none(self):
        self.assertIsNone(parse_gps_record(self._rec(speedMilesPerHour=10)))

    def test_label_truncated_to_128(self):
        out = parse_gps_record(self._rec(
            latitude=1, longitude=2, reverseGeo={"formattedLocation": "x" * 200}))
        self.assertEqual(len(out["samsara_last_location_label"]), 128)


@override_settings(SAMSARA_API_TOKEN="test-token")
class SyncVehiclesTests(TestCase):
    def _fleet(self, number, samsara_id=""):
        return FleetVehicle.objects.create(
            vehicle_number=number, year=2022, make="M", model="X", samsara_vehicle_id=samsara_id)

    def test_no_mapped_vehicles_is_noop(self):
        self._fleet("100")  # un-mapped
        out = sync_vehicles()
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "no_mapped_vehicles")

    def test_success_populates_only_mapped(self):
        mapped = self._fleet("101", samsara_id="veh-1")
        unmapped = self._fleet("102")
        stats = {"status": "success", "data": [{
            "id": "veh-1",
            "gps": {"latitude": 28.43, "longitude": -81.31, "time": "2026-06-05T12:00:00Z",
                    "speedMilesPerHour": 30.0, "reverseGeo": {"formattedLocation": "near MCO"}},
        }]}
        with patch.object(SamsaraService, "get_vehicle_stats", return_value=stats) as m:
            out = sync_vehicles()
        m.assert_called_once()
        # only the mapped id was requested
        self.assertEqual(m.call_args.kwargs["vehicle_ids"], ["veh-1"])
        self.assertEqual(out["updated"], 1)
        mapped.refresh_from_db()
        unmapped.refresh_from_db()
        self.assertEqual(mapped.samsara_last_location_label, "near MCO")
        self.assertEqual(mapped.samsara_movement_status, "driving")
        self.assertIsNotNone(mapped.samsara_last_seen_at)
        self.assertIsNotNone(mapped.samsara_last_synced_at)
        # untouched
        self.assertEqual(unmapped.samsara_last_location_label, "")
        self.assertIsNone(unmapped.samsara_last_seen_at)

    def test_rate_limited_does_not_crash_or_write(self):
        mapped = self._fleet("103", samsara_id="veh-9")
        with patch.object(SamsaraService, "get_vehicle_stats",
                          return_value={"status": "rate_limited", "retry_after": 60}):
            out = sync_vehicles()
        self.assertEqual(out["updated"], 0)
        mapped.refresh_from_db()
        self.assertIsNone(mapped.samsara_last_seen_at)


@override_settings(SAMSARA_API_TOKEN="")
class SyncVehiclesNoTokenTests(TestCase):
    def test_skipped_without_token(self):
        FleetVehicle.objects.create(
            vehicle_number="200", year=2022, make="M", model="X", samsara_vehicle_id="veh-1")
        out = sync_vehicles()
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["reason"], "no_token")


class FleetVehicleHelpersTests(TestCase):
    def test_enabled(self):
        v = FleetVehicle(vehicle_number="A", year=2022, make="M", model="X")
        self.assertFalse(v.samsara_enabled)
        v.samsara_vehicle_id = "veh-1"
        self.assertTrue(v.samsara_enabled)

    def test_freshness(self):
        v = FleetVehicle(vehicle_number="B", year=2022, make="M", model="X", samsara_vehicle_id="veh-1")
        self.assertFalse(v.samsara_is_fresh)  # no timestamp
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=5)
        self.assertTrue(v.samsara_is_fresh)
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=30)
        self.assertFalse(v.samsara_is_fresh)

    def test_age_display(self):
        v = FleetVehicle(vehicle_number="C", year=2022, make="M", model="X")
        self.assertEqual(v.samsara_age_display(), "")
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=3)
        self.assertEqual(v.samsara_age_display(), "3m ago")
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=63)
        self.assertEqual(v.samsara_age_display(), "1h 3m ago")


class ResolveAssignedFleetVehicleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        u = User.objects.create_user(username="raf_drv")
        cls.driver = Driver.objects.create(profile=u, driver_type="inhouse")
        cls.fv = FleetVehicle.objects.create(
            vehicle_number="300", year=2022, make="M", model="X", samsara_vehicle_id="veh-1")
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=TD, vehicle=cls.fv)

    def test_returns_assigned_vehicle(self):
        leg = SimpleNamespace(driver_id=self.driver.id, pickup_date=TD)
        self.assertEqual(resolve_assigned_fleet_vehicle(leg), self.fv)

    def test_none_without_driver(self):
        leg = SimpleNamespace(driver_id=None, pickup_date=TD)
        self.assertIsNone(resolve_assigned_fleet_vehicle(leg))

    def test_none_when_no_assignment_that_date(self):
        leg = SimpleNamespace(driver_id=self.driver.id, pickup_date=date(2026, 6, 2))
        self.assertIsNone(resolve_assigned_fleet_vehicle(leg))


class ReservationRenderContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=4)
        o = Location.objects.create(name="MCO")
        d = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(origin=o, destination=d, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.suv, route=cls.route, oneway_price=Decimal("100.00"),
            round_trip_price=Decimal("180.00"))
        cls.cust = Customer.objects.create(
            first_name="A", last_name="B", email="rc@example.com", phone_number="5550002222")
        u = User.objects.create_user(username="rc_drv", first_name="Drv")
        cls.driver = Driver.objects.create(profile=u, driver_type="inhouse")
        cls.staff = User.objects.create_user(username="rc_staff", password="x", is_staff=True)

    def setUp(self):
        # Reservation creation fires a post_save signal that emails in a daemon
        # thread; under SQLite-in-memory tests that races into "database table is
        # locked" + 1s retry sleeps. No-op it for clean, fast render tests.
        p = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _make(self, samsara_id="", last_seen=None, label=""):
        fv = FleetVehicle.objects.create(
            vehicle_number=f"V{samsara_id or 'none'}", year=2022, make="M", model="X",
            samsara_vehicle_id=samsara_id, samsara_last_location_label=label,
            samsara_last_seen_at=last_seen, samsara_movement_status="driving" if last_seen else "")
        DriverVehicleAssignment.objects.create(driver=self.driver, date=TD, vehicle=fv)
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.cust, vehicle=self.suv, rate=self.rate,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        Leg.objects.create(
            reservation=res, driver=self.driver, pickup_date=TD, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route, status="confirmed")
        return res

    def _get(self, res):
        self.client.force_login(self.staff)
        return self.client.get(reverse("reservation_details", args=[str(res.uuid)]))

    def test_unmapped_renders_no_live_line(self):
        res = self._make(samsara_id="")  # not onboarded
        body = self._get(res).content.decode()
        self.assertNotIn("Live:", body)
        self.assertNotIn("Live position stale", body)

    def test_fresh_renders_label(self):
        res = self._make(samsara_id="veh-1", last_seen=timezone.now() - timedelta(minutes=2),
                         label="near MCO Terminal A")
        body = self._get(res).content.decode()
        self.assertIn("near MCO Terminal A", body)
        self.assertIn("Live:", body)

    def test_stale_renders_warning(self):
        res = self._make(samsara_id="veh-2", last_seen=timezone.now() - timedelta(minutes=40),
                         label="somewhere old")
        body = self._get(res).content.decode()
        self.assertIn("Live position stale", body)
        self.assertNotIn("somewhere old", body)  # stale hides the label


# ====================== Phase 2: schedule-aware ETA + late-risk ======================

def _leg_ns(status, when, pickup="MCO", dropoff="Disney"):
    """`when` is the effective pickup datetime (aware)."""
    return SimpleNamespace(
        id=id(object()), status=status, pickup_time=when.time(),
        pickup_location=pickup, dropoff_location=dropoff,
        pickup_date=when.date(), controlling_flight=None,
    )


def _veh_ns(fresh=True, movement="driving", enabled=True, stationary_since=None):
    return SimpleNamespace(
        samsara_enabled=enabled, samsara_is_fresh=fresh,
        samsara_last_latitude=Decimal("28.44"), samsara_last_longitude=Decimal("-81.31"),
        samsara_movement_status=movement, samsara_stationary_since=stationary_since,
    )


class ChooseActiveTargetTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 6, 1, 12, 0))

    def test_confirmed_targets_upcoming_pickup(self):
        t = choose_active_target([_leg_ns("confirmed", self.now + timedelta(minutes=60))], self.now)
        self.assertEqual(t["kind"], "pickup")
        self.assertEqual(t["location"], "MCO")

    def test_picked_up_targets_dropoff(self):
        t = choose_active_target([_leg_ns("picked-up", self.now + timedelta(minutes=60))], self.now)
        self.assertEqual(t["kind"], "dropoff")
        self.assertEqual(t["location"], "Disney")

    def test_skips_finished_and_picks_upcoming(self):
        legs = [_leg_ns("completed", self.now - timedelta(hours=2)),
                _leg_ns("confirmed", self.now + timedelta(minutes=30), pickup="Hotel")]
        t = choose_active_target(legs, self.now)
        self.assertEqual(t["location"], "Hotel")

    def test_long_past_pickup_gives_no_badge(self):
        # The core fix: a pickup overdue by hours is stale -> no live badge.
        t = choose_active_target([_leg_ns("confirmed", self.now - timedelta(hours=3))], self.now)
        self.assertIsNone(t)

    def test_recent_overdue_still_flags(self):
        # Genuinely running late (20 min over) still surfaces.
        t = choose_active_target([_leg_ns("confirmed", self.now - timedelta(minutes=20))], self.now)
        self.assertIsNotNone(t)
        self.assertEqual(t["kind"], "pickup")

    def test_picks_next_upcoming_over_stale_past(self):
        legs = [_leg_ns("confirmed", self.now - timedelta(hours=4), pickup="OldStale"),
                _leg_ns("confirmed", self.now + timedelta(minutes=40), pickup="NextUp")]
        t = choose_active_target(legs, self.now)
        self.assertEqual(t["location"], "NextUp")

    def test_none_when_all_done(self):
        legs = [_leg_ns("completed", self.now), _leg_ns("cancelled", self.now)]
        self.assertIsNone(choose_active_target(legs, self.now))


class EvaluateTests(TestCase):
    def _target(self, kind="pickup", minutes_out=60, status="confirmed"):
        tt = timezone.now() + timedelta(minutes=minutes_out) if minutes_out is not None else None
        return {"leg": _leg_ns(status, timezone.now()), "kind": kind,
                "location": "MCO", "target_time": tt}

    def _eval(self, target, vehicle, drive_min=10):
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": drive_min * 60}):
            return evaluate(vehicle, target, now=timezone.now())

    def test_on_time(self):
        r = self._eval(self._target(minutes_out=60), _veh_ns(), drive_min=10)
        self.assertEqual(r["dispatch_risk_status"], "on_time")
        self.assertEqual(r["dispatch_eta_minutes"], 10)

    def test_watch_low_slack(self):
        r = self._eval(self._target(minutes_out=15), _veh_ns(), drive_min=10)  # slack 5
        self.assertEqual(r["dispatch_risk_status"], "watch")

    def test_watch_idle_near_pickup(self):
        r = self._eval(self._target(minutes_out=25), _veh_ns(movement="idle"), drive_min=5)  # slack 20 but idle+near
        self.assertEqual(r["dispatch_risk_status"], "watch")

    def test_at_risk(self):
        r = self._eval(self._target(minutes_out=20), _veh_ns(), drive_min=40)  # slack -20
        self.assertEqual(r["dispatch_risk_status"], "at_risk")

    def test_late(self):
        r = self._eval(self._target(minutes_out=-5), _veh_ns(), drive_min=10)
        self.assertEqual(r["dispatch_risk_status"], "late")

    def test_unknown_when_stale(self):
        r = self._eval(self._target(), _veh_ns(fresh=False))
        self.assertEqual(r["dispatch_risk_status"], "unknown")
        self.assertIsNone(r["dispatch_eta_minutes"])

    def test_stale_but_parked_position_still_usable(self):
        # Ignition-off gateways report sparsely; a parked car's old fix is still
        # where the car sits, so the ETA must compute instead of going unknown.
        v = _veh_ns(fresh=False, movement="idle")
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=40)
        r = self._eval(self._target(minutes_out=60), v, drive_min=10)
        self.assertEqual(r["dispatch_risk_status"], "on_time")
        self.assertEqual(r["dispatch_eta_minutes"], 10)

    def test_stale_parked_beyond_cap_is_unknown(self):
        v = _veh_ns(fresh=False, movement="idle")
        v.samsara_last_seen_at = timezone.now() - timedelta(hours=9)
        r = self._eval(self._target(), v)
        self.assertEqual(r["dispatch_risk_status"], "unknown")

    def test_stale_while_driving_is_unknown(self):
        # A moving car with a stale fix really is lost — could be anywhere.
        v = _veh_ns(fresh=False, movement="driving")
        v.samsara_last_seen_at = timezone.now() - timedelta(minutes=40)
        r = self._eval(self._target(), v)
        self.assertEqual(r["dispatch_risk_status"], "unknown")

    def test_dropoff_eta_only(self):
        r = self._eval(self._target(kind="dropoff", minutes_out=None, status="picked-up"), _veh_ns(), drive_min=12)
        self.assertEqual(r["dispatch_risk_status"], "")
        self.assertEqual(r["dispatch_eta_minutes"], 12)

    def test_none_when_not_onboarded(self):
        self.assertIsNone(self._eval(self._target(), _veh_ns(enabled=False)))


class SweepEtaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=4)
        o = Location.objects.create(name="MCO")
        d = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(origin=o, destination=d, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(vehicle=cls.suv, route=cls.route,
                                       oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.cust = Customer.objects.create(first_name="A", last_name="B",
                                           email="sw@example.com", phone_number="5550003333")
        u = User.objects.create_user(username="sw_drv")
        cls.driver = Driver.objects.create(profile=u, driver_type="inhouse")
        cls.now = timezone.make_aware(datetime(2026, 6, 1, 12, 0))  # fixed clock for determinism
        cls.today = cls.now.date()
        cls.fv = FleetVehicle.objects.create(
            vehicle_number="909", year=2022, make="M", model="X",
            samsara_vehicle_id="veh-909", samsara_last_seen_at=timezone.now(),
            samsara_last_latitude=Decimal("28.44"), samsara_last_longitude=Decimal("-81.31"),
            samsara_movement_status="driving")
        DriverVehicleAssignment.objects.create(driver=cls.driver, date=cls.today, vehicle=cls.fv)

    def setUp(self):
        p = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _leg(self, when):
        res = Reservation.objects.create(trip_type="one-way", customer=self.cust, vehicle=self.suv,
                                         rate=self.rate, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(reservation=res, driver=self.driver, pickup_date=when.date(),
                                  pickup_time=when.time(), pickup_location="MCO", dropoff_location="Disney",
                                  route=self.route, status="confirmed")

    def test_only_active_leg_flagged(self):
        early = self._leg(self.now + timedelta(minutes=30))
        late = self._leg(self.now + timedelta(minutes=180))
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 600}):
            sweep_eta(now=self.now)
        early.refresh_from_db()
        late.refresh_from_db()
        self.assertIsNotNone(early.dispatch_eta_evaluated_at)
        self.assertEqual(early.dispatch_eta_target, "pickup")
        self.assertIsNone(late.dispatch_eta_evaluated_at)
        self.assertEqual(late.dispatch_risk_status, "")

    def test_long_past_legs_get_no_badge(self):
        # The fix: stale past pickups must not be flagged.
        self._leg(self.now - timedelta(hours=3))
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 600}):
            out = sweep_eta(now=self.now)
        self.assertEqual(out["flagged"], 0)

    def test_unmapped_vehicle_no_flag(self):
        self.fv.samsara_vehicle_id = ""
        self.fv.save()
        leg = self._leg(self.now + timedelta(minutes=30))
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 600}):
            sweep_eta(now=self.now)
        leg.refresh_from_db()
        self.assertIsNone(leg.dispatch_eta_evaluated_at)
        self.assertEqual(leg.dispatch_risk_status, "")

    def test_parked_car_second_sweep_skips_google(self):
        # End-to-end cost lever through the DB: first sweep computes + persists the
        # anchor; the next sweep (vehicle still parked at the same GPS) reuses it with
        # no Google call but still re-stamps freshness.
        leg = self._leg(self.now + timedelta(minutes=40))
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": 1200}) as m:  # 20 min
            sweep_eta(now=self.now)
        self.assertEqual(m.call_count, 1)
        leg.refresh_from_db()
        self.assertEqual(leg.dispatch_eta_minutes, 20)
        self.assertIsNotNone(leg.dispatch_eta_origin_lat)
        self.assertEqual(leg.dispatch_eta_origin_target, "MCO")

        later = self.now + timedelta(minutes=3)  # next poll, car hasn't moved
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": 1200}) as m2:
            sweep_eta(now=later)
        m2.assert_not_called()
        leg.refresh_from_db()
        self.assertEqual(leg.dispatch_eta_minutes, 20)  # reused
        self.assertGreater(leg.dispatch_eta_evaluated_at, self.now)  # freshness advanced


class DispatchEtaPartialRenderTests(TestCase):
    """Render the shared badge partial directly against a leg-like context."""
    def _render(self, **attrs):
        defaults = dict(
            dispatch_eta_is_fresh=True, dispatch_eta_minutes=18, dispatch_eta_target="pickup",
            dispatch_eta_target_time=timezone.now() + timedelta(minutes=51),
            dispatch_risk_status="on_time", dispatch_risk_reason="33 min slack", live_eta_minutes=None)
        defaults.update(attrs)
        return render_to_string("dispatching/includes/_leg_dispatch_eta.html",
                                {"leg": SimpleNamespace(**defaults)})

    def test_on_time_renders_badge(self):
        html = self._render()
        self.assertIn("18 min to pickup", html)
        self.assertIn("on time", html)

    def test_at_risk_renders(self):
        html = self._render(dispatch_risk_status="at_risk", dispatch_risk_reason="ETA 40 vs 20")
        self.assertIn("at risk", html)

    def test_not_fresh_renders_nothing(self):
        self.assertNotIn("to pickup", self._render(dispatch_eta_is_fresh=False))

    def test_unknown_shown_without_driverapp_eta(self):
        html = self._render(dispatch_eta_minutes=None, dispatch_risk_status="unknown")
        self.assertIn("Live ETA unavailable", html)

    def test_unknown_suppressed_when_driverapp_eta(self):
        html = self._render(dispatch_eta_minutes=None, dispatch_risk_status="unknown", live_eta_minutes=12)
        self.assertNotIn("Live ETA unavailable", html)


# ================= Live-tracking PANEL component (4 states) =================

class PanelStateTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 6, 1, 12, 0))

    def _leg(self, **kw):
        d = dict(status="confirmed", driver_id=1, dispatch_eta_is_fresh=True,
                 dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=60),
                 dispatch_is_moving=True, dispatch_stationary_minutes=0,
                 pickup_date=self.now.date(), pickup_time=self.now.time())
        d.update(kw)
        return SimpleNamespace(**d)

    def test_unassigned_renders_nothing(self):
        self.assertIsNone(build_panel_context(self._leg(driver_id=None), self.now))

    def test_none_when_completed(self):
        self.assertIsNone(build_panel_context(self._leg(status="completed"), self.now))

    def test_none_when_assigned_but_no_fresh_feed(self):
        self.assertIsNone(build_panel_context(self._leg(dispatch_eta_is_fresh=False), self.now))

    def test_upcoming_infeasible_warns_even_if_not_on_the_way(self):
        # Pickup in 2 min, he's 38 min out, NOT on the way -> still warn (he can't make it).
        p = build_panel_context(
            self._leg(status="confirmed", pickup_location="Hard Rock Hotel",
                      dispatch_eta_minutes=38, dispatch_eta_target_time=self.now + timedelta(minutes=2)),
            self.now)
        self.assertEqual(p["state"], "at_risk")
        self.assertEqual(p["headline"], "~36 min late projected")
        self.assertIn("ETA 38 min", p["evidence"])
        self.assertIn("pickup in 2 min", p["evidence"])

    def test_zero_slack_warns(self):
        # Pickup in 30, he's 30 away (0 slack) -> tight warning, on-the-way or not.
        p = build_panel_context(
            self._leg(status="confirmed", dispatch_eta_minutes=30,
                      dispatch_eta_target_time=self.now + timedelta(minutes=30)),
            self.now)
        self.assertEqual(p["state"], "tight")

    def test_overdue_not_on_the_way_stays_quiet(self):
        # Pickup already passed and he hasn't started (stale status) -> no alarm.
        p = build_panel_context(
            self._leg(status="confirmed", pickup_location="Hard Rock Hotel",
                      dispatch_eta_minutes=38, dispatch_eta_target_time=self.now - timedelta(minutes=3)),
            self.now)
        self.assertIsNone(p)

    def test_overdue_on_the_way_shows_late(self):
        p = build_panel_context(
            self._leg(status="on-the-way", pickup_location="Hard Rock Hotel",
                      dispatch_eta_minutes=18, dispatch_eta_target_time=self.now - timedelta(minutes=5)),
            self.now)
        self.assertEqual(p["state"], "at_risk")
        self.assertIn("late", p["headline"])

    def test_arrival_flight_at_gate_keeps_warning(self):
        # Not on the way, but it's an airport arrival, the flight is already at the
        # gate (target time passed) and he's still well out -> amber warning.
        p = build_panel_context(
            self._leg(status="confirmed", pickup_location="Orlando International Airport",
                      dispatch_eta_minutes=18, dispatch_eta_target_time=self.now - timedelta(minutes=3)),
            self.now)
        self.assertEqual(p["state"], "tight")
        self.assertIn("Flight landed", p["headline"])

    def test_vehicle_in_context(self):
        p = build_panel_context(
            self._leg(status="on-the-way", dispatch_vehicle_label="007", dispatch_eta_minutes=38,
                      dispatch_eta_target_time=self.now + timedelta(minutes=2)),
            self.now)
        self.assertEqual(p["vehicle"], "007")

    def test_tight_buffer(self):
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=15)),
            self.now)
        self.assertEqual(p["state"], "tight")
        self.assertEqual(p["headline"], "5 min buffer")

    def test_tight_when_stalled_in_window(self):
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=40),
                      dispatch_is_moving=False, dispatch_stationary_minutes=15),
            self.now)
        self.assertEqual(p["state"], "tight")
        self.assertEqual(p["headline"], "Vehicle not moving")

    def test_on_track(self):
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=60),
                      dispatch_is_moving=True),
            self.now)
        self.assertEqual(p["state"], "on_track")
        self.assertEqual(p["headline"], "Arrives ~50 min early")

    def test_far_future_pickup_shows_waiting_card(self):
        # Founder case: 8:30 AM pickup viewed at ~4:20 AM read "Arrives ~231 min
        # early" — noise, the driver isn't en route. Waiting card instead.
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=18,
                      dispatch_eta_target_time=self.now + timedelta(minutes=249)),
            self.now)
        self.assertEqual(p["state"], "on_track")
        self.assertIs(p["waiting"], True)
        self.assertEqual(p["headline"], "Pickup in 4h 9m")
        self.assertIsNone(p["eta_clock"])       # no arrival projection
        self.assertEqual(p["eta_minutes"], 18)  # vehicle distance still shown

    def test_moderate_slack_keeps_projection(self):
        # slack 50 (<= PANEL_WAIT_SLACK_MIN) -> normal projection, not waiting.
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=60)),
            self.now)
        self.assertNotIn("waiting", p)
        self.assertEqual(p["headline"], "Arrives ~50 min early")

    def test_far_future_infeasible_still_warns(self):
        # Pickup 3h out but the car is 4h of driving away -> at_risk regardless
        # of how far in the future the pickup is.
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=240, dispatch_eta_target_time=self.now + timedelta(minutes=180)),
            self.now)
        self.assertEqual(p["state"], "at_risk")

    def test_stopped_label_formats_hours(self):
        p = build_panel_context(
            self._leg(dispatch_is_moving=False, dispatch_stationary_minutes=385,
                      dispatch_eta_minutes=18, dispatch_eta_target_time=self.now + timedelta(minutes=249)),
            self.now)
        self.assertEqual(p["stopped_label"], "6h 25m")

    def test_clock_times_and_motion_in_context(self):
        # The dispatcher-facing fields: arrival as a wall-clock time, the target's
        # clock time, and the movement snapshot.
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=10, dispatch_eta_target_time=self.now + timedelta(minutes=60),
                      dispatch_is_moving=True, dispatch_vehicle_label="003"),
            self.now)
        self.assertEqual(p["eta_clock"], self.now + timedelta(minutes=10))
        self.assertEqual(p["target_clock"], self.now + timedelta(minutes=60))
        self.assertEqual(p["target_label"], "pickup")
        self.assertIs(p["moving"], True)
        self.assertEqual(p["vehicle"], "003")

    def test_stopped_motion_in_context(self):
        p = build_panel_context(
            self._leg(dispatch_is_moving=False, dispatch_stationary_minutes=12), self.now)
        self.assertIs(p["moving"], False)
        self.assertEqual(p["stationary_minutes"], 12)

    def test_mapped_but_no_gps_shows_no_signal(self):
        # eta None + risk "unknown" = vehicle IS mapped but telematics are stale:
        # grey chip, not silence.
        p = build_panel_context(
            self._leg(dispatch_eta_minutes=None, dispatch_risk_status="unknown",
                      dispatch_risk_reason="Vehicle telematics stale (>15 min)",
                      dispatch_vehicle_label="007"),
            self.now)
        self.assertEqual(p["state"], "no_signal")
        self.assertEqual(p["vehicle"], "007")
        self.assertIn("stale", p["evidence"])

    def test_eta_none_without_unknown_renders_nothing(self):
        self.assertIsNone(build_panel_context(self._leg(dispatch_eta_minutes=None), self.now))


class PanelRenderTests(TestCase):
    def _render(self, panel):
        return render_to_string("dispatching/includes/_samsara_tracking_panel.html", {"panel": panel})

    def test_at_risk_card(self):
        html = self._render({"state": "at_risk", "headline": "~36 min late projected",
                             "evidence": "ETA 38 min · pickup in 2 min"})
        self.assertIn("stp-at_risk", html)
        self.assertIn("At risk", html)
        self.assertIn("~36 min late projected", html)
        self.assertIn("ETA 38 min", html)

    def test_on_track_card_silent(self):
        html = self._render({"state": "on_track", "headline": "Arrives ~50 min early", "evidence": "ETA 10 min · pickup in 60 min"})
        self.assertIn("stp-on_track", html)
        self.assertIn("On track", html)

    def test_none_renders_nothing(self):
        self.assertEqual(self._render(None).strip(), "")

    def test_clock_line_and_motion_chip_render(self):
        now = timezone.make_aware(datetime(2026, 6, 11, 3, 54))
        html = self._render({"state": "tight", "headline": "5 min buffer",
                             "evidence": "ETA 14 min · pickup in 19 min",
                             "eta_minutes": 14, "eta_clock": now + timedelta(minutes=14),
                             "target_clock": now + timedelta(minutes=19), "target_label": "pickup",
                             "moving": True, "stationary_minutes": 0, "vehicle": "003"})
        self.assertIn("Arrives ~4:08 AM", html)
        self.assertIn("pickup 4:13 AM", html)
        self.assertIn("Moving", html)
        self.assertIn("#003", html)
        self.assertIn("14 min", html)

    def test_waiting_card_renders(self):
        now = timezone.make_aware(datetime(2026, 6, 11, 4, 21))
        html = self._render({"state": "on_track", "waiting": True, "headline": "Pickup in 4h 9m",
                             "evidence": "ETA 18 min · pickup in 249 min",
                             "eta_minutes": 18, "eta_clock": None,
                             "target_clock": now + timedelta(minutes=249), "target_label": "pickup",
                             "moving": False, "stationary_minutes": 385, "stopped_label": "6h 25m"})
        self.assertIn("Pickup in 4h 9m", html)
        self.assertIn("Pickup 8:30 AM", html)
        self.assertIn("vehicle ~18 min away", html)
        self.assertIn("Stopped 6h 25m", html)
        self.assertNotIn("Arrives ~", html)

    def test_stopped_chip_renders_duration(self):
        html = self._render({"state": "on_track", "headline": "Arrives ~50 min early",
                             "moving": False, "stationary_minutes": 12})
        self.assertIn("Stopped 12m", html)

    def test_no_motion_key_renders_no_chip(self):
        # A dict without the moving key (or moving=None) must not show a phantom chip.
        html = self._render({"state": "at_risk", "headline": "~5 min late", "evidence": "x"})
        self.assertNotIn("stp-motion", html)

    def test_no_signal_renders_grey_chip(self):
        html = self._render({"state": "no_signal", "headline": "No live GPS",
                             "evidence": "Vehicle telematics stale (>15 min)",
                             "vehicle": "007", "moving": None})
        self.assertIn("stp-no_signal", html)
        self.assertIn("No signal", html)
        self.assertIn("#007", html)

    def test_tag_renders_panel(self):
        from django.template import Template, Context
        leg = SimpleNamespace(status="on-the-way", driver_id=7,
                              dispatch_eta_is_fresh=True, dispatch_eta_minutes=38,
                              dispatch_eta_target_time=timezone.now() + timedelta(minutes=2),
                              pickup_location="Hard Rock Hotel", dispatch_vehicle_label="007",
                              pickup_date=timezone.localdate(), pickup_time=time(9, 0),
                              dispatch_is_moving=True, dispatch_stationary_minutes=0)
        html = Template("{% load samsara_tags %}{% samsara_tracking_panel leg %}").render(Context({"leg": leg}))
        self.assertIn("stp-at_risk", html)
        self.assertIn("late projected", html)
        self.assertIn("#007", html)

    def test_tag_unassigned_renders_nothing(self):
        from django.template import Template, Context
        leg = SimpleNamespace(status="confirmed", driver_id=None,
                              dispatch_eta_is_fresh=False, dispatch_eta_minutes=None,
                              dispatch_eta_target_time=timezone.now() + timedelta(minutes=30),
                              pickup_date=timezone.localdate(), pickup_time=time(9, 0),
                              dispatch_is_moving=None, dispatch_stationary_minutes=None)
        html = Template("{% load samsara_tags %}{% samsara_tracking_panel leg %}").render(Context({"leg": leg}))
        self.assertNotIn("stp-", html)


class EvaluateDriverChainTests(TestCase):
    """The chain: a mid-trip driver's next pickup folds in finishing the current job."""
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 6, 1, 12, 0))

    def _leg(self, lid, status, mins_from_now, pickup="P", dropoff="D"):
        when = self.now + timedelta(minutes=mins_from_now)
        return SimpleNamespace(id=lid, status=status, pickup_time=when.time(),
                               pickup_date=when.date(), pickup_location=pickup,
                               dropoff_location=dropoff, controlling_flight=None)

    def test_free_driver_direct_eta(self):
        leg = self._leg(1, "confirmed", 30, pickup="MCO")
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 600}):
            out = evaluate_driver(_veh_ns(), [leg], self.now)
        self.assertEqual(out[1]["dispatch_eta_target"], "pickup")
        self.assertEqual(out[1]["dispatch_eta_minutes"], 10)

    def test_mid_trip_chains_next_pickup(self):
        mid = self._leg(1, "picked-up", -10, dropoff="DROP")
        nxt = self._leg(2, "confirmed", 30, pickup="NEXT")
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 1200}):  # 20 min
            out = evaluate_driver(_veh_ns(), [mid, nxt], self.now)
        self.assertEqual(out[1]["dispatch_eta_target"], "dropoff")          # current job, informational
        self.assertEqual(out[2]["dispatch_eta_target"], "next_pickup")      # chained
        self.assertEqual(out[2]["dispatch_eta_minutes"], 45)               # 20 + 5 service + 20

    def test_mid_trip_chain_panel_warns(self):
        mid = self._leg(1, "picked-up", -10, dropoff="DROP")
        nxt = self._leg(2, "confirmed", 30, pickup="NEXT")
        with patch("dispatching.samsara_risk.get_drive_time", return_value={"duration_seconds": 1200}):
            out = evaluate_driver(_veh_ns(), [mid, nxt], self.now)
        for k, v in out[2].items():
            setattr(nxt, k, v)
        nxt.dispatch_eta_is_fresh = True
        nxt.driver_id = 1
        p = build_panel_context(nxt, self.now)
        self.assertEqual(p["state"], "at_risk")     # 45-min chained ETA vs pickup in 30
        self.assertIn("after current trip", p["evidence"])


def _veh_at(lat="28.44000", lng="-81.31000", movement="idle"):
    """A Samsara-enabled vehicle namespace at an explicit GPS (lets a test move it)."""
    return SimpleNamespace(
        samsara_enabled=True, samsara_is_fresh=True,
        samsara_last_latitude=Decimal(lat), samsara_last_longitude=Decimal(lng),
        samsara_movement_status=movement, samsara_stationary_since=None)


class EtaReuseCostGateTests(TestCase):
    """
    Cost levers: skip the PAID Google drive-time call when its inputs are unchanged,
    WITHOUT ever skipping the free slack/band recompute.
      Lever 1 — parked car (no meaningful move) reuses stored ETA.
      Lever 2 — far-future pickup reuses stored ETA even if the car moved.
      Lever 4 — a cost-gated cycle (refresh_allowed=False) reuses regardless.
    A moved car or a changed target still pays for Google.
    """
    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 6, 1, 12, 0))

    def _stored_leg(self, lid=1, eta=20, pickup="MCO", target="MCO",
                    origin_lat="28.44000", origin_lng="-81.31000", pickup_in=40,
                    status="confirmed"):
        """A leg that already carries a stored ETA + the anchor it was computed against."""
        when = self.now + timedelta(minutes=pickup_in)
        return SimpleNamespace(
            id=lid, status=status, pickup_time=when.time(), pickup_date=when.date(),
            pickup_location=pickup, dropoff_location="Disney", controlling_flight=None,
            dispatch_eta_minutes=eta,
            dispatch_eta_origin_lat=Decimal(origin_lat),
            dispatch_eta_origin_lng=Decimal(origin_lng),
            dispatch_eta_origin_target=target)

    def test_parked_car_reuses_without_google(self):
        leg = self._stored_leg(eta=20, pickup_in=40)
        with patch("dispatching.samsara_risk.get_drive_time") as m:
            out = evaluate_driver(_veh_at(), [leg], self.now)
        m.assert_not_called()                                 # Lever 1: no paid call
        self.assertEqual(out[1]["dispatch_eta_minutes"], 20)  # reused, not recomputed
        self.assertEqual(out[1]["dispatch_risk_status"], "on_time")  # slack 20

    def test_parked_car_band_advances_with_clock(self):
        # CRITICAL SEPARATION: drive-time stays put, but slack shrinks as the clock
        # moves — the band must flip to at_risk on schedule, with NO Google call.
        leg = self._stored_leg(eta=20, pickup_in=40)  # pickup at 12:40
        later = self.now + timedelta(minutes=25)       # now 12:25 -> pickup in 15
        with patch("dispatching.samsara_risk.get_drive_time") as m:
            out = evaluate_driver(_veh_at(), [leg], later)
        m.assert_not_called()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 20)        # minutes unchanged
        self.assertEqual(out[1]["dispatch_risk_status"], "at_risk")  # band advanced for free

    def test_moved_car_calls_google(self):
        # ~1.1 km north of the anchor — well past the 150 m reuse threshold.
        leg = self._stored_leg(eta=20, pickup_in=40)
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": 900}) as m:
            out = evaluate_driver(_veh_at(lat="28.45000"), [leg], self.now)
        m.assert_called_once()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 15)  # refreshed from Google (900s)

    def test_jitter_under_threshold_still_reuses(self):
        # ~50 m of parked-car GPS jitter must NOT count as moved.
        leg = self._stored_leg(eta=20, pickup_in=40)
        with patch("dispatching.samsara_risk.get_drive_time") as m:
            out = evaluate_driver(_veh_at(lat="28.44045"), [leg], self.now)
        m.assert_not_called()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 20)

    def test_target_changed_calls_google(self):
        # Car hasn't moved, but the pickup location differs from the stored anchor's.
        leg = self._stored_leg(eta=20, pickup="HOTEL", target="MCO", pickup_in=40)
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": 900}) as m:
            out = evaluate_driver(_veh_at(), [leg], self.now)
        m.assert_called_once()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 15)

    def test_far_future_pickup_reuses_despite_move(self):
        # Lever 2: 4 h out -> renders as the waiting card anyway; don't pay to refresh.
        leg = self._stored_leg(eta=20, pickup_in=240)  # > ETA_FAR_FUTURE_MIN (180)
        with patch("dispatching.samsara_risk.get_drive_time") as m:
            out = evaluate_driver(_veh_at(lat="28.46000"), [leg], self.now)  # moved far
        m.assert_not_called()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 20)
        # Anchor is carried forward (the GPS the value was computed against), NOT the
        # current GPS — so cumulative drift is measured from the real compute point.
        self.assertEqual(out[1]["dispatch_eta_origin_lat"], Decimal("28.44000"))

    def test_cadence_gate_reuses_even_when_moved(self):
        # Lever 4: a non-refresh cycle reuses regardless of movement / window.
        leg = self._stored_leg(eta=20, pickup_in=40)
        with patch("dispatching.samsara_risk.get_drive_time") as m:
            out = evaluate_driver(_veh_at(lat="28.50000"), [leg], self.now,
                                  refresh_allowed=False)
        m.assert_not_called()
        self.assertEqual(out[1]["dispatch_eta_minutes"], 20)

    def test_first_compute_always_calls_google(self):
        # No stored ETA yet -> must compute once even on a cost-gated cycle.
        when = self.now + timedelta(minutes=40)
        fresh = SimpleNamespace(
            id=9, status="confirmed", pickup_time=when.time(), pickup_date=when.date(),
            pickup_location="MCO", dropoff_location="Disney", controlling_flight=None,
            dispatch_eta_minutes=None, dispatch_eta_origin_lat=None,
            dispatch_eta_origin_lng=None, dispatch_eta_origin_target="")
        with patch("dispatching.samsara_risk.get_drive_time",
                   return_value={"duration_seconds": 600}) as m:
            out = evaluate_driver(_veh_at(), [fresh], self.now, refresh_allowed=False)
        m.assert_called_once()
        self.assertEqual(out[9]["dispatch_eta_minutes"], 10)


class SnapOriginTests(TestCase):
    """Lever 3: opt-in GPS snapping collapses parked-car jitter onto one cache key."""
    def test_snap_rounds_coords(self):
        from drivers.utils import _snap_coord_origin
        self.assertEqual(_snap_coord_origin("28.441234,-81.312987"), "28.441,-81.313")

    def test_snap_passes_addresses_through(self):
        from drivers.utils import _snap_coord_origin
        self.assertEqual(_snap_coord_origin("Hard Rock Hotel"), "Hard Rock Hotel")
