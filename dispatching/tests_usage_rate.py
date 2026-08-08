"""Usage rate — average miles per day / per week, and service projections.

Run with:  ./manage.py test dispatching.tests_usage_rate

Pure arithmetic, tested before it is wired to anything, same as the rest of
dispatching/mileage.py.

The load-bearing distinction, and the reason this is a function rather than an
inline average: an UNKNOWN day and a DIDN'T-MOVE day are not the same number.

  * None (unknown — dead gateway) is excluded from the sum AND the denominator.
    Averaging it in as zero would understate a busy car and push its next
    service projection out to never.
  * 0 (provably parked) counts in the denominator. A car that sits every Sunday
    really does average less over a week, and dropping those days inflates the
    rate into a figure nobody can plan a shop day against.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from dispatching.mileage import days_to_cover, usage_rate


class UsageRateTests(SimpleTestCase):
    def test_a_simple_average(self):
        rate = usage_rate([Decimal("100"), Decimal("200"), Decimal("300")])
        self.assertEqual(rate.per_day, Decimal("200.0"))
        self.assertEqual(rate.total_miles, Decimal("600"))
        self.assertEqual(rate.known_days, 3)

    def test_per_week_is_seven_days_at_the_daily_rate(self):
        rate = usage_rate([Decimal("10"), Decimal("20")])
        self.assertEqual(rate.per_day, Decimal("15.0"))
        self.assertEqual(rate.per_week, Decimal("105.0"))

    def test_unknown_days_are_excluded_from_the_denominator(self):
        """A dead gateway must not look like a parked car."""
        rate = usage_rate([Decimal("300"), None, None, Decimal("300")])
        self.assertEqual(rate.per_day, Decimal("300.0"),
                         "unknown days dragged the average down")
        self.assertEqual(rate.known_days, 2)
        self.assertEqual(rate.total_days, 4)

    def test_zero_mile_days_do_count(self):
        """The car genuinely sat. That's real data and it lowers the rate."""
        rate = usage_rate([Decimal("300"), Decimal("0"), Decimal("0"), Decimal("300")])
        self.assertEqual(rate.per_day, Decimal("150.0"))
        self.assertEqual(rate.known_days, 4)

    def test_unknown_and_zero_are_not_confused(self):
        """The distinction the whole function exists for, in one assertion."""
        parked = usage_rate([Decimal("300"), Decimal("0")])
        offline = usage_rate([Decimal("300"), None])
        self.assertEqual(parked.per_day, Decimal("150.0"))
        self.assertEqual(offline.per_day, Decimal("300.0"))

    def test_nothing_known_reports_unknown_not_zero(self):
        rate = usage_rate([None, None, None])
        self.assertIsNone(rate.per_day)
        self.assertIsNone(rate.per_week)
        self.assertIsNone(rate.total_miles)
        self.assertEqual(rate.known_days, 0)
        self.assertEqual(rate.total_days, 3)
        self.assertFalse(rate.is_known)

    def test_an_empty_series_is_unknown(self):
        rate = usage_rate([])
        self.assertIsNone(rate.per_day)
        self.assertEqual(rate.total_days, 0)

    def test_a_genuinely_idle_car_averages_zero_and_that_is_known(self):
        """Zero is a real answer — distinct from 'we don't know'."""
        rate = usage_rate([Decimal("0"), Decimal("0")])
        self.assertEqual(rate.per_day, Decimal("0.0"))
        self.assertTrue(rate.is_known)

    def test_total_days_is_carried_for_coverage_and_changes_no_maths(self):
        rate = usage_rate([Decimal("100"), Decimal("200")], total_days=30)
        self.assertEqual(rate.per_day, Decimal("150.0"))
        self.assertEqual(rate.known_days, 2)
        self.assertEqual(rate.total_days, 30)

    def test_accepts_plain_numbers(self):
        self.assertEqual(usage_rate([100, 200]).per_day, Decimal("150.0"))


class DaysToCoverTests(SimpleTestCase):
    def test_straightforward_projection(self):
        self.assertEqual(days_to_cover(Decimal("1000"), Decimal("100")), 10)

    def test_rounds_up_so_a_projection_is_never_optimistic(self):
        """Due 'in 10 days' when it lands mid-day-11 sends someone to the shop
        a day early; the reverse sends them late."""
        self.assertEqual(days_to_cover(Decimal("1050"), Decimal("100")), 11)

    def test_an_unknown_rate_gives_no_projection(self):
        self.assertIsNone(days_to_cover(Decimal("1000"), None))

    def test_a_parked_car_gives_no_projection(self):
        """At 0 mi/day it never gets there. 'Due in 41,000 days' is a worse
        answer than declining — someone plans a shop day around this."""
        self.assertIsNone(days_to_cover(Decimal("1000"), Decimal("0")))

    def test_unknown_remaining_miles_gives_no_projection(self):
        self.assertIsNone(days_to_cover(None, Decimal("100")))

    def test_already_due_reports_zero_days(self):
        self.assertEqual(days_to_cover(Decimal("0"), Decimal("100")), 0)

    def test_overdue_reports_zero_rather_than_a_negative(self):
        self.assertEqual(days_to_cover(Decimal("-500"), Decimal("100")), 0)

    def test_a_realistic_oil_change_projection(self):
        """3,000 mi to go on a car doing ~335 mi/day — about 9 days."""
        rate = usage_rate([Decimal("335.5"), Decimal("339.0"), Decimal("329.7")])
        self.assertEqual(days_to_cover(Decimal("3000"), rate.per_day), 9)
