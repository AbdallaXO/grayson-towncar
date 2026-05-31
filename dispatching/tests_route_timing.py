"""Route-timing accuracy guards.

SimpleTestCase (no database) so they run even where the local DB role can't
create a test database. They lock in the fixes to:
  - airport classification agreement between categorize_location and get_trip_type
  - red-eye / stale-date flight anchoring
  - flight-aware time-of-day bucketing
  - sample_count counting only legs that contributed a usable timing value
  - one shared arrival anchor for dwell measurement and scheduling
"""
from datetime import datetime, date, time, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from dispatching.analytics import (
    is_airport_location, categorize_location, leg_time_of_day_category,
    best_flight_arrival_local, _compute_bucket_metrics,
)
from dispatching.scheduler import _anchor_flight_dt
from reservations.models import Leg


class AirportClassificationTests(SimpleTestCase):
    """categorize_location and Leg.get_trip_type must use the SAME airport detector."""

    def test_terminal_airline_baggage_are_mco(self):
        for txt in ["Terminal B", "Spirit Airlines", "Baggage Claim 5", "Gate 42"]:
            self.assertTrue(is_airport_location(txt), txt)
            self.assertEqual(categorize_location(txt), "MCO Terminal", txt)

    def test_named_airports(self):
        self.assertEqual(categorize_location("Orlando International Airport (MCO)"), "MCO Terminal")
        self.assertEqual(categorize_location("Orlando Sanford Airport (SFB)"), "SFB Terminal")

    def test_airport_hotel_is_not_airport(self):
        self.assertFalse(is_airport_location("Hyatt Regency Orlando Airport"))
        self.assertEqual(categorize_location("Hyatt Regency Orlando Airport"), "Airport Hotel")

    def test_cruise_port_is_not_airport(self):
        self.assertFalse(is_airport_location("Port Canaveral Cruise Terminal"))
        self.assertEqual(categorize_location("Port Canaveral Cruise Terminal"), "Port Canaveral Area")

    def test_trip_type_agrees_with_categorization(self):
        # Airport pickups written as terminal/airline names must be 'arrival', not 'other'.
        for pickup in ["Terminal B", "Spirit Airlines", "Baggage Claim 5"]:
            leg = Leg(pickup_location=pickup, dropoff_location="Disney Grand Floridian")
            self.assertEqual(leg.get_trip_type(), "arrival", pickup)
        # Drop at airport = return.
        leg = Leg(pickup_location="Disney Grand Floridian", dropoff_location="Terminal A")
        self.assertEqual(leg.get_trip_type(), "return")
        # Non-airport both ends = other.
        leg = Leg(pickup_location="Disney Grand Floridian", dropoff_location="Hilton Bonnet Creek")
        self.assertEqual(leg.get_trip_type(), "other")


class FlightAnchorTests(SimpleTestCase):
    """_anchor_flight_dt places the flight clock on the calendar day nearest pickup."""

    def test_redeye_same_day(self):
        self.assertEqual(
            _anchor_flight_dt(datetime(2026, 5, 1, 0, 30), datetime(2026, 5, 1, 0, 45)),
            datetime(2026, 5, 1, 0, 30),
        )

    def test_prev_day_landing_closest(self):
        self.assertEqual(
            _anchor_flight_dt(datetime(2026, 4, 30, 23, 50), datetime(2026, 5, 1, 0, 20)),
            datetime(2026, 4, 30, 23, 50),
        )

    def test_normal_midday(self):
        self.assertEqual(
            _anchor_flight_dt(datetime(2026, 5, 1, 14, 0), datetime(2026, 5, 1, 14, 20)),
            datetime(2026, 5, 1, 14, 0),
        )

    def test_stale_date_pulled_to_pickup_day(self):
        # Old bug: a flight carrying a far-off date forced the clearing clock weeks off.
        self.assertEqual(
            _anchor_flight_dt(datetime(2026, 3, 15, 9, 0), datetime(2026, 5, 1, 9, 30)),
            datetime(2026, 5, 1, 9, 0),
        )


class TimeOfDayBucketTests(SimpleTestCase):
    """Arrivals bucket by actual flight arrival; everything else by scheduled pickup."""

    @staticmethod
    def _flight(dt):
        return SimpleNamespace(
            actual_gate_arrival_local=dt, estimated_gate_arrival_local=None,
            actual_arrival_local=None, estimated_arrival_local=None,
            scheduled_gate_arrival_local=None, scheduled_arrival_local=None,
        )

    def test_delayed_arrival_buckets_by_flight(self):
        leg = SimpleNamespace(get_trip_type=lambda: "arrival", pickup_time=time(8, 30),
                              flight_information=self._flight(datetime(2026, 5, 1, 12, 5)))
        self.assertEqual(leg_time_of_day_category(leg), "midday")

    def test_arrival_without_flight_falls_back(self):
        leg = SimpleNamespace(get_trip_type=lambda: "arrival", pickup_time=time(8, 30),
                              flight_information=None)
        self.assertEqual(leg_time_of_day_category(leg), "morning_rush")

    def test_return_uses_pickup(self):
        leg = SimpleNamespace(get_trip_type=lambda: "return", pickup_time=time(8, 30),
                              flight_information=None)
        self.assertEqual(leg_time_of_day_category(leg), "morning_rush")

    def test_best_flight_arrival_gate_preferred(self):
        f = SimpleNamespace(
            actual_gate_arrival_local=datetime(2026, 5, 1, 12, 0),
            estimated_gate_arrival_local=None,
            actual_arrival_local=datetime(2026, 5, 1, 11, 45),  # runway earlier
            estimated_arrival_local=None, scheduled_gate_arrival_local=None,
            scheduled_arrival_local=None,
        )
        self.assertEqual(best_flight_arrival_local(f), datetime(2026, 5, 1, 12, 0))


class _StatusHistory(list):
    """Minimal stand-in for leg.status_history supporting .filter(...).first()."""
    def filter(self, **kw):
        if "status" in kw:
            rows = [x for x in self if x.status == kw["status"]]
        else:
            rows = [x for x in self if x.status in kw.get("status__in", [])]
        return SimpleNamespace(
            first=lambda: (rows[0] if rows else None),
            order_by=lambda *a: SimpleNamespace(first=lambda: (rows[0] if rows else None)),
        )


def _fake_return_leg(drive_min, store_stop=False):
    base = datetime(2026, 5, 1, 12, 0)
    sh = _StatusHistory([
        SimpleNamespace(status="on-the-way", timestamp=base - timedelta(minutes=20)),
        SimpleNamespace(status="picked-up", timestamp=base),
        SimpleNamespace(status="completed", timestamp=base + timedelta(minutes=drive_min)),
    ])
    return SimpleNamespace(
        get_trip_type=lambda: "return", pickup_time=time(12, 0), pickup_date=date(2026, 5, 1),
        status_history=sh, reservation=SimpleNamespace(store_stop=store_stop),
        flight_information=None,
    )


class SampleCountTests(SimpleTestCase):
    """sample_count must count only legs that produced a usable timing value."""

    def test_counts_contributing_only(self):
        legs = [
            _fake_return_leg(40), _fake_return_leg(42), _fake_return_leg(44),
            _fake_return_leg(41, store_stop=True),   # skipped (store stop)
            _fake_return_leg(5),                     # skipped (< 15-min floor)
        ]
        res = _compute_bucket_metrics(legs, "return")
        self.assertEqual(res["sample_count"], 3)
        self.assertIsNotNone(res["avg_drive_time"])

    def test_empty_bucket(self):
        res = _compute_bucket_metrics([_fake_return_leg(5)], "return")  # only an unusable leg
        self.assertEqual(res["sample_count"], 0)
        self.assertIsNone(res["avg_drive_time"])
