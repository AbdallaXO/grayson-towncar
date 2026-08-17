"""
Tests for the Fleet Management sync layer (dispatching/fleet_sync.py).

Covers the properties that are expensive to get wrong: idempotency, local day
boundaries, contiguity across midnight, gateway-swap refusal, the nightly gate's
durability across worker recycles, and feed health.
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import (
    FleetSyncState, FleetVehicle, VehicleDayReading, VehicleFault,
    VehicleServiceRecord, VehicleServiceSchedule,
)
from dispatching.fleet_sync import (
    FEED_NIGHTLY,
    accrue_vehicle_day,
    finalise_previous_day,
    record_feed_result,
    refresh_vehicle_master,
    should_reconcile,
)
from dispatching import fleet_health
from dispatching.mileage import METERS_PER_MILE
from dispatching.samsara_service import SamsaraService


def m(miles):
    """Miles -> meters, as a Decimal, for readable fixtures."""
    return (Decimal(str(miles)) * METERS_PER_MILE).quantize(Decimal("0.1"))


def local(y, mo, d, h=12, mi=0):
    """An aware datetime at a LOCAL wall-clock time."""
    return timezone.make_aware(datetime(y, mo, d, h, mi))


class FleetFixtureMixin:
    def vehicle(self, number="001", samsara_id="veh-1", odo=None, dist=None, **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, year=2022, make="Chevrolet", model="Suburban",
            samsara_vehicle_id=samsara_id,
            samsara_odometer_meters=odo,
            samsara_gps_distance_meters=dist,
            **kw,
        )


class AccrueVehicleDayTests(FleetFixtureMixin, TestCase):
    def test_creates_a_row_for_today(self):
        v = self.vehicle(odo=m(50_000))
        now = local(2026, 8, 5, 14)
        out = accrue_vehicle_day(now=now)
        self.assertEqual(out["status"], "success")
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(row.end_odometer_meters, m(50_000))
        self.assertEqual(row.sample_count, 1)

    def test_first_ever_day_has_unknown_mileage_not_zero(self):
        # Nothing to diff against. An em-dash, never a 0 that reads as "parked".
        self.vehicle(odo=m(50_000))
        accrue_vehicle_day(now=local(2026, 8, 5, 14))
        row = VehicleDayReading.objects.get()
        self.assertIsNone(row.miles_driven)
        self.assertEqual(row.mileage_source, "none")

    def test_is_idempotent_on_repeat_runs(self):
        # Two workers racing, or a manual re-run, must not double-count. This is
        # the property that makes miles_driven derived rather than accumulated.
        v = self.vehicle(odo=m(50_000))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(49_900),
        )
        now = local(2026, 8, 5, 14)
        accrue_vehicle_day(now=now)
        first = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(first.miles_driven, Decimal("100.0"))

        accrue_vehicle_day(now=now)
        accrue_vehicle_day(now=now)
        again = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(again.miles_driven, Decimal("100.0"))
        self.assertEqual(VehicleDayReading.objects.filter(date=date(2026, 8, 5)).count(), 1)
        self.assertEqual(again.sample_count, 3)  # samples counted, miles not

    def test_day_opens_from_yesterdays_close_so_no_miles_are_lost(self):
        # An overnight MCO run straddles midnight. If a day opened at its own
        # first sample instead of yesterday's close, those miles would vanish.
        v = self.vehicle(odo=m(50_080))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_000),
        )
        accrue_vehicle_day(now=local(2026, 8, 5, 9))
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(row.start_odometer_meters, m(50_000))
        self.assertEqual(row.miles_driven, Decimal("80.0"))

    def test_uses_local_date_not_utc(self):
        # 8pm local on Aug 5 is Aug 6 in UTC. Booking a whole evening of airport
        # work onto the wrong day would be a real, quiet error.
        v = self.vehicle(odo=m(50_000))
        accrue_vehicle_day(now=local(2026, 8, 5, 20))
        self.assertTrue(
            VehicleDayReading.objects.filter(vehicle=v, date=date(2026, 8, 5)).exists()
        )
        self.assertFalse(
            VehicleDayReading.objects.filter(vehicle=v, date=date(2026, 8, 6)).exists()
        )

    def test_gateway_swap_refuses_to_produce_mileage(self):
        v = self.vehicle(samsara_id="veh-NEW", odo=m(120_000))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-OLD",
            end_odometer_meters=m(50_000),
        )
        accrue_vehicle_day(now=local(2026, 8, 5, 14))
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertIsNone(row.miles_driven)
        self.assertIn("gateway changed", row.mileage_note)

    def test_stationary_day_records_a_real_zero(self):
        v = self.vehicle(odo=m(50_000))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_000),
        )
        accrue_vehicle_day(now=local(2026, 8, 5, 14))
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(row.miles_driven, Decimal("0.0"))
        self.assertEqual(row.mileage_source, "obd")

    def test_vehicle_with_no_telemetry_gets_no_row(self):
        self.vehicle(odo=None, dist=None)
        out = accrue_vehicle_day(now=local(2026, 8, 5, 14))
        self.assertEqual(out["rows"], 0)
        self.assertEqual(VehicleDayReading.objects.count(), 0)

    def test_unmapped_and_inactive_vehicles_are_skipped(self):
        self.vehicle(number="900", samsara_id="", odo=m(1_000))
        self.vehicle(number="901", samsara_id="veh-x", odo=m(1_000), is_active=False)
        out = accrue_vehicle_day(now=local(2026, 8, 5, 14))
        self.assertEqual(out["rows"], 0)

    def test_no_mapped_vehicles_is_a_clean_skip(self):
        out = accrue_vehicle_day(now=local(2026, 8, 5, 14))
        self.assertEqual(out["status"], "skipped")

    def test_gps_only_vehicle_uses_the_distance_counter(self):
        v = self.vehicle(odo=None, dist=m(3_000))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_gps_distance_meters=m(2_950),
        )
        accrue_vehicle_day(now=local(2026, 8, 5, 14))
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(row.miles_driven, Decimal("50.0"))
        self.assertEqual(row.mileage_source, "gps")


class FinalisePreviousDayTests(FleetFixtureMixin, TestCase):
    def test_recomputes_a_day_whose_opener_arrived_late(self):
        # Day 2 was written before day 1 had a closing reading, so it had no
        # opener and read as unknown. The nightly must repair it.
        v = self.vehicle(odo=m(50_200))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_000))
        broken = VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 5), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_200), miles_driven=None, mileage_source="none")

        finalise_previous_day(now=local(2026, 8, 5, 4))
        broken.refresh_from_db()
        self.assertEqual(broken.miles_driven, Decimal("200.0"))
        self.assertEqual(broken.mileage_source, "obd")

    def test_is_safe_with_no_rows(self):
        self.assertEqual(finalise_previous_day(now=local(2026, 8, 5, 4))["rows"], 0)

    def test_repeated_runs_are_stable(self):
        v = self.vehicle(odo=m(50_200))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 4), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_000))
        VehicleDayReading.objects.create(
            vehicle=v, date=date(2026, 8, 5), samsara_vehicle_id="veh-1",
            end_odometer_meters=m(50_200))
        for _ in range(3):
            finalise_previous_day(now=local(2026, 8, 5, 4))
        row = VehicleDayReading.objects.get(vehicle=v, date=date(2026, 8, 5))
        self.assertEqual(row.miles_driven, Decimal("200.0"))


class NightlyGateTests(TestCase):
    def test_closed_outside_the_window(self):
        self.assertFalse(should_reconcile(now=local(2026, 8, 5, 14)))
        self.assertFalse(should_reconcile(now=local(2026, 8, 5, 2)))
        self.assertFalse(should_reconcile(now=local(2026, 8, 5, 6)))

    def test_open_inside_the_window_when_never_run(self):
        self.assertTrue(should_reconcile(now=local(2026, 8, 5, 3)))
        self.assertTrue(should_reconcile(now=local(2026, 8, 5, 5, 59)))

    def test_closes_once_today_has_run(self):
        record_feed_result(FEED_NIGHTLY, "success", now=local(2026, 8, 5, 3, 5))
        self.assertFalse(should_reconcile(now=local(2026, 8, 5, 3, 30)))

    def test_reopens_the_next_day(self):
        record_feed_result(FEED_NIGHTLY, "success", now=local(2026, 8, 5, 3, 5))
        self.assertTrue(should_reconcile(now=local(2026, 8, 6, 3, 5)))

    def test_stamp_survives_a_worker_recycle(self):
        # The whole reason the stamp is in the DB: ghl_integration's in-memory
        # _cycle_count resets on every recycle, and --max-requests 1500 makes
        # those routine. Simulated here by dropping all module state.
        record_feed_result(FEED_NIGHTLY, "success", now=local(2026, 8, 5, 3, 5))
        import importlib

        import dispatching.fleet_sync as fs
        importlib.reload(fs)
        self.assertFalse(fs.should_reconcile(now=local(2026, 8, 5, 4)))

    def test_a_failed_run_does_not_close_the_gate(self):
        record_feed_result(FEED_NIGHTLY, "error", error="boom", now=local(2026, 8, 5, 3, 5))
        self.assertTrue(should_reconcile(now=local(2026, 8, 5, 3, 30)))


class FeedHealthTests(TestCase):
    def test_success_clears_the_failure_streak(self):
        record_feed_result("vehicle_stats", "error", error="nope")
        record_feed_result("vehicle_stats", "error", error="nope")
        state = FleetSyncState.objects.get(feed="vehicle_stats")
        self.assertEqual(state.consecutive_failures, 2)
        self.assertIsNone(state.last_success_at)

        record_feed_result("vehicle_stats", "success")
        state.refresh_from_db()
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.last_error, "")
        self.assertIsNotNone(state.last_success_at)

    def test_last_run_advances_even_on_failure(self):
        # The distinction that matters: "we tried and it failed" vs "nothing has
        # run at all". The 25-day outage looked like the second.
        record_feed_result("vehicle_stats", "error", error="401")
        state = FleetSyncState.objects.get(feed="vehicle_stats")
        self.assertIsNotNone(state.last_run_at)
        self.assertIsNone(state.last_success_at)

    def test_long_errors_are_truncated_not_raised(self):
        record_feed_result("vehicle_stats", "error", error="x" * 5000)
        self.assertLessEqual(
            len(FleetSyncState.objects.get(feed="vehicle_stats").last_error), 2000)


class RefreshVehicleMasterTests(FleetFixtureMixin, TestCase):
    def _payload(self, **kw):
        base = {"id": "veh-1", "name": "Unit 001",
                "vin": "1GNSKJKC5PR100001", "licensePlate": "ABC1234"}
        base.update(kw)
        return {"status": "success", "data": [base]}

    def test_fills_in_blank_identity_fields(self):
        v = self.vehicle()
        with patch.object(SamsaraService, "list_vehicles", return_value=self._payload()):
            out = refresh_vehicle_master(service=SamsaraService())
        self.assertEqual(out["updated"], 1)
        v.refresh_from_db()
        self.assertEqual(v.vin, "1GNSKJKC5PR100001")
        self.assertEqual(v.license_plate, "ABC1234")
        self.assertEqual(v.samsara_name, "Unit 001")

    def test_vin_drift_is_detected_audited_and_not_auto_corrected(self):
        # A gateway moved between cars. Silently re-attributing history is the
        # worst outcome available, so this must be loud and must NOT self-heal.
        from reservations.models import AuditLog

        v = self.vehicle()
        FleetVehicle.objects.filter(pk=v.pk).update(vin="1GNSKJKC5PR100001")
        drifted = self._payload(vin="5LMJJ2LT0KEL00002")

        with patch.object(SamsaraService, "list_vehicles", return_value=drifted):
            out = refresh_vehicle_master(service=SamsaraService())

        self.assertEqual(out["vin_drift"], 1)
        v.refresh_from_db()
        self.assertEqual(v.vin, "1GNSKJKC5PR100001")  # unchanged on purpose
        log = AuditLog.objects.get(model_name="FleetVehicle", field_name="vin")
        self.assertEqual(log.new_value, "5LMJJ2LT0KEL00002")
        self.assertIn("gateway swap", log.notes.lower())

    def test_api_failure_writes_nothing(self):
        v = self.vehicle()
        with patch.object(SamsaraService, "list_vehicles",
                          return_value={"status": "error", "error": "401"}):
            out = refresh_vehicle_master(service=SamsaraService())
        self.assertEqual(out["updated"], 0)
        v.refresh_from_db()
        self.assertEqual(v.vin, "")

    def test_unmapped_samsara_vehicles_are_ignored(self):
        self.vehicle(samsara_id="veh-OTHER")
        with patch.object(SamsaraService, "list_vehicles", return_value=self._payload()):
            out = refresh_vehicle_master(service=SamsaraService())
        self.assertEqual(out["updated"], 0)


class ServiceScheduleTests(FleetFixtureMixin, TestCase):
    def test_due_at_odometer_from_a_mileage_interval(self):
        v = self.vehicle()
        s = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="oil", interval_miles=5000,
            last_done_odometer_miles=Decimal("50000.0"))
        self.assertEqual(s.due_at_odometer_miles, Decimal("55000.0"))

    def test_due_on_date_from_a_day_interval(self):
        v = self.vehicle()
        s = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="oil", interval_days=180,
            last_done_on=date(2026, 1, 1))
        self.assertEqual(s.due_on_date, date(2026, 1, 1) + timedelta(days=180))

    def test_missing_inputs_yield_none_not_a_guess(self):
        v = self.vehicle()
        s = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="oil", interval_miles=5000)
        self.assertIsNone(s.due_at_odometer_miles)
        self.assertIsNone(s.due_on_date)


class VehicleFaultTests(FleetFixtureMixin, TestCase):
    def test_one_open_episode_per_external_id(self):
        from django.db import IntegrityError

        v = self.vehicle()
        now = timezone.now()
        VehicleFault.objects.create(
            vehicle=v, source="obd_fault", external_id="f-1",
            first_seen_at=now, last_seen_at=now)
        with self.assertRaises(IntegrityError):
            VehicleFault.objects.create(
                vehicle=v, source="obd_fault", external_id="f-1",
                first_seen_at=now, last_seen_at=now)

    def test_a_resolved_episode_lets_the_same_code_recur(self):
        v = self.vehicle()
        now = timezone.now()
        VehicleFault.objects.create(
            vehicle=v, source="obd_fault", external_id="f-1",
            first_seen_at=now, last_seen_at=now, resolved_at=now)
        recurrence = VehicleFault.objects.create(
            vehicle=v, source="obd_fault", external_id="f-1",
            first_seen_at=now, last_seen_at=now)
        self.assertTrue(recurrence.is_open)
        self.assertEqual(VehicleFault.objects.filter(external_id="f-1").count(), 2)


class FleetPageTests(FleetFixtureMixin, TestCase):
    """
    Render contracts. These also prove the templates parse — a template syntax
    error is invisible until something renders it.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            "dispatcher", "d@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)

    def test_list_renders_for_staff(self):
        self.vehicle(odo=m(50_000))
        resp = self.client.get(reverse("fleet_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "#001")

    def test_unknown_mileage_renders_an_em_dash_not_zero(self):
        # The single most important rendering rule in this module.
        self.vehicle(odo=None)
        resp = self.client.get(reverse("fleet_list"))
        html = resp.content.decode()
        self.assertIn("—", html)
        self.assertIn("no reading, not zero miles", html)

    def test_feed_health_is_loud_when_nothing_has_ever_synced(self):
        self.vehicle()
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "No Samsara data")
        self.assertContains(resp, "SAMSARA_API_TOKEN")

    def test_feed_health_reads_live_after_a_recent_success(self):
        self.vehicle()
        record_feed_result("vehicle_stats", "success")
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "Samsara: Live")

    def test_unmapped_filter_surfaces_the_onboarding_backlog(self):
        self.vehicle(number="001", samsara_id="veh-1")
        self.vehicle(number="004", samsara_id="")
        resp = self.client.get(reverse("fleet_list"), {"coverage": "unmapped"})
        html = resp.content.decode()
        self.assertIn("#004", html)
        self.assertNotIn("#001", html)

    def test_search_matches_vin_and_plate(self):
        v = self.vehicle(number="007")
        FleetVehicle.objects.filter(pk=v.pk).update(
            vin="1GNSKJKC5PR100001", license_plate="XYZ9876")
        self.assertContains(
            self.client.get(reverse("fleet_list"), {"q": "XYZ9876"}), "#007")
        self.assertContains(
            self.client.get(reverse("fleet_list"), {"q": "1GNSKJKC5PR"}), "#007")

    def test_units_sort_naturally_not_lexically(self):
        # '001' before '10' before '13' — a plain string sort gets this wrong.
        for number in ("13", "001", "10", "002"):
            self.vehicle(number=number, samsara_id=f"veh-{number}")
        resp = self.client.get(reverse("fleet_list"))
        html = resp.content.decode()
        order = [html.index(f"#{n}") for n in ("001", "002", "10", "13")]
        self.assertEqual(order, sorted(order))

    def test_low_fuel_shows_an_advisory_chip(self):
        self.vehicle(odo=m(1_000), samsara_fuel_percent=8)
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "Fuel 8%")

    def test_detail_renders(self):
        v = self.vehicle(odo=m(50_000))
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "50,000")

    def test_a_healthy_vehicle_shows_no_attention_panel(self):
        # Silence is the correct output for a car with nothing wrong. A panel
        # that always renders trains people to ignore it.
        v = self.vehicle(odo=m(50_000))
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertNotContains(resp, "Needs attention")

    def test_chips_carry_the_advisory_disclaimer(self):
        # Anything that looks like a warning must say it does not block work.
        v = self.vehicle(odo=m(50_000), samsara_fuel_percent=8)
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertContains(resp, "Needs attention")
        self.assertContains(resp, "Advisory only")

    def test_detail_shows_provenance_on_the_odometer(self):
        v = self.vehicle(odo=m(50_000))
        FleetVehicle.objects.filter(pk=v.pk).update(samsara_odometer_source="gps")
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertContains(resp, "fleet-src gps")

    def test_detail_of_an_unmapped_vehicle_says_so_calmly(self):
        v = self.vehicle(samsara_id="")
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Not onboarded")

    def test_pages_do_not_call_samsara(self):
        # DB-only is a hard rule: a 30s statement_timeout and a 60s gunicorn
        # timeout both sit on the request path, and a synchronous external call
        # in render already caused one worker-timeout incident.
        v = self.vehicle(odo=m(1_000))
        with patch.object(SamsaraService, "get_vehicle_stats") as stats, \
             patch.object(SamsaraService, "list_vehicles") as listing:
            self.client.get(reverse("fleet_list"))
            self.client.get(reverse("fleet_detail", args=[v.pk]))
        stats.assert_not_called()
        listing.assert_not_called()

    def test_non_staff_cannot_reach_the_pages(self):
        from django.contrib.auth.models import User

        User.objects.create_user("guest", "g@example.com", "pw")
        self.client.logout()
        self.client.login(username="guest", password="pw")
        resp = self.client.get(reverse("fleet_list"))
        self.assertNotEqual(resp.status_code, 200)


class FleetEditEndpointTests(FleetFixtureMixin, TestCase):
    """
    In-page editing. The whole fleet job must be doable from the page, so these
    endpoints replace the Django admin for a dispatcher.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            "dispatcher", "d@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)
        self.v = self.vehicle(odo=m(50_000))

    def post(self, name, pk, payload):
        return self.client.post(
            reverse(name, args=[pk]), data=json.dumps(payload),
            content_type="application/json")

    # ── compliance details ──────────────────────────────────────────────
    def test_saves_compliance_dates_and_notes(self):
        r = self.post("fleet_update_details", self.v.pk, {
            "registration_expires_on": "2027-01-31",
            "insurance_expires_on": "2026-12-01",
            "notes": "Rear tyres due soon",
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.v.refresh_from_db()
        self.assertEqual(self.v.registration_expires_on, date(2027, 1, 31))
        self.assertEqual(self.v.notes, "Rear tyres due soon")

    def test_blank_clears_a_date(self):
        FleetVehicle.objects.filter(pk=self.v.pk).update(
            registration_expires_on=date(2027, 1, 31))
        self.post("fleet_update_details", self.v.pk, {"registration_expires_on": ""})
        self.v.refresh_from_db()
        self.assertIsNone(self.v.registration_expires_on)

    def test_bad_date_is_a_400_with_a_readable_message(self):
        r = self.post("fleet_update_details", self.v.pk,
                      {"registration_expires_on": "next tuesday"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("date", r.json()["error"].lower())

    def test_saves_a_toll_transponder(self):
        r = self.post("fleet_update_details", self.v.pk, {
            "transponder_number": "SP-0093412", "transponder_type": "sunpass"})
        self.assertTrue(r.json()["success"])
        self.v.refresh_from_db()
        self.assertEqual(self.v.transponder_number, "SP-0093412")
        self.assertEqual(self.v.get_transponder_type_display(), "SunPass")

    def test_transponder_is_searchable_from_the_list(self):
        # The point of storing it: trace a toll charge back to a unit.
        FleetVehicle.objects.filter(pk=self.v.pk).update(transponder_number="SP-0093412")
        resp = self.client.get(reverse("fleet_list"), {"q": "0093412"})
        self.assertContains(resp, f"#{self.v.vehicle_number}")

    def test_unknown_transponder_network_is_rejected(self):
        r = self.post("fleet_update_details", self.v.pk,
                      {"transponder_type": "moon-tolls"})
        self.assertEqual(r.status_code, 400)

    def test_transponder_can_be_cleared_when_moved_between_cars(self):
        FleetVehicle.objects.filter(pk=self.v.pk).update(transponder_number="SP-1")
        self.post("fleet_update_details", self.v.pk, {"transponder_number": ""})
        self.v.refresh_from_db()
        self.assertEqual(self.v.transponder_number, "")

    def test_cannot_edit_samsara_owned_fields(self):
        # The poller owns these; an edit here would be overwritten in 3 minutes.
        self.post("fleet_update_details", self.v.pk,
                  {"vin": "HACKED", "samsara_odometer_meters": "1"})
        self.v.refresh_from_db()
        self.assertNotEqual(self.v.vin, "HACKED")

    # ── schedules ───────────────────────────────────────────────────────
    def test_creates_an_interval(self):
        r = self.post("fleet_save_schedule", self.v.pk, {
            "service_type": "oil", "interval_miles": "5000",
            "last_done_odometer_miles": "48000", "last_done_on": "2026-06-01"})
        self.assertTrue(r.json()["success"])
        s = VehicleServiceSchedule.objects.get(vehicle=self.v, service_type="oil")
        self.assertEqual(s.interval_miles, 5000)
        self.assertEqual(s.due_at_odometer_miles, Decimal("53000.0"))

    def test_resaving_the_same_type_edits_rather_than_duplicating(self):
        # (vehicle, service_type) is unique — a plain create would 500.
        for miles in ("5000", "7500"):
            self.post("fleet_save_schedule", self.v.pk,
                      {"service_type": "oil", "interval_miles": miles})
        self.assertEqual(
            VehicleServiceSchedule.objects.filter(vehicle=self.v).count(), 1)
        self.assertEqual(
            VehicleServiceSchedule.objects.get(vehicle=self.v).interval_miles, 7500)

    def test_an_interval_with_neither_bound_is_rejected(self):
        # It could never come due — it would sit there looking active.
        r = self.post("fleet_save_schedule", self.v.pk, {"service_type": "oil"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("interval", r.json()["error"].lower())

    def test_unknown_service_type_is_rejected(self):
        r = self.post("fleet_save_schedule", self.v.pk,
                      {"service_type": "teleportation", "interval_miles": "10"})
        self.assertEqual(r.status_code, 400)

    def test_deletes_an_interval(self):
        s = VehicleServiceSchedule.objects.create(
            vehicle=self.v, service_type="oil", interval_miles=5000)
        self.assertTrue(self.post("fleet_delete_schedule", s.pk, {}).json()["success"])
        self.assertFalse(VehicleServiceSchedule.objects.filter(pk=s.pk).exists())

    # ── service records ─────────────────────────────────────────────────
    def test_logs_a_service_record(self):
        r = self.post("fleet_add_service", self.v.pk, {
            "service_type": "oil", "performed_on": "2026-08-01",
            "odometer_miles": "50000", "vendor": "Bob's Garage", "cost": "89.99"})
        self.assertTrue(r.json()["success"])
        rec = VehicleServiceRecord.objects.get(vehicle=self.v)
        self.assertEqual(rec.vendor, "Bob's Garage")
        self.assertEqual(rec.cost, Decimal("89.99"))
        self.assertEqual(rec.created_by, self.staff)

    def test_logging_a_service_advances_the_matching_interval(self):
        # The reason this is worth doing in-page: no double entry.
        s = VehicleServiceSchedule.objects.create(
            vehicle=self.v, service_type="oil", interval_miles=5000,
            last_done_on=date(2026, 1, 1),
            last_done_odometer_miles=Decimal("45000.0"))
        r = self.post("fleet_add_service", self.v.pk, {
            "service_type": "oil", "performed_on": "2026-08-01",
            "odometer_miles": "50000"})
        self.assertTrue(r.json()["schedule_advanced"])
        s.refresh_from_db()
        self.assertEqual(s.last_done_on, date(2026, 8, 1))
        self.assertEqual(s.last_done_odometer_miles, Decimal("50000.0"))
        self.assertEqual(s.due_at_odometer_miles, Decimal("55000.0"))

    def test_backdated_receipt_does_not_rewind_a_newer_service(self):
        s = VehicleServiceSchedule.objects.create(
            vehicle=self.v, service_type="oil", interval_miles=5000,
            last_done_on=date(2026, 7, 1),
            last_done_odometer_miles=Decimal("49000.0"))
        r = self.post("fleet_add_service", self.v.pk, {
            "service_type": "oil", "performed_on": "2026-03-01",
            "odometer_miles": "40000"})
        self.assertFalse(r.json()["schedule_advanced"])
        s.refresh_from_db()
        self.assertEqual(s.last_done_on, date(2026, 7, 1))
        self.assertEqual(s.last_done_odometer_miles, Decimal("49000.0"))

    def test_a_service_with_no_matching_interval_still_logs(self):
        r = self.post("fleet_add_service", self.v.pk,
                      {"service_type": "repair", "performed_on": "2026-08-01"})
        self.assertTrue(r.json()["success"])
        self.assertFalse(r.json()["schedule_advanced"])

    def test_future_dated_service_is_rejected(self):
        future = (timezone.localdate() + timedelta(days=3)).isoformat()
        r = self.post("fleet_add_service", self.v.pk,
                      {"service_type": "oil", "performed_on": future})
        self.assertEqual(r.status_code, 400)
        self.assertIn("future", r.json()["error"].lower())

    def test_service_requires_a_date(self):
        r = self.post("fleet_add_service", self.v.pk, {"service_type": "oil"})
        self.assertEqual(r.status_code, 400)

    def test_backwards_out_of_service_window_is_rejected(self):
        r = self.post("fleet_add_service", self.v.pk, {
            "service_type": "repair", "performed_on": "2026-08-01",
            "out_of_service_from": "2026-08-05", "out_of_service_to": "2026-08-02"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("before", r.json()["error"].lower())

    def test_out_of_service_window_shows_as_a_label_only(self):
        # It must never remove the unit from any pool — see the Guard A removal.
        self.post("fleet_add_service", self.v.pk, {
            "service_type": "repair", "performed_on": "2026-08-01",
            "out_of_service_from": timezone.localdate().isoformat()})
        resp = self.client.get(reverse("fleet_detail", args=[self.v.pk]))
        self.assertContains(resp, "In shop")
        self.v.refresh_from_db()
        self.assertTrue(self.v.is_active)  # untouched

    def test_deletes_a_service_record(self):
        rec = VehicleServiceRecord.objects.create(
            vehicle=self.v, service_type="oil", performed_on=date(2026, 8, 1))
        self.assertTrue(self.post("fleet_delete_service", rec.pk, {}).json()["success"])
        self.assertFalse(VehicleServiceRecord.objects.filter(pk=rec.pk).exists())

    # ── access + method ─────────────────────────────────────────────────
    def test_endpoints_reject_get(self):
        self.assertEqual(
            self.client.get(reverse("fleet_update_details", args=[self.v.pk])).status_code,
            405)

    def test_endpoints_reject_non_staff(self):
        from django.contrib.auth.models import User

        User.objects.create_user("guest", "g@example.com", "pw")
        self.client.logout()
        self.client.login(username="guest", password="pw")
        r = self.post("fleet_update_details", self.v.pk, {"notes": "nope"})
        self.assertNotEqual(r.status_code, 200)
        self.v.refresh_from_db()
        self.assertEqual(self.v.notes, "")

    def test_malformed_json_is_a_400_not_a_500(self):
        r = self.client.post(
            reverse("fleet_update_details", args=[self.v.pk]),
            data="{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)


class MetricLabellingTests(FleetFixtureMixin, TestCase):
    """
    Fuel and engine state ARE collected — the earlier "not reported" wording
    came from reading the wrong response key, not from a plan limitation.
    These lock in that the page shows the real value.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_user(
            "d2", "d2@example.com", "pw", is_staff=True))

    def test_fuel_and_engine_state_are_requested(self):
        from dispatching.samsara_service import EXTENDED_STAT_TYPES

        self.assertIn("fuelPercents", EXTENDED_STAT_TYPES)
        self.assertIn("engineStates", EXTENDED_STAT_TYPES)

    def test_fuel_renders_its_value(self):
        v = self.vehicle(odo=m(1_000), samsara_fuel_percent=41)
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertContains(resp, "41%")
        self.assertNotContains(resp, "not reported")

    def test_a_vehicle_with_no_fuel_reading_shows_a_dash_not_a_zero(self):
        v = self.vehicle(odo=m(1_000))
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertContains(resp, "—")

    def test_low_fuel_raises_an_advisory_chip(self):
        v = self.vehicle(odo=m(1_000), samsara_fuel_percent=9)
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertContains(resp, "Fuel 9%")

    def test_a_type_we_do_not_request_is_labelled_not_reported(self):
        # The "not reported" wording still exists for genuinely uncollected
        # metrics — it's driven off EXTENDED_STAT_TYPES, so it can't go stale.
        from dispatching import fleet_views

        self.assertFalse(fleet_views.EXTENDED_STAT_TYPES.__contains__("tirePressure"))


class CoverageWordingTests(TestCase):
    def test_no_known_days_explains_itself_instead_of_saying_zero(self):
        # "across 0 of 1 days" beside an em-dash reads like a bug. It isn't —
        # day one has no prior close to diff against.
        text = fleet_health.summarise_coverage(0, 1)
        self.assertNotIn("0 of 1", text)
        self.assertIn("prior day", text)

    def test_full_coverage_reads_plainly(self):
        self.assertEqual(fleet_health.summarise_coverage(30, 30), "across all 30 days")

    def test_partial_coverage_states_the_gap(self):
        self.assertEqual(
            fleet_health.summarise_coverage(26, 31), "across 26 of 31 days")

    def test_no_rows_at_all_says_nothing(self):
        self.assertEqual(fleet_health.summarise_coverage(0, 0), "")


class FuelColumnTests(FleetFixtureMixin, TestCase):
    """
    Fuel on the list page.

    The column answers one question at 6am — "who am I sending for gas tonight"
    — so what matters is that it never lies about it: an unknown level is not an
    empty tank, a stale reading is not a current one, and the bands it colours
    by are the same bands the readiness chip warns on.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            "fuel_dispatcher", "f@example.com", "pw", is_staff=True)
        self.client.force_login(self.staff)
        self.now = timezone.now()

    def test_a_car_that_never_reported_fuel_has_no_reading(self):
        # Not 0%. A GPS-only gateway legitimately never sends a level, and an
        # empty gauge would send someone to a car that's actually full.
        self.assertIsNone(
            fleet_health.fuel_reading(self.vehicle(), self.now))

    def test_the_bands_match_the_readiness_chip(self):
        """One tank, one verdict — the column and the chip read the same
        thresholds, so they can never disagree on the same car."""
        for percent, expected in (
            (fleet_health.FUEL_CRITICAL_PCT, fleet_health.CRITICAL),
            (fleet_health.FUEL_CRITICAL_PCT + 1, fleet_health.WARN),
            (fleet_health.FUEL_LOW_PCT, fleet_health.WARN),
            (fleet_health.FUEL_LOW_PCT + 1, fleet_health.INFO),
        ):
            reading = fleet_health.fuel_reading(
                self.vehicle(number=f"f{percent}", samsara_id=f"veh-f{percent}",
                             samsara_fuel_percent=percent),
                self.now)
            self.assertEqual(reading["level"], expected, f"at {percent}%")

    def test_a_fresh_reading_is_not_marked_stale(self):
        v = self.vehicle(samsara_fuel_percent=64)
        v.samsara_last_seen_at = self.now - timedelta(minutes=8)
        reading = fleet_health.fuel_reading(v, self.now)
        self.assertFalse(reading["stale"])

    def test_a_quiet_gateway_marks_its_last_level_as_stale(self):
        """Fuel is the one reading that changes while nobody is watching."""
        v = self.vehicle(samsara_fuel_percent=64)
        v.samsara_last_seen_at = self.now - timedelta(
            hours=fleet_health.TELEMETRY_STALE_HOURS + 1)
        reading = fleet_health.fuel_reading(v, self.now)
        self.assertTrue(reading["stale"])
        self.assertIn("may be lower now", reading["detail"])

    def test_the_list_shows_a_percentage(self):
        self.vehicle(samsara_fuel_percent=41)
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "41%")

    def test_the_list_shows_a_dash_for_a_car_with_no_reading(self):
        self.vehicle(samsara_fuel_percent=None)
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "never reported a fuel level")

    def test_sorting_by_fuel_puts_the_emptiest_car_first(self):
        self.vehicle(number="full", samsara_id="veh-full", samsara_fuel_percent=90)
        self.vehicle(number="empty", samsara_id="veh-empty", samsara_fuel_percent=6)
        self.vehicle(number="half", samsara_id="veh-half", samsara_fuel_percent=50)
        html = self.client.get(
            reverse("fleet_list"), {"sort": "fuel"}).content.decode()
        self.assertLess(html.index("#empty"), html.index("#half"))
        self.assertLess(html.index("#half"), html.index("#full"))

    def test_an_unknown_level_sorts_last_not_as_an_empty_tank(self):
        self.vehicle(number="known", samsara_id="veh-k", samsara_fuel_percent=30)
        self.vehicle(number="silent", samsara_id="veh-s", samsara_fuel_percent=None)
        html = self.client.get(
            reverse("fleet_list"), {"sort": "fuel"}).content.decode()
        self.assertLess(html.index("#known"), html.index("#silent"))


class OdometerDisplayTests(FleetFixtureMixin, TestCase):
    def test_odometer_miles_is_none_when_never_reported(self):
        # Templates render this as an em-dash. A 0 would read as a brand-new car.
        self.assertIsNone(self.vehicle().odometer_miles)

    def test_odometer_miles_converts_from_meters(self):
        v = self.vehicle(odo=m(50_000))
        self.assertEqual(v.odometer_miles, Decimal("50000"))

    def test_estimate_flag_tracks_the_stored_source(self):
        v = self.vehicle(odo=m(1_000))
        v.samsara_odometer_source = "obd"
        self.assertFalse(v.odometer_is_estimate)
        v.samsara_odometer_source = "gps"
        self.assertTrue(v.odometer_is_estimate)
