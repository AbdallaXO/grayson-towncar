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
  * ONE ROW PER SHOP VISIT: the VehicleDowntime ledger, so two windows can
    coexist and a closed one is history rather than an overwritten column.
  * AN EXPIRED PERMIT IS NOT A PERMIT: a lapsed decal is worth exactly as much
    as no decal, and must never render as a tick.
  * PICKUP ONLY: a car with no decal may still DROP at MCO / SFB / the Port.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from drivers.models import FleetVehicle, VehicleDowntime
from rates.models import Vehicle
from business.datefmt import strf

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
    """The downtime ledger, read through the same per-date questions every
    scheduling surface asks. ``expected_back_on`` is the FIRST DAY BACK."""

    def _down(self, v, starts_on, back=None, reason="Transmission", **kw):
        return VehicleDowntime.objects.create(
            vehicle=v, starts_on=starts_on, expected_back_on=back, reason=reason, **kw)

    def test_a_healthy_unit_is_never_out_of_service(self):
        v = self._unit()
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_now)
        self.assertEqual(v.out_of_service_label(TODAY), "")

    def test_open_ended_window_covers_today_and_every_day_after(self):
        v = self._unit()
        self._down(v, TODAY)
        self.assertTrue(v.is_out_of_service_on(TODAY))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=90)))
        self.assertTrue(v.is_out_of_service_now)

    def test_a_window_does_not_reach_back_before_it_starts(self):
        """Yesterday's board must not retroactively lose a car that broke today."""
        v = self._unit()
        self._down(v, TODAY)
        self.assertFalse(v.is_out_of_service_on(TODAY - timedelta(days=1)))

    def test_blocked_up_to_but_not_including_the_expected_return_day(self):
        v = self._unit()
        self._down(v, TODAY + timedelta(days=1), back=TODAY + timedelta(days=4))
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=1)))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=3)))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=4)),
                         "the expected-back day is the first day dispatch can plan on it")

    def test_a_planned_window_releases_the_car_by_itself(self):
        """Fleet must not have to be at a keyboard at 5 AM for dispatch to
        have the car back on the day it was promised."""
        v = self._unit()
        self._down(v, TODAY, back=TODAY + timedelta(days=2))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=2)))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=30)))

    def test_a_future_window_leaves_the_unit_usable_today(self):
        """Booking a shop slot for next week must not take the car off this week."""
        v = self._unit()
        self._down(v, TODAY + timedelta(days=7), back=TODAY + timedelta(days=10))
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_now)

    def test_a_closed_downtime_never_blocks(self):
        """History is history — once closed it must not touch any board."""
        v = self._unit()
        self._down(v, TODAY - timedelta(days=5), back=TODAY + timedelta(days=5),
                   ended_on=TODAY - timedelta(days=1))
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=2)))

    def test_closing_early_releases_the_car_from_that_day(self):
        v = self._unit()
        d = self._down(v, TODAY - timedelta(days=3), back=TODAY + timedelta(days=3))
        self.assertTrue(v.is_out_of_service_on(TODAY))
        d.ended_on = TODAY
        d.save()
        v = FleetVehicle.objects.get(pk=v.pk)  # drop the instance cache
        self.assertFalse(v.is_out_of_service_on(TODAY))
        self.assertTrue(v.is_out_of_service_on(TODAY - timedelta(days=1)) is False,
                        "closed rows are not consulted for any day")

    def test_two_windows_on_one_unit_each_gate_their_own_dates(self):
        """A repair this week AND a tyre slot next month — the reason the
        single-window columns were replaced."""
        v = self._unit()
        self._down(v, TODAY, back=TODAY + timedelta(days=2), reason="Brakes")
        self._down(v, TODAY + timedelta(days=20), back=TODAY + timedelta(days=22),
                   reason="Tyres", category="tires")
        self.assertTrue(v.is_out_of_service_on(TODAY))
        self.assertFalse(v.is_out_of_service_on(TODAY + timedelta(days=5)))
        self.assertTrue(v.is_out_of_service_on(TODAY + timedelta(days=21)))
        self.assertIn("Tyres", v.out_of_service_label(TODAY + timedelta(days=21)))
        self.assertIn("Brakes", v.out_of_service_label(TODAY))

    def test_a_null_day_never_reports_out_of_service(self):
        v = self._unit()
        self._down(v, TODAY)
        self.assertFalse(v.is_out_of_service_on(None))

    def test_prefetched_and_lazy_answers_agree(self):
        v = self._unit()
        self._down(v, TODAY, back=TODAY + timedelta(days=2))
        lazy = FleetVehicle.objects.get(pk=v.pk)
        eager = FleetVehicle.objects.with_open_downtimes().get(pk=v.pk)
        for offset in range(-1, 4):
            day = TODAY + timedelta(days=offset)
            self.assertEqual(lazy.is_out_of_service_on(day), eager.is_out_of_service_on(day))


class UnconfirmedReturnTests(_VehicleFixture):
    """Rule 2: an open row past its expected-back day is a question, not a block."""

    def test_past_the_expected_day_the_unit_is_usable_with_a_notice(self):
        v = self._unit()
        VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=4),
            expected_back_on=TODAY - timedelta(days=1), reason="Transmission")
        self.assertFalse(v.is_out_of_service_on(TODAY),
                         "a forgotten row must cost a nag, never a car")
        self.assertEqual(v.out_of_service_label(TODAY), "")
        notice = v.downtime_notice(TODAY)
        self.assertIn("Transmission", notice)
        self.assertIn("not yet confirmed", notice)

    def test_no_notice_while_still_inside_the_window(self):
        v = self._unit()
        VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY, expected_back_on=TODAY + timedelta(days=2),
            reason="Transmission")
        self.assertEqual(v.downtime_notice(TODAY), "")

    def test_state_helpers(self):
        v = self._unit()
        live = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=1),
            expected_back_on=TODAY + timedelta(days=1), reason="a")
        overdue = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY - timedelta(days=5),
            expected_back_on=TODAY, reason="b")
        planned = VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY + timedelta(days=3), reason="c")
        self.assertTrue(live.is_live(TODAY))
        self.assertFalse(live.is_overdue(TODAY))
        self.assertTrue(overdue.is_overdue(TODAY))
        self.assertFalse(overdue.is_live(TODAY))
        self.assertTrue(planned.is_planned(TODAY))
        self.assertEqual(live.days_down(TODAY), 2)
        self.assertEqual(planned.days_down(TODAY), None)
        self.assertEqual(planned.planned_days(), None)


class OutOfServiceLabelTests(_VehicleFixture):
    def test_label_names_the_reason_and_the_return_date(self):
        v = self._unit()
        back = TODAY + timedelta(days=3)
        VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY, expected_back_on=back,
            reason="Transmission, at Bob's")
        label = v.out_of_service_label(TODAY)
        self.assertIn("Transmission, at Bob's", label)
        self.assertIn("back", label)
        self.assertIn(strf(back, "%a %b %-d"), label)

    def test_open_ended_label_says_there_is_no_return_date(self):
        v = self._unit()
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY, reason="Rear-ended 8/3")
        label = v.out_of_service_label(TODAY)
        self.assertIn("Rear-ended 8/3", label)
        self.assertIn("no return date", label)

    def test_label_falls_back_to_the_category_when_no_reason_was_given(self):
        v = self._unit()
        VehicleDowntime.objects.create(vehicle=v, starts_on=TODAY, reason="", category="repair")
        self.assertIn("Repair", v.out_of_service_label(TODAY))

    def test_no_label_on_a_day_the_window_does_not_cover(self):
        v = self._unit()
        VehicleDowntime.objects.create(
            vehicle=v, starts_on=TODAY, expected_back_on=TODAY + timedelta(days=1),
            reason="Oil change")
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
