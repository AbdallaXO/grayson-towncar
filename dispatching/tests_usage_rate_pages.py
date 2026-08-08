"""Usage rate + odometer endpoints, as the Fleet pages render them.

Run with:  ./manage.py test dispatching.tests_usage_rate_pages

The arithmetic is covered in tests_usage_rate. This file guards the promises the
pages make:

  * The daily table shows BOTH ends of the odometer, so a miles figure can be
    checked against the dash rather than trusted.
  * The list and detail pages agree on the rate — two different averages for the
    same car is how a dispatcher learns to disbelieve both.
  * A projection is offered ONLY when it can be trusted, and never reads as a
    committed date.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import (FleetVehicle, VehicleDayReading,
                            VehicleServiceSchedule)
from rates.models import Vehicle

TODAY = timezone.localdate()
METERS_PER_MILE = Decimal("1609.344")


def _m(miles):
    """Miles -> meters, for seeding odometer columns."""
    return (Decimal(miles) * METERS_PER_MILE).quantize(Decimal("0.1"))


class _RateFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.staff = User.objects.create_user("ur_staff", password="x",
                                             is_staff=True, is_superuser=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _unit(self, number="7", **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, vehicle_type=self.vtype, year=2024,
            make="Chevrolet", model="Suburban", **kw)

    def _day(self, unit, days_ago, miles, start_odo=None):
        """One day's reading. miles=None means UNKNOWN (dead gateway)."""
        return VehicleDayReading.objects.create(
            vehicle=unit,
            date=TODAY - timedelta(days=days_ago),
            miles_driven=None if miles is None else Decimal(str(miles)),
            mileage_source="" if miles is None else "obd",
            start_odometer_meters=_m(start_odo) if start_odo is not None else None,
            end_odometer_meters=(
                _m(Decimal(str(start_odo)) + Decimal(str(miles)))
                if start_odo is not None and miles is not None else None
            ),
        )

    def _detail(self, unit):
        return self.client.get(reverse("fleet_detail", args=[unit.pk]))

    def _list(self):
        return self.client.get(reverse("fleet_list"))


class DailyTableTests(_RateFixture):
    def test_both_odometer_ends_are_shown(self):
        unit = self._unit()
        self._day(unit, 1, miles=335, start_odo=104210)
        resp = self._detail(unit)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "104,210")
        self.assertContains(resp, "104,545")

    def test_start_and_end_are_exposed_on_the_row(self):
        unit = self._unit()
        self._day(unit, 1, miles=100, start_odo=50000)
        day = self._detail(unit).context["days"][0]
        self.assertEqual(day.start_miles, Decimal("50000"))
        self.assertEqual(day.end_miles, Decimal("50100"))

    def test_a_missing_odometer_renders_as_unknown_not_zero(self):
        unit = self._unit()
        self._day(unit, 1, miles=None)
        day = self._detail(unit).context["days"][0]
        self.assertIsNone(day.start_miles)
        self.assertIsNone(day.end_miles)

    def test_the_header_advertises_the_new_columns(self):
        unit = self._unit()
        self._day(unit, 1, miles=100, start_odo=1000)
        resp = self._detail(unit)
        self.assertContains(resp, "Start odo")
        self.assertContains(resp, "End odo")


class DetailRateTests(_RateFixture):
    def test_average_per_day_and_per_week(self):
        unit = self._unit()
        for i, miles in enumerate([300, 200, 100], start=1):
            self._day(unit, i, miles=miles, start_odo=1000 * i)
        rate = self._detail(unit).context["rate"]
        self.assertEqual(rate.per_day, Decimal("200.0"))
        self.assertEqual(rate.per_week, Decimal("1400.0"))
        self.assertEqual(rate.known_days, 3)

    def test_unknown_days_do_not_drag_the_average_down(self):
        """A week of dead gateway must not halve a busy car's apparent rate."""
        unit = self._unit()
        self._day(unit, 1, miles=300, start_odo=1000)
        self._day(unit, 2, miles=None)
        self._day(unit, 3, miles=None)
        rate = self._detail(unit).context["rate"]
        self.assertEqual(rate.per_day, Decimal("300.0"))
        self.assertEqual(rate.known_days, 1)

    def test_parked_days_do_count(self):
        unit = self._unit()
        self._day(unit, 1, miles=300, start_odo=1000)
        self._day(unit, 2, miles=0, start_odo=1300)
        rate = self._detail(unit).context["rate"]
        self.assertEqual(rate.per_day, Decimal("150.0"))

    def test_a_car_with_no_readings_reports_unknown(self):
        unit = self._unit()
        resp = self._detail(unit)
        self.assertIsNone(resp.context["rate"].per_day)
        self.assertContains(resp, "No day in this window has a usable reading.")

    def test_the_page_states_how_many_days_the_average_covers(self):
        unit = self._unit()
        self._day(unit, 1, miles=300, start_odo=1000)
        self._day(unit, 2, miles=None)
        self.assertContains(self._detail(unit), "Average across the 1 day")


class ServiceProjectionTests(_RateFixture):
    def _schedule(self, unit, **kw):
        return VehicleServiceSchedule.objects.create(
            vehicle=unit, service_type="oil", is_active=True, **kw)

    def _busy(self, unit, per_day=100):
        for i in range(1, 6):
            self._day(unit, i, miles=per_day, start_odo=1000 * i)

    def test_a_projected_date_is_offered_when_the_rate_is_known(self):
        unit = self._unit()
        unit.samsara_odometer_meters = _m(10000)
        unit.samsara_odometer_source = "obd"
        unit.save()
        self._busy(unit, per_day=100)
        self._schedule(unit, interval_miles=5000,
                       last_done_odometer_miles=Decimal("9000"))
        item = self._detail(unit).context["schedules"][0]
        # due at 14,000; 4,000 to go at 100 mi/day -> 40 days
        self.assertEqual(item["miles_remaining"], Decimal("4000"))
        self.assertEqual(item["projected_days"], 40)
        self.assertEqual(item["projected_date"], TODAY + timedelta(days=40))

    def test_the_projection_is_labelled_as_a_rate_estimate(self):
        """It must never read as a booked date."""
        unit = self._unit()
        unit.samsara_odometer_meters = _m(10000)
        unit.save()
        self._busy(unit)
        self._schedule(unit, interval_miles=5000,
                       last_done_odometer_miles=Decimal("9000"))
        self.assertContains(self._detail(unit), "at this rate")

    def test_no_projection_when_the_car_is_not_moving(self):
        """At 0 mi/day it never gets there — say nothing rather than 'in 41,000
        days', because someone plans a shop day around this."""
        unit = self._unit()
        unit.samsara_odometer_meters = _m(10000)
        unit.save()
        for i in range(1, 4):
            self._day(unit, i, miles=0, start_odo=10000)
        self._schedule(unit, interval_miles=5000,
                       last_done_odometer_miles=Decimal("9000"))
        item = self._detail(unit).context["schedules"][0]
        self.assertIsNone(item["projected_days"])
        self.assertIsNone(item["projected_date"])

    def test_no_projection_when_the_rate_is_unknown(self):
        unit = self._unit()
        unit.samsara_odometer_meters = _m(10000)
        unit.save()
        self._day(unit, 1, miles=None)
        self._schedule(unit, interval_miles=5000,
                       last_done_odometer_miles=Decimal("9000"))
        self.assertIsNone(self._detail(unit).context["schedules"][0]["projected_date"])

    def test_no_projection_without_a_mileage_baseline(self):
        """A schedule with no last-done odometer can't say where due is."""
        unit = self._unit()
        self._busy(unit)
        self._schedule(unit, interval_days=180)
        item = self._detail(unit).context["schedules"][0]
        self.assertIsNone(item["miles_remaining"])
        self.assertIsNone(item["projected_date"])

    def test_an_overdue_interval_projects_no_future_date(self):
        unit = self._unit()
        unit.samsara_odometer_meters = _m(20000)
        unit.save()
        self._busy(unit)
        self._schedule(unit, interval_miles=5000,
                       last_done_odometer_miles=Decimal("9000"))
        item = self._detail(unit).context["schedules"][0]
        self.assertLess(item["miles_remaining"], 0)
        self.assertEqual(item["projected_days"], 0)
        self.assertIsNone(item["projected_date"],
                          "already overdue is a status, not a future date")


class FleetListRateTests(_RateFixture):
    def test_the_list_shows_a_weekly_rate(self):
        unit = self._unit()
        for i in range(1, 5):
            self._day(unit, i, miles=100, start_odo=1000 * i)
        resp = self._list()
        row = next(r for r in resp.context["rows"] if r["vehicle"].id == unit.id)
        self.assertEqual(row["per_day"], Decimal("100.0"))
        self.assertEqual(row["per_week"], Decimal("700.0"))
        self.assertContains(resp, "Avg / week")

    def test_list_and_detail_report_the_same_rate(self):
        """Two different averages for one car is how a dispatcher learns to
        disbelieve both."""
        unit = self._unit()
        self._day(unit, 1, miles=300, start_odo=1000)
        self._day(unit, 2, miles=0, start_odo=1300)
        self._day(unit, 3, miles=None)
        list_row = next(r for r in self._list().context["rows"]
                        if r["vehicle"].id == unit.id)
        detail_rate = self._detail(unit).context["rate"]
        self.assertEqual(list_row["per_day"], detail_rate.per_day)
        self.assertEqual(list_row["per_week"], detail_rate.per_week)

    def test_a_car_with_no_data_shows_unknown_not_zero(self):
        unit = self._unit()
        row = next(r for r in self._list().context["rows"]
                   if r["vehicle"].id == unit.id)
        self.assertIsNone(row["per_week"],
                          "an unknown rate is not a low one — it must not sort "
                          "as the least-used car")
