"""The Fleet desk: downtime ledger endpoints, the demand check, the attention
list, fault episodes, alerts, the report, and the fleet-manager experience.

Run with:  ./manage.py test dispatching.tests_fleet_desk

The pure modules (fleet_capacity, fleet_attention) are tested with hand-made
inputs; the endpoints through the test client as staff. Nothing here calls
Samsara, and nothing here sends a text (fleet_notify is inert under TESTING).
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dispatching import (
    fleet_attention, fleet_capacity, fleet_desk, fleet_notify, fleet_report,
)
from dispatching.fleet_sync import upsert_fault_episodes
from dispatching.samsara_service import extract_fault_codes
from drivers.models import (
    Driver, DriverVehicleAssignment, FleetVehicle, VehicleDowntime, VehicleFault,
    VehicleIssue, VehicleServiceRecord, VehicleServiceSchedule,
)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from users.models import UserProfile

TODAY = timezone.localdate()
DAY = TODAY + timedelta(days=5)


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

class _FleetFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.sprinter = Vehicle.objects.create(
            vehicle_type="Van(14 Pax)", capacity=14, luggage_capacity=14,
            requires_certification=True)
        cls.towncar = Vehicle.objects.create(vehicle_type="towncar", capacity=3, luggage_capacity=3)
        mco = Location.objects.create(name="MCO")
        disney = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=mco, destination=disney, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.suv, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="j@example.com",
            phone_number="5551234567")
        cls.staff = User.objects.create_user("fd_staff", password="x", is_staff=True)
        cls.george = Driver.objects.create(
            profile=User.objects.create_user("fd_george", first_name="George"),
            driver_type="inhouse")

    def setUp(self):
        self.client.force_login(self.staff)

    def unit(self, number, vtype=None, **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, vehicle_type=vtype or self.suv, year=2024,
            make="Chevrolet", model="Suburban", **kw)

    def leg(self, day, hour=9, vtype=None, pickup="MCO Terminal B", dropoff="Disney"):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=vtype or self.suv, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=time(hour, 0),
            pickup_location=pickup, dropoff_location=dropoff,
            route=self.route, status="confirmed")

    def post_json(self, name, args, payload):
        return self.client.post(reverse(name, args=args), data=json.dumps(payload),
                                content_type="application/json")


def _demand(day, cumulative, peak=None, per_type=None, total=None):
    """A day_demand dict with only the fields judge_day reads."""
    return {
        "date": day, "total": total if total is not None else sum(cumulative.values()),
        "per_type": per_type or {}, "peak": peak if peak is not None else max(cumulative.values(), default=0),
        "peak_at": "9:30 AM", "cumulative": cumulative,
        "cumulative_at": {k: "9:30 AM" for k in cumulative},
    }


def _fake_unit(uid, vtype):
    return SimpleNamespace(id=uid, vehicle_number=str(uid),
                           vehicle_type=SimpleNamespace(vehicle_type=vtype) if vtype else None)


# ════════════════════════════════════════════════════════════════════════════
# fleet_capacity — the arithmetic
# ════════════════════════════════════════════════════════════════════════════

class JudgeDayTests(TestCase):
    """Nested tiers: a Sprinter can run an SUV job; an SUV cannot run a Sprinter job."""

    def _supply(self, *units):
        return {"available": list(units), "down": []}

    def test_clear_when_every_tier_has_a_spare(self):
        units = [_fake_unit(1, "suv"), _fake_unit(2, "suv"),
                 _fake_unit(3, "Van(14 Pax)"), _fake_unit(4, "Van(14 Pax)")]
        # cumulative: peak of in-flight legs with tier >= t
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"suv": 2, "Van(14 Pax)": 1}), self._supply(*units))
        self.assertEqual(verdict["level"], "clear")
        self.assertEqual(verdict["reasons"], [])

    def test_reasons_lead_with_the_worst_tier_and_skip_restatements(self):
        units = [_fake_unit(1, "suv"), _fake_unit(2, "suv")]
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"Van(14 Pax)": 1, "suv": 2}), self._supply(*units))
        self.assertEqual(verdict["level"], "conflict")
        self.assertTrue(verdict["reasons"][0].startswith("Sprinter"))
        # "Any car: 2 needed" would only restate the SUV line in vaguer words.
        self.assertFalse(any(r.startswith("Any car") for r in verdict["reasons"]))

    def test_tight_when_a_tier_lands_exactly_on_the_fleet(self):
        units = [_fake_unit(1, "suv"), _fake_unit(3, "Van(14 Pax)")]
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"suv": 2, "Van(14 Pax)": 1}), self._supply(*units))
        self.assertEqual(verdict["level"], "tight")
        self.assertTrue(any("Sprinter" in r for r in verdict["reasons"]))

    def test_conflict_when_a_tier_needs_more_than_is_left(self):
        units = [_fake_unit(1, "suv"), _fake_unit(2, "suv")]
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"Van(14 Pax)": 1, "suv": 2}), self._supply(*units))
        self.assertEqual(verdict["level"], "conflict")
        self.assertIn("Sprinter", verdict["reasons"][0])
        self.assertIn("0 left", verdict["reasons"][0])

    def test_a_higher_tier_covers_a_lower_tier_job(self):
        """Two Sprinters can cover two SUV legs — nested compatibility."""
        units = [_fake_unit(1, "Van(14 Pax)"), _fake_unit(2, "Van(14 Pax)"), _fake_unit(3, "suv")]
        verdict = fleet_capacity.judge_day(_demand(TODAY, {"suv": 2}), self._supply(*units))
        self.assertEqual(verdict["level"], "clear")

    def test_towncar_legs_are_served_by_anything(self):
        units = [_fake_unit(1, "suv")]
        verdict = fleet_capacity.judge_day(_demand(TODAY, {"towncar": 1}), self._supply(*units))
        self.assertEqual(verdict["level"], "tight")  # exactly one car for one leg

    def test_history_makes_a_clear_day_short(self):
        """Booked demand is a floor; the weekday's real use is the second opinion."""
        units = [_fake_unit(1, "suv"), _fake_unit(2, "suv")]
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"suv": 1}), self._supply(*units), typical_units=3)
        self.assertEqual(verdict["level"], "conflict")
        self.assertTrue(any("have run 3 cars" in r for r in verdict["reasons"]))

    def test_history_equal_to_supply_is_tight(self):
        units = [_fake_unit(1, "suv"), _fake_unit(2, "suv")]
        verdict = fleet_capacity.judge_day(
            _demand(TODAY, {"suv": 1}), self._supply(*units), typical_units=2)
        self.assertEqual(verdict["level"], "tight")

    def test_busyness_bands(self):
        units = [_fake_unit(i, "suv") for i in range(10)]
        quiet = fleet_capacity.judge_day(_demand(TODAY, {"suv": 3}), self._supply(*units))
        busy = fleet_capacity.judge_day(_demand(TODAY, {"suv": 9}), self._supply(*units))
        over = fleet_capacity.judge_day(_demand(TODAY, {"suv": 11}), self._supply(*units))
        self.assertEqual(quiet["busyness"], "quiet")
        self.assertEqual(busy["busyness"], "busy")
        self.assertEqual(over["busyness"], "over")
        self.assertEqual(over["level"], "conflict")

    def test_untyped_legs_still_need_a_body(self):
        units = [_fake_unit(1, "suv")]
        demand = _demand(TODAY, {}, peak=2, total=2)
        verdict = fleet_capacity.judge_day(demand, self._supply(*units))
        self.assertEqual(verdict["level"], "conflict")

    def test_need_at_or_above_reads_the_next_booked_tier(self):
        need = fleet_capacity.need_at_or_above({"cumulative": {"Van(14 Pax)": 2, "towncar": 5}})
        # nothing booked at 'suv', so the need there is the Sprinter figure
        self.assertEqual(need["suv"], 2)
        self.assertEqual(need["towncar"], 5)
        self.assertEqual(need["Van(14 Pax)"], 2)


class CheckWindowTests(_FleetFixture):
    """Against real legs: the hypothetical unit is removed from supply."""

    def test_taking_the_only_sprinter_down_on_a_sprinter_day_is_short(self):
        sprinter = self.unit("11", self.sprinter)
        self.unit("1")
        self.leg(DAY, 9, vtype=self.sprinter)
        units = fleet_capacity.fleet_units()
        result = fleet_capacity.check_window(sprinter, DAY, DAY + timedelta(days=1), units,
                                             today=TODAY, use_cache=False)
        self.assertEqual(result["level"], "conflict")
        self.assertIn("Short on", result["summary"])
        self.assertTrue(result["days"][0]["down"][0]["hypothetical"])

    def test_a_spare_car_makes_it_clear(self):
        self.unit("11", self.sprinter)
        self.unit("12", self.sprinter)
        spare = self.unit("1")
        self.leg(DAY, 9, vtype=self.sprinter)
        units = fleet_capacity.fleet_units()
        result = fleet_capacity.check_window(spare, DAY, DAY + timedelta(days=1), units,
                                             today=TODAY, use_cache=False)
        self.assertEqual(result["level"], "clear")

    def test_an_existing_downtime_counts_against_supply(self):
        a, b = self.unit("1"), self.unit("2")
        VehicleDowntime.objects.create(vehicle=a, starts_on=DAY, expected_back_on=DAY + timedelta(days=1), reason="x")
        self.leg(DAY, 9)
        units = fleet_capacity.fleet_units()
        result = fleet_capacity.check_window(b, DAY, DAY + timedelta(days=1), units,
                                             today=TODAY, use_cache=False)
        self.assertEqual(result["level"], "conflict")

    def test_editing_ignores_the_rows_old_self(self):
        a = self.unit("1")
        self.unit("2")
        d = VehicleDowntime.objects.create(vehicle=a, starts_on=DAY, expected_back_on=DAY + timedelta(days=1), reason="x")
        self.leg(DAY, 9)
        units = fleet_capacity.fleet_units()
        result = fleet_capacity.check_window(a, DAY, DAY + timedelta(days=1), units, today=TODAY,
                                             use_cache=False, ignore_downtime_id=d.id)
        # one leg, two cars, one hypothetically down -> exactly one left: tight, not short
        self.assertEqual(result["level"], "tight")

    def test_suggest_windows_prefers_clear_then_quiet_then_soon(self):
        self.unit("1")
        target = self.unit("2")
        busy_day = TODAY + timedelta(days=2)
        for hour in (8, 9, 10):
            self.leg(busy_day, hour)
        units = fleet_capacity.fleet_units()
        suggestions = fleet_capacity.suggest_windows(target, 1, units, today=TODAY,
                                                     horizon_days=5, use_cache=False)
        self.assertTrue(suggestions)
        self.assertNotEqual(suggestions[0]["starts_on"], busy_day)
        self.assertEqual(suggestions[0]["level"], "clear")
        self.assertGreaterEqual(suggestions[0]["starts_on"], TODAY + timedelta(days=1))


class TypicalUseTests(_FleetFixture):
    def test_median_units_per_weekday_from_assignment_history(self):
        a, b, c = self.unit("1"), self.unit("2"), self.unit("3")
        sam = Driver.objects.create(
            profile=User.objects.create_user("fd_sam", first_name="Sam"), driver_type="inhouse")
        lee = Driver.objects.create(
            profile=User.objects.create_user("fd_lee", first_name="Lee"), driver_type="inhouse")
        # Three past Mondays with 3, 2, 3 units used; other weekdays have too few samples.
        monday = TODAY - timedelta(days=TODAY.weekday() + 7)
        for weeks_back, used in ((0, 3), (1, 2), (2, 3)):
            day = monday - timedelta(days=7 * weeks_back)
            drivers = [self.george, sam, lee][:used]
            for driver, unit in zip(drivers, (a, b, c)):
                DriverVehicleAssignment.objects.create(driver=driver, date=day, vehicle=unit)
        typical = fleet_capacity.typical_units_by_weekday(TODAY, use_cache=False)
        self.assertEqual(typical.get(0), 3)
        self.assertNotIn(1, typical)


# ════════════════════════════════════════════════════════════════════════════
# fleet_attention — the list
# ════════════════════════════════════════════════════════════════════════════

class AttentionTests(_FleetFixture):
    def _build(self, **kw):
        now = timezone.now()
        args = dict(
            today=TODAY, now=now, vehicles=list(FleetVehicle.objects.filter(is_active=True)),
            downtimes_open=[], issues_open=[], faults_open=[], faults_recent=[],
            schedules=[], feed={"level": "ok"}, assigned_today={},
        )
        args.update(kw)
        for key in ("downtimes_open", "issues_open", "faults_open", "schedules"):
            for row in args[key]:
                row.vehicle = FleetVehicle.objects.get(pk=row.vehicle_id)
        return fleet_attention.build_attention(**args)

    def test_an_overdue_return_is_critical_and_first(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=4), expected_back_on=TODAY - timedelta(days=1),
            reason="Transmission")
        att = self._build(downtimes_open=[d])
        self.assertEqual(att["now"][0]["kind"], "downtime_overdue")
        self.assertEqual(att["now"][0]["level"], "critical")
        self.assertIn("#7", att["now"][0]["title"])

    def test_dispatch_using_a_down_car_is_flagged(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY - timedelta(days=1), reason="Brakes")
        att = self._build(downtimes_open=[d], assigned_today={v.id: "George"})
        kinds = [it["kind"] for it in att["now"]]
        self.assertIn("downtime_in_use", kinds)

    def test_a_car_down_with_no_date_gets_nagged_after_three_days(self):
        v = self.unit("7")
        fresh = VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY - timedelta(days=1), reason="x")
        self.assertEqual([it["kind"] for it in self._build(downtimes_open=[fresh])["now"]], [])
        old = VehicleDowntime.objects.create(vehicle=self.unit("8"), starts_on=TODAY - timedelta(days=5), reason="y")
        att = self._build(downtimes_open=[old])
        self.assertIn("downtime_no_eta", [it["kind"] for it in att["now"]])

    def test_returning_soon_is_information_not_alarm(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=1), expected_back_on=TODAY + timedelta(days=2), reason="x")
        att = self._build(downtimes_open=[d])
        self.assertEqual(att["now"], [])
        self.assertEqual(att["week"][0]["kind"], "downtime_returning")
        self.assertEqual(att["week"][0]["level"], "info")

    def test_planned_downtime_that_now_conflicts_is_raised(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY + timedelta(days=3), expected_back_on=TODAY + timedelta(days=4), reason="tyres")
        att = self._build(downtimes_open=[d], downtime_verdicts={d.id: "conflict"})
        self.assertEqual(att["week"][0]["kind"], "downtime_conflict")
        self.assertEqual(att["week"][0]["level"], "warn")

    def test_issue_severity_maps_to_urgency_and_stale_issues_escalate(self):
        v = self.unit("7")
        ground = VehicleIssue.objects.create(vehicle=v, title="brakes", severity="ground")
        watch = VehicleIssue.objects.create(vehicle=v, title="rattle", severity="watch")
        att = self._build(issues_open=[ground, watch])
        self.assertEqual(att["now"][0]["level"], "critical")
        self.assertIn("brakes", att["now"][0]["title"])
        self.assertEqual(att["later"][0]["level"], "info")
        VehicleIssue.objects.filter(pk=watch.pk).update(reported_at=timezone.now() - timedelta(days=10))
        watch.refresh_from_db()
        att = self._build(issues_open=[ground, watch])
        self.assertIn("still open", [it["detail"] for it in att["now"] if "rattle" in it["title"]][0])

    def test_faults_are_one_line_per_car_with_the_codes(self):
        v = self.unit("7")
        now = timezone.now()
        f1 = VehicleFault.objects.create(vehicle=v, external_id="obdii:P0420", code="P0420",
                                         description="Catalyst", first_seen_at=now, last_seen_at=now)
        f2 = VehicleFault.objects.create(vehicle=v, external_id="obdii:P0301", code="P0301",
                                         first_seen_at=now, last_seen_at=now)
        att = self._build(faults_open=[f1, f2], faults_recent=[f1, f2])
        faults = [it for it in att["now"] if it["kind"] == "fault"]
        self.assertEqual(len(faults), 1)
        self.assertIn("2 fault codes", faults[0]["title"])
        self.assertIn("P0420", faults[0]["detail"])
        self.assertEqual(faults[0]["level"], "critical")

    def test_a_recurring_code_is_named_as_such(self):
        v = self.unit("7")
        now = timezone.now()
        history = [
            VehicleFault.objects.create(vehicle=v, external_id="obdii:P0420", code="P0420",
                                        first_seen_at=now - timedelta(days=d), last_seen_at=now - timedelta(days=d),
                                        resolved_at=None if d == 0 else now - timedelta(days=d - 1))
            for d in (0, 5, 12)
        ]
        att = self._build(faults_open=[history[0]], faults_recent=history)
        fault = [it for it in att["now"] if it["kind"] == "fault"][0]
        self.assertIn("recurring", fault["title"])

    def test_one_service_line_per_car_the_most_urgent(self):
        v = self.unit("7", samsara_vehicle_id="s1", samsara_odometer_meters=Decimal("160934000"))  # 100,000 mi
        oil = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="oil", interval_miles=5000, last_done_odometer_miles=Decimal("94000"))
        tires = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="tires", interval_miles=7500, last_done_odometer_miles=Decimal("93000"))
        att = self._build(schedules=[oil, tires])
        service = [it for it in att["now"] + att["week"] if it["kind"] == "service"]
        self.assertEqual(len(service), 1)
        self.assertEqual(service[0]["level"], "critical")
        self.assertIn("overdue", service[0]["title"])

    def test_projected_service_dates_land_in_coming_up(self):
        v = self.unit("7", samsara_vehicle_id="s1", samsara_odometer_meters=Decimal("160934000"))
        oil = VehicleServiceSchedule.objects.create(
            vehicle=v, service_type="oil", interval_miles=5000, last_done_odometer_miles=Decimal("97000"))
        att = self._build(schedules=[oil], projected_dates={oil.id: TODAY + timedelta(days=9)})
        later = [it for it in att["later"] if it["kind"] == "service_projected"]
        self.assertEqual(len(later), 1)
        self.assertIn("≈", later[0]["title"])

    def test_paperwork_is_one_line_per_car_naming_each_document(self):
        v = self.unit("7", registration_expires_on=TODAY - timedelta(days=2),
                      insurance_expires_on=TODAY + timedelta(days=10),
                      permit_mco=True, permit_mco_expires_on=TODAY - timedelta(days=1))
        att = self._build()
        paper = [it for it in att["now"] if it["kind"] == "paperwork"]
        self.assertEqual(len(paper), 1)
        self.assertEqual(paper[0]["level"], "critical")
        self.assertIn("Registration expired", paper[0]["title"])
        self.assertIn("Insurance due", paper[0]["title"])
        self.assertIn("MCO permit expired", paper[0]["title"])

    def test_fuel_is_never_on_the_fleet_list(self):
        self.unit("7", samsara_vehicle_id="s1", samsara_fuel_percent=5,
                  samsara_last_seen_at=timezone.now())
        att = self._build()
        self.assertFalse(any("Fuel" in it["title"] for group in ("now", "week", "later") for it in att[group]))

    def test_many_quiet_gateways_collapse_to_one_line(self):
        stale = timezone.now() - timedelta(hours=30)
        for n in range(5):
            self.unit(str(n), samsara_vehicle_id=f"s{n}", samsara_last_seen_at=stale)
        att = self._build()
        gps = [it for it in att["now"] if it["kind"] == "gps_stale"]
        self.assertEqual(len(gps), 1)
        self.assertIn("5 gateways", gps[0]["title"])

    def test_setup_items_are_collapsed_and_never_alarms(self):
        self.unit("7")
        self.unit("8")
        att = self._build()
        self.assertEqual(att["now"], [])
        kinds = {it["kind"] for it in att["setup"]}
        self.assertIn("setup_intervals", kinds)
        self.assertIn("setup_paperwork", kinds)
        self.assertTrue(all(it["level"] == "info" for it in att["setup"]))

    def test_feed_down_leads_the_list(self):
        self.unit("7")
        att = self._build(feed={"level": "critical", "label": "Feed down", "detail": "last sync 2d ago"})
        self.assertEqual(att["now"][0]["kind"], "feed")


class StatusBoardTests(_FleetFixture):
    def _rows(self, **kw):
        now = timezone.now()
        args = dict(today=TODAY, now=now, vehicles=list(FleetVehicle.objects.filter(is_active=True)),
                    downtimes_open=[], issues_open=[], faults_open=[], schedules=[], assigned_today={})
        args.update(kw)
        return fleet_attention.status_rows(**args)

    def test_state_precedence_and_ordering(self):
        ready = self.unit("1")
        down = self.unit("2")
        watch = self.unit("3")
        planned = self.unit("4")
        VehicleDowntime.objects.create(vehicle=down, starts_on=TODAY, reason="Transmission")
        VehicleDowntime.objects.create(vehicle=planned, starts_on=TODAY + timedelta(days=2),
                                       expected_back_on=TODAY + timedelta(days=3), reason="Tyres")
        VehicleIssue.objects.create(vehicle=watch, title="AC warm", severity="soon")
        downtimes = list(VehicleDowntime.objects.filter(ended_on__isnull=True))
        issues = list(VehicleIssue.objects.filter(resolved_at__isnull=True))
        for d in downtimes:
            d.vehicle = FleetVehicle.objects.get(pk=d.vehicle_id)
        rows = self._rows(downtimes_open=downtimes, issues_open=issues,
                          assigned_today={ready.id: "George"})
        states = [(r["vehicle"].vehicle_number, r["state"]) for r in rows]
        self.assertEqual(states, [("2", "down"), ("3", "watch"), ("4", "planned"), ("1", "ready")])
        by_num = {r["vehicle"].vehicle_number: r for r in rows}
        self.assertEqual(by_num["1"]["driver"], "George")
        self.assertFalse(by_num["1"]["idle_today"])
        self.assertTrue(by_num["3"]["idle_today"])
        self.assertIn("Transmission", by_num["2"]["headline"])
        self.assertIn("AC warm", by_num["3"]["headline"])


# ════════════════════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════════════════════

class DowntimeEndpointTests(_FleetFixture):
    def test_take_out_of_service_and_dispatch_sees_it(self):
        v = self.unit("7")
        resp = self.post_json("fleet_save_downtime", [v.pk], {
            "category": "repair", "reason": "Transmission, at Bob's", "vendor": "Bob's",
            "starts_on": DAY.isoformat(), "expected_back_on": (DAY + timedelta(days=2)).isoformat(),
        })
        self.assertTrue(resp.json()["success"], resp.content)
        v = FleetVehicle.objects.get(pk=v.pk)
        self.assertTrue(v.is_out_of_service_on(DAY))
        self.assertFalse(v.is_out_of_service_on(DAY + timedelta(days=2)))
        row = VehicleDowntime.objects.get()
        self.assertEqual(row.created_by, self.staff)
        self.assertEqual(row.demand_verdict, "clear")
        # ...and the assignment endpoint refuses it, overridably, exactly as before.
        blocked = self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": self.george.id, "date": DAY.isoformat(), "vehicle_id": v.id},
            content_type="application/json")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("Bob's", blocked.json()["error"])

    def test_a_short_day_asks_before_saving(self):
        only = self.unit("7")
        self.leg(DAY, 9)
        resp = self.post_json("fleet_save_downtime", [only.pk], {
            "reason": "Brakes", "starts_on": DAY.isoformat(),
            "expected_back_on": (DAY + timedelta(days=1)).isoformat()})
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertTrue(body["needs_ack"])
        self.assertEqual(body["level"], "conflict")
        self.assertFalse(VehicleDowntime.objects.exists())
        resp = self.post_json("fleet_save_downtime", [only.pk], {
            "reason": "Brakes", "starts_on": DAY.isoformat(),
            "expected_back_on": (DAY + timedelta(days=1)).isoformat(), "acknowledge": True})
        self.assertTrue(resp.json()["success"])
        self.assertEqual(VehicleDowntime.objects.get().demand_verdict, "conflict")

    def test_validation(self):
        v = self.unit("7")
        no_reason = self.post_json("fleet_save_downtime", [v.pk], {"starts_on": DAY.isoformat()})
        self.assertEqual(no_reason.status_code, 400)
        backwards = self.post_json("fleet_save_downtime", [v.pk], {
            "reason": "x", "starts_on": DAY.isoformat(),
            "expected_back_on": (DAY - timedelta(days=1)).isoformat()})
        self.assertEqual(backwards.status_code, 400)
        self.assertIn("after", backwards.json()["error"])
        bad_category = self.post_json("fleet_save_downtime", [v.pk], {
            "reason": "x", "starts_on": DAY.isoformat(), "category": "vacation"})
        self.assertEqual(bad_category.status_code, 400)

    def test_overlapping_open_windows_are_refused(self):
        v = self.unit("7")
        VehicleDowntime.objects.create(vehicle=v, starts_on=DAY, expected_back_on=DAY + timedelta(days=3), reason="a")
        resp = self.post_json("fleet_save_downtime", [v.pk], {
            "reason": "b", "starts_on": (DAY + timedelta(days=1)).isoformat()})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already", resp.json()["error"])

    def test_edit_moves_the_window(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(vehicle=v, starts_on=DAY, expected_back_on=DAY + timedelta(days=1), reason="a")
        resp = self.post_json("fleet_update_downtime", [d.pk], {
            "reason": "a, waiting on a part", "expected_back_on": (DAY + timedelta(days=4)).isoformat()})
        self.assertTrue(resp.json()["success"], resp.content)
        d.refresh_from_db()
        self.assertEqual(d.expected_back_on, DAY + timedelta(days=4))
        self.assertEqual(d.reason, "a, waiting on a part")

    def test_close_records_the_real_return_and_resolves_the_issue(self):
        v = self.unit("7")
        issue = VehicleIssue.objects.create(vehicle=v, title="Brakes grinding", severity="ground")
        d = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=3), expected_back_on=TODAY + timedelta(days=2),
            reason="Brakes", issue=issue)
        resp = self.post_json("fleet_close_downtime", [d.pk], {
            "ended_on": TODAY.isoformat(), "notes": "Rotors and pads", "resolve_issue": True})
        body = resp.json()
        self.assertTrue(body["success"], resp.content)
        self.assertTrue(body["resolved_issue"])
        d.refresh_from_db()
        issue.refresh_from_db()
        self.assertEqual(d.ended_on, TODAY)
        self.assertEqual(d.closed_by, self.staff)
        self.assertEqual(d.days_down(TODAY), 3)
        self.assertIsNotNone(issue.resolved_at)
        self.assertIn("Rotors", issue.resolution)
        v = FleetVehicle.objects.get(pk=v.pk)
        self.assertFalse(v.is_out_of_service_on(TODAY))

    def test_close_refuses_a_future_date_and_a_date_before_the_start(self):
        v = self.unit("7")
        d = VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY - timedelta(days=2), reason="x")
        self.assertEqual(self.post_json("fleet_close_downtime", [d.pk], {
            "ended_on": (TODAY + timedelta(days=5)).isoformat()}).status_code, 400)
        self.assertEqual(self.post_json("fleet_close_downtime", [d.pk], {
            "ended_on": (TODAY - timedelta(days=9)).isoformat()}).status_code, 400)

    def test_delete_only_before_it_has_cost_a_day(self):
        v = self.unit("7")
        planned = VehicleDowntime.objects.create(vehicle=v, starts_on=DAY, reason="x")
        old = VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY - timedelta(days=2), reason="y")
        self.assertTrue(self.post_json("fleet_delete_downtime", [planned.pk], {}).json()["success"])
        self.assertEqual(self.post_json("fleet_delete_downtime", [old.pk], {}).status_code, 400)
        self.assertTrue(VehicleDowntime.objects.filter(pk=old.pk).exists())

    def test_check_window_json(self):
        v = self.unit("7")
        self.unit("8")
        resp = self.client.get(reverse("fleet_check_window", args=[v.pk]) + f"?from={DAY.isoformat()}")
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["level"], "clear")
        self.assertEqual(len(body["days"]), fleet_capacity.OPEN_ENDED_CHECK_DAYS)
        bad = self.client.get(reverse("fleet_check_window", args=[v.pk]) + "?from=nope")
        self.assertEqual(bad.status_code, 400)


class IssueEndpointTests(_FleetFixture):
    def test_report_then_resolve(self):
        v = self.unit("7")
        resp = self.post_json("fleet_report_issue", [v.pk], {
            "title": "AC blowing warm", "severity": "soon", "source": "driver", "details": "since Tuesday"})
        body = resp.json()
        self.assertTrue(body["success"], resp.content)
        self.assertEqual(body["notified"], 0)  # alerts are inert under TESTING
        issue = VehicleIssue.objects.get()
        self.assertEqual(issue.reported_by, self.staff)
        self.assertEqual(issue.source, "driver")
        resp = self.post_json("fleet_resolve_issue", [issue.pk], {"resolution": "Recharged, leak test ok"})
        self.assertTrue(resp.json()["success"])
        issue.refresh_from_db()
        self.assertEqual(issue.resolved_by, self.staff)
        self.assertEqual(self.post_json("fleet_resolve_issue", [issue.pk], {"resolution": "again"}).status_code, 400)

    def test_report_and_take_down_in_one_click(self):
        v = self.unit("7")
        resp = self.post_json("fleet_report_issue", [v.pk], {
            "title": "Brakes grinding", "severity": "ground", "take_down": True,
            "expected_back_on": (TODAY + timedelta(days=2)).isoformat()})
        body = resp.json()
        self.assertTrue(body["success"], resp.content)
        d = VehicleDowntime.objects.get(pk=body["downtime_id"])
        self.assertEqual(d.starts_on, TODAY)
        self.assertEqual(d.issue.title, "Brakes grinding")
        v = FleetVehicle.objects.get(pk=v.pk)
        self.assertTrue(v.is_out_of_service_on(TODAY))
        self.assertEqual(v.out_of_service_label(TODAY).split(" — ")[0], "Brakes grinding")

    def test_a_resolution_is_required(self):
        v = self.unit("7")
        issue = VehicleIssue.objects.create(vehicle=v, title="x")
        self.assertEqual(self.post_json("fleet_resolve_issue", [issue.pk], {}).status_code, 400)

    def test_an_empty_title_is_refused(self):
        v = self.unit("7")
        self.assertEqual(self.post_json("fleet_report_issue", [v.pk], {"title": "  "}).status_code, 400)


class StandardIntervalTests(_FleetFixture):
    def test_per_vehicle_adds_only_what_is_missing(self):
        v = self.unit("7", samsara_vehicle_id="s1", samsara_odometer_meters=Decimal("160934000"))
        VehicleServiceSchedule.objects.create(vehicle=v, service_type="oil", interval_miles=7500)
        resp = self.post_json("fleet_apply_standard_intervals", [v.pk], {})
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertNotIn("oil", body["created"])
        self.assertIn("tires", body["created"])
        oil = VehicleServiceSchedule.objects.get(vehicle=v, service_type="oil")
        self.assertEqual(oil.interval_miles, 7500)  # untouched
        tires = VehicleServiceSchedule.objects.get(vehicle=v, service_type="tires")
        # NO BASELINE. Stamping today's date invents a service history: press the
        # button fleet-wide and all nineteen cars come due in the same fortnight
        # on dates nobody serviced anything on.
        self.assertIsNone(tires.last_done_on)
        self.assertIsNone(tires.last_done_odometer_miles)

    def test_a_seeded_interval_is_inert_until_it_has_a_baseline(self):
        """An interval with no baseline must produce no due date at all —
        "an invented due date is worse than none, because someone will plan a
        shop day around it" (fleet_health.service_findings)."""
        from dispatching import fleet_health
        v = self.unit("7", samsara_vehicle_id="s1", samsara_odometer_meters=Decimal("160934000"))
        self.post_json("fleet_apply_standard_intervals", [v.pk], {})
        for schedule in VehicleServiceSchedule.objects.filter(vehicle=v):
            self.assertEqual(
                fleet_health.service_findings(schedule, v.odometer_miles, TODAY), [],
                schedule.service_type)

    def test_a_diesel_sprinter_gets_a_longer_oil_interval(self):
        """One table for the whole fleet put a diesel Sprinter on a Suburban's
        5,000-mile oil interval — at 190-350 miles a day that is a shop visit a
        fortnight the van never needed."""
        suv = self.unit("7")
        sprinter = self.unit("8", vtype=self.sprinter)
        for unit in (suv, sprinter):
            self.post_json("fleet_apply_standard_intervals", [unit.pk], {})
        self.assertEqual(
            VehicleServiceSchedule.objects.get(vehicle=suv, service_type="oil").interval_miles, 5_000)
        self.assertEqual(
            VehicleServiceSchedule.objects.get(vehicle=sprinter, service_type="oil").interval_miles, 10_000)

    def test_the_desk_does_not_go_quiet_once_intervals_exist(self):
        """Removing the fabricated baseline must not make the maintenance half
        of the desk silent — silence and health look identical."""
        v = self.unit("7")
        desk = fleet_desk.load_desk(today=TODAY, use_cache=False)
        self.assertIsNotNone(desk["setup_intervals"])
        self.assertIsNone(desk["setup_baselines"])

        self.post_json("fleet_apply_standard_intervals", [v.pk], {})
        desk = fleet_desk.load_desk(today=TODAY, use_cache=False)
        self.assertIsNone(desk["setup_intervals"])
        self.assertIsNotNone(desk["setup_baselines"])
        self.assertIn("no baseline", desk["setup_baselines"]["title"])

    def test_fleet_wide(self):
        self.unit("7")
        self.unit("8")
        self.unit("9", is_active=False)
        resp = self.post_json("fleet_apply_standard_intervals_all", [], {})
        self.assertEqual(resp.json()["vehicles"], 2)
        self.assertEqual(VehicleServiceSchedule.objects.count(), 2 * len(
            __import__("dispatching.fleet_views", fromlist=["STANDARD_INTERVALS"]).STANDARD_INTERVALS))


# ════════════════════════════════════════════════════════════════════════════
# Pages
# ════════════════════════════════════════════════════════════════════════════

class PageTests(_FleetFixture):
    def test_desk_renders_with_a_mixed_fleet(self):
        ready = self.unit("1")
        down = self.unit("2")
        VehicleDowntime.objects.create(vehicle=down, starts_on=TODAY, expected_back_on=TODAY + timedelta(days=1),
                                       reason="Transmission, at Bob's", vendor="Bob's")
        VehicleIssue.objects.create(vehicle=ready, title="AC warm", severity="soon")
        self.leg(TODAY + timedelta(days=1), 9)
        resp = self.client.get(reverse("fleet_desk"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Transmission, at Bob")
        self.assertContains(resp, "AC warm")
        self.assertContains(resp, "Do now")
        self.assertEqual(resp.context["summary"]["down"], 1)
        self.assertEqual(resp.context["summary"]["watch"], 1)

    def test_desk_is_calm_when_nothing_is_wrong(self):
        from dispatching.fleet_sync import FEED_VEHICLE_STATS, record_feed_result
        record_feed_result(FEED_VEHICLE_STATS, "success")  # a live feed, or the feed line fires
        self.unit("1")
        resp = self.client.get(reverse("fleet_desk"))
        self.assertContains(resp, "Nothing is wrong today")

    def test_a_dead_feed_is_the_first_thing_on_the_desk(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_desk"))
        self.assertEqual(resp.context["attention"]["now"][0]["kind"], "feed")

    def test_outlook_renders_for_a_unit(self):
        v = self.unit("1")
        self.unit("2")
        self.leg(DAY, 9)
        resp = self.client.get(reverse("fleet_outlook") + f"?unit={v.id}&hours=4")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["unit_id"], v.id)
        self.assertContains(resp, "#1 · 2024 Chevrolet Suburban")

    def test_report_renders(self):
        v = self.unit("1")
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY - timedelta(days=5),
                                       expected_back_on=TODAY - timedelta(days=3),
                                       ended_on=TODAY - timedelta(days=2), reason="x", category="tires")
        resp = self.client.get(reverse("fleet_report") + "?days=30")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fleet available")
        self.assertEqual(resp.context["report"]["total_down_days"], 3)

    def test_vehicle_page_shows_the_ledger_and_the_forms(self):
        v = self.unit("1")
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY, reason="Transmission")
        resp = self.client.get(reverse("fleet_detail", args=[v.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Transmission")
        self.assertContains(resp, "Take out of service")
        self.assertContains(resp, "Report a problem")
        self.assertNotContains(resp, "f_out_of_service_from")

    def test_vehicles_table_still_flags_a_down_unit(self):
        v = self.unit("1")
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY, reason="Transmission")
        resp = self.client.get(reverse("fleet_list"))
        self.assertContains(resp, "Out of service")
        self.assertContains(resp, "Transmission")

    def test_non_staff_cannot_open_the_desk(self):
        self.client.force_login(User.objects.create_user("fd_grunt", password="x"))
        resp = self.client.get(reverse("fleet_desk"))
        self.assertIn(resp.status_code, (302, 403))

    def test_logged_out_lands_on_the_real_login_page(self):
        """Django's default /accounts/login/ is a 404 on this site. A fleet
        manager whose session expired must land on the login page, not a 404,
        from every fleet URL — the desk is their home page."""
        self.client.logout()
        for name, args in (("fleet_desk", []), ("fleet_list", []), ("fleet_outlook", []),
                           ("fleet_report", []), ("fleet_detail", [self.unit("1").pk])):
            resp = self.client.get(reverse(name, args=args))
            self.assertEqual(resp.status_code, 302, name)
            self.assertTrue(resp.url.startswith(reverse("login")), (name, resp.url))
        resp = self.client.post(reverse("fleet_save_downtime", args=[FleetVehicle.objects.get().pk]),
                                data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(reverse("login")))


class FleetManagerExperienceTests(_FleetFixture):
    def _fleet_user(self, superuser=False):
        user = User.objects.create_user("fd_fleet", password="x", is_staff=True, is_superuser=superuser)
        UserProfile.objects.create(user=user, phone_number="+14075550100", is_fleet_manager=True)
        return user

    def test_fleet_manager_lands_on_the_desk(self):
        self.client.force_login(self._fleet_user())
        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, reverse("fleet_desk"), fetch_redirect_response=False)

    def test_a_founder_flagged_as_fleet_manager_keeps_the_dashboard(self):
        self.client.force_login(self._fleet_user(superuser=True))
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)

    def test_fleet_manager_sees_the_fleet_bar_not_the_dispatch_bar(self):
        self.unit("1")
        self.client.force_login(self._fleet_user())
        resp = self.client.get(reverse("fleet_desk"))
        self.assertContains(resp, "Outlook")
        self.assertNotContains(resp, "Swap Tester")
        self.assertNotContains(resp, "Reservations</a>")

    def test_dispatchers_get_a_fleet_link(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_desk"))
        self.assertContains(resp, "Swap Tester")
        self.assertContains(resp, ">Fleet")

    def test_login_redirect(self):
        self._fleet_user()
        resp = self.client.post(reverse("login"), {"username": "fd_fleet", "password": "x"})
        self.assertRedirects(resp, reverse("fleet_desk"), fetch_redirect_response=False)


class DispatchSurfaceTests(_FleetFixture):
    def test_planner_shows_the_unconfirmed_notice_without_blocking(self):
        v = self.unit("7")
        VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=4), expected_back_on=TODAY - timedelta(days=1),
            reason="Transmission")
        self.leg(TODAY, 9)
        resp = self.client.get(reverse("capacity_planner") + f"?date={TODAY.isoformat()}")
        self.assertEqual(resp.context["out_of_service_count"], 0)
        self.assertContains(resp, "not confirmed")
        ok = self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": self.george.id, "date": TODAY.isoformat(), "vehicle_id": v.id},
            content_type="application/json")
        self.assertTrue(ok.json()["success"])

    def test_planner_links_to_the_fleet_desk(self):
        self.unit("7")
        self.leg(TODAY, 9)
        resp = self.client.get(reverse("capacity_planner") + f"?date={TODAY.isoformat()}")
        self.assertContains(resp, reverse("fleet_desk"))


# ════════════════════════════════════════════════════════════════════════════
# Fault episodes from Samsara
# ════════════════════════════════════════════════════════════════════════════

class ExtractFaultCodesTests(TestCase):
    def test_obdii_ecu_nesting(self):
        codes, answered = extract_fault_codes({
            "time": "2026-09-14T12:00:00Z",
            "obdii": {"diagnosticTroubleCodes": [
                {"confirmedDtcs": [{"dtcShortCode": "P0301", "dtcDescription": "Cyl 1 misfire"}],
                 "pendingDtcs": [{"dtcShortCode": "P0128"}], "permanentDtcs": [{"dtcShortCode": "P0420"}],
                 "milStatus": True},
                {"confirmedDtcs": [], "pendingDtcs": [], "permanentDtcs": [], "milStatus": False},
            ]}})
        self.assertTrue(answered)
        self.assertEqual([c["code"] for c in codes], ["P0301", "P0420"])
        self.assertEqual(codes[0]["description"], "Cyl 1 misfire")
        self.assertEqual(codes[0]["severity"], "critical")  # lamp lit on that ECU
        self.assertEqual(codes[0]["external_id"], "obdii:P0301")

    def test_lamp_with_no_readable_code(self):
        codes, answered = extract_fault_codes({"obdii": {"diagnosticTroubleCodes": [
            {"confirmedDtcs": [], "pendingDtcs": [], "permanentDtcs": [], "milStatus": True}]}})
        self.assertTrue(answered)
        self.assertEqual(codes[0]["code"], "MIL")

    def test_j1939_entries_are_faults(self):
        codes, answered = extract_fault_codes({"j1939": {"diagnosticTroubleCodes": [
            {"spnId": 100, "fmiId": 1, "spnDescription": "Engine oil pressure", "fmiDescription": "low"}]}})
        self.assertTrue(answered)
        self.assertEqual(codes[0]["code"], "SPN 100 FMI 1")
        self.assertIn("oil pressure", codes[0]["description"])

    def test_absent_is_not_an_answer(self):
        self.assertEqual(extract_fault_codes(None), ([], False))
        self.assertEqual(extract_fault_codes({"time": "2026-09-14T12:00:00Z"}), ([], False))

    def test_clean_car_is_an_empty_answer(self):
        codes, answered = extract_fault_codes({"obdii": {"diagnosticTroubleCodes": []}})
        self.assertTrue(answered)
        self.assertEqual(codes, [])

    def test_duplicates_collapse(self):
        codes, _ = extract_fault_codes({"obdii": {"diagnosticTroubleCodes": [
            {"confirmedDtcs": [{"dtcShortCode": "P0420"}], "permanentDtcs": [{"dtcShortCode": "P0420"}]}]}})
        self.assertEqual(len(codes), 1)


class FaultEpisodeTests(_FleetFixture):
    def test_open_refresh_resolve(self):
        v = self.unit("7", samsara_vehicle_id="s1")
        p0420 = {"external_id": "obdii:P0420", "code": "P0420", "description": "Catalyst", "severity": "warning"}
        p0301 = {"external_id": "obdii:P0301", "code": "P0301", "description": "", "severity": "warning"}
        out = upsert_fault_episodes(v, [p0420, p0301])
        self.assertEqual((out["opened"], out["refreshed"], out["resolved"]), (2, 0, 0))
        out = upsert_fault_episodes(v, [p0420])
        self.assertEqual((out["opened"], out["refreshed"], out["resolved"]), (0, 1, 1))
        open_rows = VehicleFault.objects.filter(vehicle=v, resolved_at__isnull=True)
        self.assertEqual([f.code for f in open_rows], ["P0420"])
        self.assertEqual(open_rows[0].occurrence_count, 2)
        self.assertIsNotNone(VehicleFault.objects.get(code="P0301").resolved_at)
        # the same code coming back later opens a NEW episode, not a collision
        upsert_fault_episodes(v, [p0420, p0301])
        self.assertEqual(VehicleFault.objects.filter(code="P0301").count(), 2)

    def test_the_poller_never_resolves_on_an_absent_answer(self):
        from dispatching.samsara_scheduler import _sync_fault_episodes

        v = self.unit("7", samsara_vehicle_id="s1")
        upsert_fault_episodes(v, [{"external_id": "obdii:P0420", "code": "P0420", "description": "", "severity": "warning"}])
        _sync_fault_episodes(v, None, None)                                   # type absent
        _sync_fault_episodes(v, {"time": "2026-09-14T12:00:00Z"}, None)      # no bus answered
        self.assertEqual(VehicleFault.objects.filter(resolved_at__isnull=True).count(), 1)
        _sync_fault_episodes(v, {"obdii": {"diagnosticTroubleCodes": []}}, None)  # a real "all clear"
        self.assertEqual(VehicleFault.objects.filter(resolved_at__isnull=True).count(), 0)

    def test_sync_vehicles_populates_episodes(self):
        from dispatching.samsara_scheduler import sync_vehicles
        from dispatching.samsara_service import SamsaraService

        v = self.unit("7", samsara_vehicle_id="veh-1")
        payload = {"status": "success", "data": [{
            "id": "veh-1",
            "gps": {"latitude": 28.4, "longitude": -81.3, "time": "2026-09-14T12:00:00Z",
                    "speedMilesPerHour": 0, "reverseGeo": {"formattedLocation": "near MCO"}},
            "faultCodes": {"time": "2026-09-14T12:00:00Z", "obdii": {"diagnosticTroubleCodes": [
                {"confirmedDtcs": [{"dtcShortCode": "P0420", "dtcDescription": "Catalyst"}],
                 "pendingDtcs": [], "permanentDtcs": [], "milStatus": False}]}},
        }]}
        with patch.object(SamsaraService, "is_configured", return_value=True), \
             patch.object(SamsaraService, "get_vehicle_stats", return_value=payload):
            sync_vehicles()
        fault = VehicleFault.objects.get(vehicle=v)
        self.assertEqual(fault.code, "P0420")
        self.assertEqual(fault.description, "Catalyst")
        v.refresh_from_db()
        self.assertEqual(v.samsara_open_fault_count, 1)


# ════════════════════════════════════════════════════════════════════════════
# Alerts
# ════════════════════════════════════════════════════════════════════════════

class NotifyTests(_FleetFixture):
    def test_recipients_are_flagged_profiles_plus_settings(self):
        fm = User.objects.create_user("fd_fm", password="x", is_staff=True)
        UserProfile.objects.create(user=fm, phone_number="407-555-0100", is_fleet_manager=True)
        other = User.objects.create_user("fd_other", password="x", is_staff=True)
        UserProfile.objects.create(user=other, phone_number="407-555-0199", is_fleet_manager=False)
        with override_settings(FLEET_NOTIFY_PHONES=["+14075550100", "+14075550111"]):
            self.assertEqual(fleet_notify.recipients(), ["+14075550100", "+14075550111"])

    def test_nothing_sends_under_testing(self):
        self.assertFalse(fleet_notify.alerts_enabled())
        v = self.unit("7")
        issue = VehicleIssue.objects.create(vehicle=v, title="x", severity="soon")
        with patch("drivers.sms.send") as send:
            out = fleet_notify.notify_issue_reported(issue)
        send.assert_not_called()
        self.assertEqual(out["skipped"], "disabled")

    def test_digest_is_silent_on_a_quiet_day_and_short_on_a_bad_one(self):
        self.assertEqual(fleet_notify.digest_text({"now": [], "week": [], "counts": {}}, TODAY), "")
        attention = {"now": [{"title": f"#{n} thing"} for n in range(6)], "week": [1, 2], "counts": {}}
        text = fleet_notify.digest_text(attention, TODAY)
        self.assertIn("6 to look at now", text)
        self.assertIn("+2 more", text)
        self.assertLessEqual(len(text), 600)

    def test_issue_text_names_the_car_and_the_reporter(self):
        v = self.unit("7")
        issue = VehicleIssue.objects.create(vehicle=v, title="Brakes grinding", severity="ground",
                                            reported_by=self.staff)
        text = fleet_notify.issue_text(issue)
        self.assertIn("#7", text)
        self.assertIn("Brakes grinding", text)
        self.assertIn("do not drive", text)

    def test_digest_gate_is_free_when_alerts_are_off(self):
        from dispatching.fleet_sync import should_send_digest
        with self.assertNumQueries(0):
            self.assertFalse(should_send_digest())


# ════════════════════════════════════════════════════════════════════════════
# Report
# ════════════════════════════════════════════════════════════════════════════

class ReportTests(_FleetFixture):
    def test_availability_and_categories(self):
        a, b = self.unit("1"), self.unit("2")
        start, end = TODAY - timedelta(days=9), TODAY
        VehicleDowntime.objects.create(vehicle=a, starts_on=start + timedelta(days=1),
                                       expected_back_on=start + timedelta(days=3),
                                       ended_on=start + timedelta(days=3), reason="x", category="tires",
                                       demand_verdict="clear")
        VehicleDowntime.objects.create(vehicle=b, starts_on=TODAY - timedelta(days=1), reason="y")  # open
        report = fleet_report.build_report(start, end, TODAY)
        self.assertEqual(report["total_unit_days"], 20)
        self.assertEqual(report["total_down_days"], 2 + 2)
        self.assertEqual(report["availability_pct"], 80.0)
        cats = {c["category"]: c["days"] for c in report["down_by_category"]}
        self.assertEqual(cats, {"tires": 2, "repair": 2})
        self.assertEqual(report["placement"]["clear"], 1)
        self.assertEqual(report["most_down"][0]["vehicle"].vehicle_number, "1")

    def test_pm_adherence_compares_each_service_to_the_one_before(self):
        v = self.unit("1")
        VehicleServiceSchedule.objects.create(vehicle=v, service_type="oil", interval_miles=5000)
        VehicleServiceRecord.objects.create(vehicle=v, service_type="oil", performed_on=TODAY - timedelta(days=60),
                                            odometer_miles=Decimal("90000"))
        VehicleServiceRecord.objects.create(vehicle=v, service_type="oil", performed_on=TODAY - timedelta(days=20),
                                            odometer_miles=Decimal("96200"), cost=Decimal("80"))
        VehicleServiceRecord.objects.create(vehicle=v, service_type="oil", performed_on=TODAY - timedelta(days=2),
                                            odometer_miles=Decimal("100000"), cost=Decimal("80"))
        report = fleet_report.build_report(TODAY - timedelta(days=30), TODAY, TODAY)
        self.assertEqual(report["pm"]["late"], 1)      # 96,200 vs due at 95,000
        self.assertEqual(report["pm"]["on_time"], 1)   # 100,000 vs due at 101,200
        self.assertEqual(report["pm"]["median_late_by_miles"], 1200)
        self.assertEqual(report["service_cost_total"], Decimal("160"))

    def test_repeats_and_imbalance(self):
        a, b = self.unit("1"), self.unit("2")
        for _ in range(2):
            VehicleIssue.objects.create(vehicle=a, title="AC blowing warm", severity="soon")
        from drivers.models import VehicleDayReading
        for offset in range(1, 6):
            day = TODAY - timedelta(days=offset)
            VehicleDayReading.objects.create(vehicle=a, date=day, miles_driven=Decimal("300"))
            VehicleDayReading.objects.create(vehicle=b, date=day, miles_driven=Decimal("100"))
        report = fleet_report.build_report(TODAY - timedelta(days=29), TODAY, TODAY)
        self.assertEqual(report["repeats"][0]["what"], "ac blowing warm")
        self.assertEqual(report["repeats"][0]["times"], 2)
        self.assertEqual(report["imbalance"][0]["ratio"], Decimal("3.0"))
        self.assertTrue(report["imbalance"][0]["flag"])


class ReportHonestyTests(_FleetFixture):
    """100% available, no downtime and $0 spend is what a perfect month looks
    like AND what a ledger nobody has filled looks like. Shown to a manager the
    second reads as the first, which made this the most misleading screen in the
    product."""

    def _report(self):
        return fleet_report.build_report(TODAY - timedelta(days=29), TODAY, TODAY)

    def test_an_untouched_ledger_is_not_reported_as_a_perfect_month(self):
        self.unit("1")
        self.unit("2")
        ledger = self._report()["ledger"]
        self.assertFalse(ledger["started"])
        self.assertFalse(ledger["has_downtime"])
        self.assertFalse(ledger["has_service"])
        self.assertTrue(ledger["measured_only"])

    def test_the_page_says_so_rather_than_printing_a_number(self):
        self.unit("1")
        resp = self.client.get(reverse("fleet_report"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "cannot be calculated")
        self.assertNotContains(resp, "100.0%")

    def test_the_flags_are_per_dependency_not_one_switch(self):
        """A reported issue does not make AVAILABILITY meaningful, and an oil
        change logged does not make DOWNTIME meaningful."""
        unit = self.unit("1")
        VehicleIssue.objects.create(vehicle=unit, title="AC warm", severity="soon")
        ledger = self._report()["ledger"]
        self.assertEqual(ledger["issues"], 1)
        self.assertFalse(ledger["has_downtime"])
        self.assertFalse(ledger["has_service"])
        self.assertFalse(ledger["started"])

    def test_one_downtime_unlocks_availability_but_not_spend(self):
        unit = self.unit("1")
        VehicleDowntime.objects.create(
            vehicle=unit, category="repair", reason="Brakes",
            starts_on=TODAY - timedelta(days=3), ended_on=TODAY - timedelta(days=1))
        ledger = self._report()["ledger"]
        self.assertTrue(ledger["has_downtime"])
        self.assertFalse(ledger["has_service"])
        self.assertTrue(ledger["started"])
        resp = self.client.get(reverse("fleet_report"))
        self.assertNotContains(resp, "cannot be calculated")

    def test_the_measured_sections_are_never_suppressed(self):
        """Mileage balance and becoming-unreliable come from the trackers, not
        from paperwork, so they are real on day one and must keep working."""
        self.unit("1")
        resp = self.client.get(reverse("fleet_report"))
        self.assertContains(resp, "Mileage balance")
        self.assertContains(resp, "Becoming unreliable")
