"""Vehicle out-of-service windows and operating permits (FleetVehicle).

Run with:  ./manage.py test dispatching.tests_vehicle_status

The model layer, tested before anything is wired to it — same order the mileage
resolver was built in, and for the same reason: this is the code every other
surface will trust.

What must hold:
  * PER-DATE, NOT "NOW": the planner schedules future dates, so a unit in the
    shop this week must be assignable next week. Every question is asked about a
    specific day.
  * OPEN-ENDED IS A REAL STATE: "down, no ETA" must be expressible and must not
    read as "available".
  * AN EXPIRED PERMIT IS NOT A PERMIT: a lapsed decal is worth exactly as much
    as no decal, and must never render as a tick.
  * PICKUP ONLY: a car with no decal may still DROP at MCO / SFB / the Port.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from drivers.models import FleetVehicle
from rates.models import Vehicle

TODAY = timezone.localdate()


class _VehicleFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6)

    def _unit(self, number="7", **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, vehicle_type=self.vtype, year=2024,
            make="Chevrolet", model="Suburban", **kw)


class OutOfServiceWindowTests(_VehicleFixture):
    def test_a_healthy_unit_is_never_out_of_service(self):
        v = self._unit()
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_now)
        self.assertEqual(v.out_of_service_label(TODAY), "")

    def test_open_ended_window_covers_today_and_every_day_after(self):
        v = self._unit(out_of_service_from=TODAY, out_of_service_reason="Transmission")
        self.assertTrue(v.is_out_of_service_on(TODAY))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=90)))
        self.assertTrue(v.is_out_of_service_now)

    def test_a_window_does_not_reach_back_before_it_starts(self):
        """Yesterday's board must not retroactively lose a car that broke today."""
        v = self._unit(out_of_service_from=TODAY)
        self.assertFalse(v.is_out_of_service_on(TODAY - timedelta(days=1)))

    def test_closed_window_is_inclusive_at_both_ends(self):
        v = self._unit(
            out_of_service_from=TODAY + timedelta(days=1),
            out_of_service_until=TODAY + timedelta(days=3))
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=1)))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=3)))

    def test_the_unit_is_back_the_day_after_the_window_closes(self):
        v = self._unit(
            out_of_service_from=TODAY, out_of_service_until=TODAY + timedelta(days=2))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=3)),
                         "a closed window must release the unit, not strand it")

    def test_a_future_window_leaves_the_unit_usable_today(self):
        """Booking a shop slot for next week must not take the car off this week."""
        v = self._unit(
            out_of_service_from=TODAY + timedelta(days=7),
            out_of_service_until=TODAY + timedelta(days=9))
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_now)

    def test_until_without_from_means_in_service(self):
        """A stray end date with no start is not a window — it must not gate."""
        v = self._unit(out_of_service_until=TODAY + timedelta(days=5))
        self.assertFalse(v.is_out_of_service_on(TODAY))

    def test_a_null_day_never_reports_out_of_service(self):
        v = self._unit(out_of_service_from=TODAY)
        self.assertFalse(v.is_out_of_service_on(None))


class OutOfServiceLabelTests(_VehicleFixture):
    def test_label_names_the_reason_and_the_return_date(self):
        v = self._unit(
            out_of_service_from=TODAY,
            out_of_service_until=TODAY + timedelta(days=2),
            out_of_service_reason="Transmission, at Bob's")
        label = v.out_of_service_label(TODAY)
        self.assertIn("Transmission, at Bob's", label)
        self.assertIn("back", label)
        back = (TODAY + timedelta(days=3)).strftime("%a %b %-d")
        self.assertIn(back, label, "the car is back the day AFTER the window ends")

    def test_open_ended_label_says_there_is_no_return_date(self):
        v = self._unit(out_of_service_from=TODAY, out_of_service_reason="Rear-ended 8/3")
        label = v.out_of_service_label(TODAY)
        self.assertIn("Rear-ended 8/3", label)
        self.assertIn("no return date", label)

    def test_label_falls_back_when_no_reason_was_given(self):
        v = self._unit(out_of_service_from=TODAY)
        self.assertIn("Out of service", v.out_of_service_label(TODAY))

    def test_no_label_on_a_day_the_window_does_not_cover(self):
        v = self._unit(
            out_of_service_from=TODAY, out_of_service_until=TODAY,
            out_of_service_reason="Oil change")
        self.assertEqual(v.out_of_service_label(TODAY + timedelta(days=1)), "")


class PermitTests(_VehicleFixture):
    def test_a_new_unit_holds_no_permits(self):
        rows = self._unit().permits(day=TODAY)
        self.assertEqual([r["key"] for r in rows], ["mco", "sanford", "port_canaveral"])
        self.assertTrue(all(not r["valid"] for r in rows))

    def test_a_held_permit_with_no_expiry_is_valid(self):
        v = self._unit(permit_mco=True)
        mco = v.permit_for_location("MCO Terminal", day=TODAY)
        self.assertTrue(mco["on_file"])
        self.assertTrue(mco["valid"])
        self.assertFalse(mco["expired"])

    def test_a_permit_valid_through_today_still_counts(self):
        v = self._unit(permit_mco=True, permit_mco_expires_on=TODAY)
        self.assertTrue(v.permit_for_location("MCO Terminal", day=TODAY)["valid"])

    def test_an_expired_permit_is_not_a_permit(self):
        v = self._unit(permit_mco=True,
                       permit_mco_expires_on=TODAY - timedelta(days=1))
        mco = v.permit_for_location("MCO Terminal", day=TODAY)
        self.assertTrue(mco["on_file"], "we still know it was on file")
        self.assertTrue(mco["expired"])
        self.assertFalse(mco["valid"], "a lapsed decal is worth no decal")

    def test_permits_are_evaluated_against_the_day_asked_about(self):
        """Scheduling three weeks out against a decal that lapses next week."""
        v = self._unit(permit_port_canaveral=True,
                       permit_port_canaveral_expires_on=TODAY + timedelta(days=7))
        self.assertTrue(v.permit_for_location(
            "Port Canaveral Area", day=TODAY)["valid"])
        self.assertFalse(v.permit_for_location(
            "Port Canaveral Area", day=TODAY + timedelta(days=21))["valid"])

    def test_a_location_needing_no_permit_returns_none(self):
        self.assertIsNone(
            self._unit().permit_for_location("Disney Resort", day=TODAY))


class PermitPickupMatchTests(_VehicleFixture):
    """The permit gate is about PICKING UP at a permitted place."""

    def test_missing_mco_permit_is_reported_for_an_mco_pickup(self):
        v = self._unit()
        row = v.missing_permit_for_pickup("MCO Terminal B", day=TODAY)
        self.assertIsNotNone(row)
        self.assertEqual(row["key"], "mco")

    def test_a_permitted_unit_reports_nothing_missing(self):
        v = self._unit(permit_mco=True)
        self.assertIsNone(v.missing_permit_for_pickup("MCO Terminal B", day=TODAY))

    def test_an_expired_permit_is_reported_missing(self):
        v = self._unit(permit_mco=True,
                       permit_mco_expires_on=TODAY - timedelta(days=1))
        row = v.missing_permit_for_pickup("MCO Terminal B", day=TODAY)
        self.assertIsNotNone(row)
        self.assertTrue(row["expired"])

    def test_port_canaveral_pickup_needs_the_port_permit(self):
        v = self._unit(permit_mco=True)  # MCO decal doesn't cover the Port
        row = v.missing_permit_for_pickup("Port Canaveral Terminal 1", day=TODAY)
        self.assertEqual(row["key"], "port_canaveral")

    def test_sanford_pickup_needs_the_sanford_permit(self):
        v = self._unit()
        row = v.missing_permit_for_pickup("Orlando Sanford International Airport",
                                          day=TODAY)
        self.assertEqual(row["key"], "sanford")

    def test_an_unpermitted_location_never_reports_a_problem(self):
        v = self._unit()
        for where in ("Disney's Grand Floridian", "1234 Main St, Kissimmee",
                      "Universal Portofino Bay"):
            self.assertIsNone(v.missing_permit_for_pickup(where, day=TODAY),
                              f"{where} needs no permit")

    def test_a_blank_pickup_location_is_not_a_permit_problem(self):
        self.assertIsNone(self._unit().missing_permit_for_pickup("", day=TODAY))
