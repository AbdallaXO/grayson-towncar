"""detect_leg_flags on the pickup_policy clocks (advisor plan, hazard guard 12).

Run with:  ./manage.py test dispatching.tests_dispatch_flags

The board's row-flag engine (dispatching/utils.py detect_leg_flags) used to run
raw clock math against the booked pickup time, so it could contradict both the
Recovery Advisor and the GPS sweep at the threshold. Flags 2 ("should be on the
way") and 3 ("not picked up") now ride pickup_policy:

  * Flag 2 measures against pickup_deadline() — a flight-tracked arrival is due
    at gate + ARRIVAL_MEET_GRACE_MIN, so a DELAYED flight moves the deadline out
    and the driver is no longer chased for a plane that hasn't landed.
  * Flag 3 measures against pickup_expected_dt() and keeps the arrivals-excluded
    rule (drivers legitimately wait 1+ hours at the airport).
  * Both overdue flags EXPIRE past OVERDUE_STALE_MIN (45): an untouched overdue
    is an unpressed button — data hygiene, never a live alarm (guard 2 parity
    with the advisor and samsara_risk).
  * Flag 1 ("not confirmed") is status hygiene and deliberately unchanged.
  * The cruise OTW lead (50 min — Port Canaveral is far) is still honored,
    now against the policy deadline.
"""
from datetime import date, datetime, time

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from dispatching.pickup_policy import ARRIVAL_MEET_GRACE_MIN, OVERDUE_STALE_MIN
from dispatching.utils import OTW_LEAD_MINUTES, OTW_LEAD_MINUTES_CRUISE, detect_leg_flags
from drivers.models import Driver
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Flight, Leg, Reservation

TARGET = date(2026, 6, 1)


def _dt(hour, minute=0):
    return datetime(TARGET.year, TARGET.month, TARGET.day, hour, minute)


class _FlagFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5559876543")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user("df_tito", first_name="Tito"),
            driver_type="inhouse")

    def _leg(self, pickup, dropoff, pickup_time, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        defaults = dict(
            reservation=res, pickup_date=TARGET, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=self.driver)
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _arrival(self, pickup_time, arrival_dt, **kw):
        """MCO→Disney arrival whose controlling flight lands at arrival_dt."""
        flight = Flight.objects.create(
            airline="DL", flight_number="100",
            scheduled_arrival_local=arrival_dt)
        leg = self._leg("MCO Airport", "Disney Resort", pickup_time, **kw)
        leg.flight_information = flight
        leg.save()
        return leg

    @staticmethod
    def _texts(flags):
        return [f["text"] for f in flags]


class DelayedFlightTests(_FlagFixture):
    """The unification's whole point: a delayed flight is NOT 'late'."""

    def test_delayed_flight_is_not_flagged(self):
        # Booked 10:00, flight now landing 10:40 → driver due 10:50. At 10:15
        # the OLD raw-clock rule screamed "not on the way — 15 min past pickup";
        # the policy deadline is 35 min out, beyond the 20-min lead. No flags.
        leg = self._arrival(time(10, 0), _dt(10, 40))
        self.assertEqual(detect_leg_flags(leg, _dt(10, 15)), [])

    def test_delayed_flight_flags_inside_lead_of_new_deadline(self):
        # Same leg at 10:35 — 15 min before the 10:50 meet deadline → the OTW
        # nudge fires as a WARNING against the policy clock, not the booked one.
        leg = self._arrival(time(10, 0), _dt(10, 40))
        flags = detect_leg_flags(leg, _dt(10, 35))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["level"], "warning")
        self.assertIn("Not on the way yet", flags[0]["text"])

    def test_on_schedule_flight_still_flags_past_meet_deadline(self):
        # Flight landed 10:00 as booked → due 10:10. At 10:25 he's 15 past the
        # meet deadline and still not on the way — danger, honestly earned.
        self.assertEqual(ARRIVAL_MEET_GRACE_MIN, 10)
        leg = self._arrival(time(10, 0), _dt(10, 0))
        flags = detect_leg_flags(leg, _dt(10, 25))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["level"], "danger")
        self.assertIn("Not on the way", flags[0]["text"])

    def test_arrivals_stay_excluded_from_not_picked_up(self):
        # Guard: gate-to-guest-in-car takes ~45 min legitimately. 30 min after
        # the flight landed there must be no "not picked up" flag — and the OTW
        # flag (20 past the meet deadline) is the only thing showing.
        leg = self._arrival(time(10, 0), _dt(10, 0))
        flags = detect_leg_flags(leg, _dt(10, 30))
        self.assertEqual(len(flags), 1)
        self.assertIn("Not on the way", flags[0]["text"])
        self.assertNotIn("Not picked up", " ".join(self._texts(flags)))


class OverdueStaleTests(_FlagFixture):
    """Past OVERDUE_STALE_MIN an overdue pickup is an unpressed button."""

    def test_overdue_past_stale_window_stops_flagging(self):
        self.assertEqual(OVERDUE_STALE_MIN, 45)
        leg = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        # 50 min past a return's booked pickup, nobody tapped anything:
        # both the OTW danger and the not-picked-up flag have aged out.
        self.assertEqual(detect_leg_flags(leg, _dt(9, 50)), [])

    def test_overdue_inside_stale_window_still_flags(self):
        leg = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        flags = detect_leg_flags(leg, _dt(9, 30))
        texts = " | ".join(self._texts(flags))
        self.assertIn("Not on the way", texts)
        self.assertIn("Not picked up", texts)
        self.assertTrue(all(f["level"] == "danger" for f in flags))

    def test_stale_expiry_never_touches_not_confirmed(self):
        # Flag 1 is status hygiene, not an overdue signal — it survives.
        leg = self._leg("Disney Resort", "MCO Airport", time(9, 0),
                        status="in-progress")
        flags = detect_leg_flags(leg, _dt(9, 50))
        self.assertEqual(self._texts(flags), ["Not confirmed yet"])
        self.assertEqual(flags[0]["level"], "danger")


class CruiseLeadTests(_FlagFixture):
    """Port Canaveral is far — the 50-min OTW lead still holds."""

    def test_cruise_lead_time_still_honored(self):
        self.assertEqual(OTW_LEAD_MINUTES_CRUISE, 50)
        leg = self._leg("Disney Resort", "Port Canaveral Terminal 5", time(9, 0))
        # 45 min before a cruise pickup: inside the 50-min cruise lead → warn.
        flags = detect_leg_flags(leg, _dt(8, 15))
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["level"], "warning")
        self.assertIn("Not on the way yet", flags[0]["text"])

    def test_non_cruise_keeps_the_short_lead(self):
        self.assertEqual(OTW_LEAD_MINUTES, 20)
        leg = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        # Same 45 minutes out on a plain return: beyond the 20-min lead → quiet.
        self.assertEqual(detect_leg_flags(leg, _dt(8, 15)), [])

    def test_otw_and_later_statuses_never_nagged(self):
        leg = self._leg("Disney Resort", "Port Canaveral Terminal 5", time(9, 0),
                        status="on-the-way")
        self.assertEqual(detect_leg_flags(leg, _dt(8, 45)), [])


class GuardRailTests(_FlagFixture):
    def test_unassigned_completed_cancelled_return_nothing(self):
        for kw in (dict(driver=None), dict(status="completed"),
                   dict(status="cancelled")):
            leg = self._leg("Disney Resort", "MCO Airport", time(9, 0), **kw)
            self.assertEqual(detect_leg_flags(leg, _dt(9, 5)), [])

    def test_return_shape_is_level_icon_text(self):
        leg = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        for flag in detect_leg_flags(leg, _dt(9, 5)):
            self.assertEqual(set(flag), {"level", "icon", "text"})
            self.assertIn(flag["level"], ("warning", "danger"))
