"""The redesigned Fleet desk and outlook: the hour-level capacity model, the
payload both screens read, and the Do-now queue.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_windows

Leg end times are pinned to pickup + 90 minutes here so the squares are
arithmetic, not a drive-time estimate.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.urls import reverse

from dispatching import fleet_desk, fleet_queue, fleet_windows
from dispatching.tests_fleet_desk import DAY, TODAY, _FleetFixture
from drivers.models import (
    DriverVehicleAssignment, VehicleDowntime, VehicleFault, VehicleIssue,
)


def _ninety_minutes(leg, target_date):
    return datetime.combine(target_date, leg.pickup_time) + timedelta(minutes=90)


def _fault(vehicle, code, *, severity="warning", days_ago=0):
    from django.utils import timezone
    seen = timezone.now() - timedelta(days=days_ago)
    return VehicleFault.objects.create(
        vehicle=vehicle, source="obd_fault", external_id=f"{vehicle.pk}-{code}", code=code,
        severity=severity, description=f"{code} description", first_seen_at=seen, last_seen_at=seen,
        occurrence_count=3)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class HourlyNeedTests(_FleetFixture):
    def test_empty_day_is_all_zeros(self):
        h = fleet_windows.hourly_need(DAY, [])
        self.assertEqual(h["overall"], [0] * fleet_windows.SLOTS)
        self.assertEqual(h["tiers"]["suv"], [0] * fleet_windows.SLOTS)
        self.assertEqual(h["peak"], 0)

    def test_a_leg_counts_in_every_square_it_touches(self):
        # 8:30 – 10:00 touches the 8 and 9 squares (10:00 is the end, half-open).
        a = self.leg(DAY, 8)
        a.pickup_time = a.pickup_time.replace(minute=30)
        h = fleet_windows.hourly_need(DAY, [a])
        self.assertEqual(h["tiers"]["suv"][:4], [0, 1, 1, 0])
        # Nested: an SUV job also needs a "town car or bigger" and a
        # "Metris or bigger" unit, but never a Sprinter.
        self.assertEqual(h["tiers"]["towncar"][:4], [0, 1, 1, 0])
        self.assertEqual(h["tiers"]["mini_van"][:4], [0, 1, 1, 0])
        self.assertEqual(h["tiers"]["Van(14 Pax)"][:4], [0, 0, 0, 0])

    def test_overlaps_peak_and_the_peak_square_is_reported(self):
        a = self.leg(DAY, 9)                       # 9:00 – 10:30 SUV
        b = self.leg(DAY, 9)                       # 9:30 – 11:00 SUV
        b.pickup_time = b.pickup_time.replace(minute=30)
        c = self.leg(DAY, 10, vtype=self.sprinter)  # 10:00 – 11:30 Sprinter
        h = fleet_windows.hourly_need(DAY, [a, b, c])
        suv = h["tiers"]["suv"]
        self.assertEqual(suv[2], 2)   # 9–10: a, b
        self.assertEqual(suv[3], 3)   # 10–11: a (to 10:30), b, c
        self.assertEqual(suv[4], 1)   # 11–12: c only (b ends 11:00)
        self.assertEqual(h["tiers"]["Van(14 Pax)"][2:5], [0, 1, 1])
        self.assertEqual(h["peak"], 3)
        self.assertEqual(fleet_windows.slot_of(h["peak_at"], DAY), 3)

    def test_a_square_inherits_the_state_across_its_boundary(self):
        # 8:30 – 10:00: nothing happens at 9:00 exactly, yet the 9 square is busy.
        a = self.leg(DAY, 8)
        a.pickup_time = a.pickup_time.replace(minute=30)
        h = fleet_windows.hourly_need(DAY, [a])
        self.assertEqual(h["overall"][1:3], [1, 1])


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class WindowPayloadTests(_FleetFixture):
    def test_payload_shape_and_the_if_down_verdict(self):
        one, two = self.unit("1"), self.unit("2")
        three = self.unit("3", vtype=self.sprinter)
        for hour in (9, 9, 9):   # three SUV jobs in flight at once on DAY
            self.leg(DAY, hour)
        VehicleDowntime.objects.create(vehicle=two, starts_on=DAY + timedelta(days=1),
                                       expected_back_on=DAY + timedelta(days=2), reason="tyres")
        from dispatching import fleet_capacity
        units = fleet_capacity.fleet_units()
        p = fleet_windows.window_payload(TODAY, 14, units, today=TODAY, use_cache=False)

        self.assertEqual(p["slots"], 10)
        self.assertEqual([u["number"] for u in p["units"]], ["1", "2", "3"])
        unit_two = next(u for u in p["units"] if u["number"] == "2")
        self.assertEqual(unit_two["down"], [(DAY + timedelta(days=1)).isoformat()])
        self.assertEqual(unit_two["booked"]["id"], VehicleDowntime.objects.get().id)
        self.assertIsNone(next(u for u in p["units"] if u["number"] == "1")["booked"])

        day = p["days"][(DAY - TODAY).days]
        self.assertEqual(day["trips"], 3)
        self.assertEqual(day["peak"], 3)
        suv = day["by_tier"]["suv"]
        self.assertEqual(suv["need"][2], 3)
        self.assertEqual(suv["have"], 3)          # two SUVs + a Sprinter can run SUV jobs
        # Every SUV-capable unit is on a trip at 9; one more down means short.
        self.assertEqual(suv["if_down"]["level"], "conflict")
        self.assertTrue(suv["if_down"]["worsened"])
        self.assertEqual(day["baseline"]["level"], "tight")
        # A quiet day: nothing tips, nothing is short.
        quiet = p["days"][1]
        self.assertEqual(quiet["baseline"]["level"], "clear")
        self.assertFalse(quiet["by_tier"]["suv"]["if_down"]["worsened"])
        # A Sprinter is not needed at all that day, so taking one down costs
        # the SUV pool one body — the same conflict.
        self.assertEqual(day["by_tier"]["Van(14 Pax)"]["if_down"]["level"], "conflict")
        # The day #2 is down for: the ledger already applied to the pool.
        after = p["days"][(DAY - TODAY).days + 1]
        self.assertEqual(after["by_tier"]["suv"]["have"], 2)
        self.assertEqual(after["down"], ["#2"])
        self.assertEqual(next(t for t in p["tiers"] if t["key"] == "suv")["owned"], 3)


class QueueTests(_FleetFixture):
    def _desk(self):
        return fleet_desk.load_desk(today=TODAY, use_cache=False)

    def test_one_row_per_unit_with_the_right_tag(self):
        shop = self.unit("4")
        _fault(shop, "P202E"); _fault(shop, "P208E")          # two codes -> needs the shop
        watch = self.unit("10")
        _fault(watch, "P0299")                                 # one warning -> watch
        down = self.unit("12")
        VehicleDowntime.objects.create(vehicle=down, starts_on=TODAY - timedelta(days=2),
                                       expected_back_on=TODAY + timedelta(days=2), reason="Out of service")
        booked = self.unit("7")
        VehicleDowntime.objects.create(vehicle=booked, starts_on=TODAY + timedelta(days=3),
                                       expected_back_on=TODAY + timedelta(days=4), reason="Brakes")
        late = self.unit("8")
        VehicleDowntime.objects.create(vehicle=late, starts_on=TODAY - timedelta(days=5),
                                       expected_back_on=TODAY - timedelta(days=1), reason="Transmission")
        self.unit("1")                                         # ready: no row

        q = self._desk()["queue"]
        by_unit = {r["number"]: r for r in q["rows"]}
        self.assertEqual([r["number"] for r in q["rows"]], ["4", "8", "10", "7", "12"])
        self.assertEqual(by_unit["4"]["tag"], fleet_queue.NEEDS_SHOP)
        self.assertEqual([c["code"] for c in by_unit["4"]["codes"]], ["P202E", "P208E"])
        self.assertEqual(by_unit["4"]["primary"]["kind"], "finder")
        self.assertEqual(by_unit["4"]["secondary"]["kind"], "takeoff")
        self.assertEqual(by_unit["4"]["category"], "repair")
        self.assertIn("P202E", by_unit["4"]["reason"])
        self.assertEqual(by_unit["10"]["tag"], fleet_queue.WATCH)
        self.assertEqual(by_unit["8"]["tag"], fleet_queue.UNCONFIRMED)
        self.assertEqual(by_unit["8"]["primary"]["default_date"], (TODAY - timedelta(days=1)).isoformat())
        self.assertTrue(by_unit["7"]["handled"])
        self.assertEqual(by_unit["7"]["tag"], fleet_queue.BOOKED)
        self.assertEqual(by_unit["7"]["secondary"]["kind"], "cancel")
        self.assertTrue(by_unit["12"]["handled"])
        self.assertEqual(by_unit["12"]["tag"], fleet_queue.DOWN)
        self.assertEqual(by_unit["12"]["primary"]["kind"], "close")
        self.assertEqual(q["summary"]["text"], "3 open · 1 can't wait · 2 handled")

    def test_a_reported_issue_names_who_and_a_booked_problem_car_reads_booked(self):
        v = self.unit("5")
        VehicleIssue.objects.create(vehicle=v, title="brakes grinding", severity="soon",
                                    reported_by=self.staff)
        rows = self._desk()["queue"]["rows"]
        self.assertEqual(rows[0]["tag"], fleet_queue.NEEDS_SHOP)
        self.assertEqual(rows[0]["title"], "#5 — brakes grinding")
        self.assertIn("reported by fd_staff today", rows[0]["meaning"])
        # Nobody has built today's board here, so the queue must NOT claim the car
        # has no chauffeur — before Day Setup runs that is true of every unit, and
        # saying it per car reads as "the whole fleet is free" (audit bug 2).
        self.assertNotIn("No chauffeur on it today", rows[0]["meaning"])

        # Give the day a board, with this car left without a driver, and the
        # sentence is earned.
        other = self.unit("6")
        DriverVehicleAssignment.objects.create(
            driver=self.george, vehicle=other, date=TODAY)
        rows = self._desk()["queue"]["rows"]
        self.assertIn("No chauffeur on it today", rows[0]["meaning"])
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY + timedelta(days=2),
                                       expected_back_on=TODAY + timedelta(days=3), reason="brakes")
        rows = self._desk()["queue"]["rows"]
        self.assertEqual(rows[0]["tag"], fleet_queue.BOOKED)
        self.assertIn("brakes grinding", rows[0]["title"])

    def test_state_bar_covers_every_unit_once_and_skips_empty_states(self):
        self.unit("1"); self.unit("2")
        down = self.unit("3")
        VehicleDowntime.objects.create(vehicle=down, starts_on=TODAY, reason="x")
        bar = self._desk()["state_bar"]
        self.assertEqual([(s["key"], s["n"]) for s in bar], [("ready", 2), ("down", 1)])

    def test_paperwork_is_grouped_fleet_wide(self):
        a = self.unit("1", registration_expires_on=TODAY + timedelta(days=10))
        b = self.unit("2", registration_expires_on=TODAY + timedelta(days=16))
        c = self.unit("3", insurance_expires_on=TODAY - timedelta(days=2))
        for v in (a, b, c):
            v.permit_mco = True
            v.permit_mco_expires_on = TODAY + timedelta(days=16)
            v.save()
        rows = self._desk()["paperwork"]
        labels = [(r["label"], r["badge"], r["units_text"]) for r in rows]
        self.assertEqual(labels[0], ("Insurance", "Expired", "#3"))
        self.assertIn(("Registration", "10 days", "#1 and #2"), labels)
        self.assertIn(("MCO airport permits", "16 days", "all 3 units"), labels)
        self.assertFalse(any(r["badge"] == "Missing" for r in rows))
        # A unit with no dates at all is a setup gap, grouped on one line.
        self.unit("4")
        rows = self._desk()["paperwork"]
        self.assertEqual(rows[-1]["badge"], "Missing")
        self.assertEqual(rows[-1]["units_text"], "#4")
        # The permits count once every unit holds one.
        self.assertIn(("MCO airport permits", "16 days", "#1, #2 and #3"),
                      [(r["label"], r["badge"], r["units_text"]) for r in rows])


class PageTests(_FleetFixture):
    def test_desk_renders_the_queue_and_the_finder(self):
        v = self.unit("4")
        _fault(v, "P202E"); _fault(v, "P208E")
        self.leg(DAY, 9)
        resp = self.client.get(reverse("fleet_desk"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Do now")
        self.assertContains(resp, "Needs the shop")
        self.assertContains(resp, "P202E")
        self.assertContains(resp, "When can I take a car down?")
        self.assertEqual(resp.context["finder_default_unit"], v.id)
        self.assertEqual(resp.context["finder_default_hours"], 4)
        self.assertEqual(resp.context["finder_payload"]["units"][0]["codes"], ["P202E", "P208E"])
        self.assertEqual(len(resp.context["finder_payload"]["days"]), fleet_windows.DESK_DAYS)

    def test_outlook_renders_with_a_unit_and_a_horizon(self):
        v = self.unit("1")
        self.unit("2")
        _fault(v, "P0420")
        self.leg(DAY, 9)
        resp = self.client.get(reverse("fleet_outlook") + f"?unit={v.id}&hours=6&days=28")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["unit_id"], v.id)
        self.assertEqual(resp.context["hours"], 6)
        self.assertEqual(resp.context["days"], 28)
        self.assertEqual(len(resp.context["payload"]["days"]), 28)
        self.assertEqual(resp.context["payload"]["units"][0]["codes"], ["P0420"])
        self.assertContains(resp, "Day by day")
        # Nonsense parameters fall back rather than 500.
        resp = self.client.get(reverse("fleet_outlook") + "?unit=999&hours=5&days=99")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["days"], 14)
        self.assertEqual(resp.context["hours"], 4)
