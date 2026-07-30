"""Tests for dispatching/load_insights.py.

Every rule gets a fires-case and a stays-silent-on-the-boundary case, because a rule
that fires on a fair fleet is worse than no rule at all. Rows are hand-made dicts —
the module is pure, so no database is needed.
"""

from datetime import date, timedelta

from django.test import SimpleTestCase

from dispatching.load_insights import (
    MIN_STREAK_DAYS,
    build_insights,
    min_avail_days,
    min_worked_days,
)

LABELS = {"full_time": "Full time", "part_time": "Part time", "": "Unlabelled"}


def row(id=1, name="Driver", emp="full_time", legs=0, worked=0, avail=0,
        worked_dates=(), window=30):
    return {
        "id": id, "name": name, "initials": name[:2].upper(), "color": "#404040",
        "employment_type": emp,
        "employment_label": LABELS[emp],
        "is_full_time": emp == "full_time",
        "legs": legs, "worked_days": worked, "avail_days": avail,
        "idle_days": max(0, avail - worked),
        "per_worked_day": (legs / worked) if worked else 0.0,
        "per_available_day": (legs / avail) if avail else 0.0,
        "per_week": legs / (window / 7) if window else 0.0,
        "per_month": legs / (window / 30) if window else 0.0,
        "worked_dates": list(worked_dates),
    }


def finding_ids(result):
    return [f["id"] for f in result["findings"]]


def exception_rules(result):
    return [(e["name"], e["rule"]) for e in result["exceptions"]]


class FloorScalingTests(SimpleTestCase):
    def test_floors_scale_with_the_window(self):
        self.assertEqual(min_avail_days(7), 3)
        self.assertEqual(min_avail_days(30), 6)
        self.assertEqual(min_avail_days(90), 18)
        self.assertEqual(min_worked_days(7), 3)
        self.assertEqual(min_worked_days(30), 3)
        self.assertEqual(min_worked_days(90), 9)


class EmptyAndQuietTests(SimpleTestCase):
    def test_empty_rows_produce_nothing(self):
        out = build_insights([], 30)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["exceptions"], [])

    def test_balanced_fleet_produces_nothing(self):
        """Three full-timers pulling near-identical loads: no rule may fire."""
        rows = [
            row(id=1, name="Ana", legs=40, worked=20, avail=22),
            row(id=2, name="Ben", legs=38, worked=19, avail=21),
            row(id=3, name="Cal", legs=42, worked=21, avail=23),
        ]
        out = build_insights(rows, 30)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["exceptions"], [])


class UnevenLoadFindingTests(SimpleTestCase):
    def test_fires_when_top_doubles_bottom(self):
        rows = [
            row(id=1, name="Top", legs=60, worked=15, avail=20),
            row(id=2, name="Mid", legs=35, worked=15, avail=20),
            row(id=3, name="Low", legs=20, worked=15, avail=20),
        ]
        out = build_insights(rows, 30)
        self.assertIn("ft_uneven_load", finding_ids(out))
        text = next(f["text"] for f in out["findings"] if f["id"] == "ft_uneven_load")
        self.assertIn("Top", text)
        self.assertIn("Low", text)
        self.assertIn("14", text)          # 60 legs / (30/7) weeks = 14 a week

    def test_silent_below_double(self):
        rows = [
            row(id=1, name="Top", legs=30, worked=15, avail=20),
            row(id=2, name="Mid", legs=25, worked=15, avail=20),
            row(id=3, name="Low", legs=20, worked=15, avail=20),
        ]
        self.assertNotIn("ft_uneven_load", finding_ids(build_insights(rows, 30)))

    def test_needs_three_qualifying_full_timers(self):
        rows = [
            row(id=1, name="Top", legs=60, worked=15, avail=20),
            row(id=2, name="Low", legs=20, worked=15, avail=20),
        ]
        self.assertNotIn("ft_uneven_load", finding_ids(build_insights(rows, 30)))

    def test_too_few_worked_days_do_not_qualify(self):
        """One big day must not brand someone the busiest driver on the fleet."""
        rows = [
            row(id=1, name="Spike", legs=12, worked=2, avail=20),   # 2 < floor of 3
            row(id=2, name="Mid", legs=25, worked=15, avail=20),
            row(id=3, name="Low", legs=20, worked=15, avail=20),
        ]
        self.assertNotIn("ft_uneven_load", finding_ids(build_insights(rows, 30)))


class IdleConcentrationFindingTests(SimpleTestCase):
    def test_fires_when_one_of_three_holds_most_idle_days(self):
        rows = [
            row(id=1, name="Held", legs=5, worked=5, avail=20),     # 15 idle
            row(id=2, name="Ok", legs=20, worked=19, avail=20),     # 1 idle
            row(id=3, name="Fine", legs=20, worked=18, avail=20),   # 2 idle
        ]
        out = build_insights(rows, 30)
        self.assertIn("ft_idle_concentration", finding_ids(out))
        text = next(f["text"] for f in out["findings"]
                    if f["id"] == "ft_idle_concentration")
        self.assertIn("Held", text)
        self.assertIn("18", text)          # cohort total
        self.assertIn("15", text)

    def test_even_spread_is_not_concentration(self):
        """Three people each holding a third: proportional, not a finding."""
        rows = [
            row(id=1, name="A", legs=10, worked=14, avail=20),
            row(id=2, name="B", legs=10, worked=14, avail=20),
            row(id=3, name="C", legs=10, worked=14, avail=20),
        ]
        self.assertNotIn("ft_idle_concentration",
                         finding_ids(build_insights(rows, 30)))

    def test_small_totals_stay_silent(self):
        rows = [
            row(id=1, name="A", legs=10, worked=18, avail=20),      # 2 idle
            row(id=2, name="B", legs=10, worked=19, avail=20),      # 1 idle
            row(id=3, name="C", legs=10, worked=20, avail=20),      # 0 idle
        ]
        self.assertNotIn("ft_idle_concentration",
                         finding_ids(build_insights(rows, 30)))


class ShareTrendFindingTests(SimpleTestCase):
    def _cur(self):
        return [row(id=1, name="A", legs=20, worked=12, avail=20),
                row(id=2, name="B", legs=20, worked=12, avail=20)]   # share 60%

    def test_fires_on_a_ten_point_drop(self):
        prior = [row(id=1, name="A", legs=20, worked=16, avail=20),
                 row(id=2, name="B", legs=20, worked=16, avail=20)]  # share 80%
        out = build_insights(self._cur(), 30, prior_rows=prior)
        self.assertIn("ft_share_trend", finding_ids(out))
        text = next(f["text"] for f in out["findings"] if f["id"] == "ft_share_trend")
        self.assertIn("60%", text)
        self.assertIn("80%", text)
        self.assertIn("down", text)

    def test_fires_upward_too(self):
        prior = [row(id=1, name="A", legs=20, worked=8, avail=20),
                 row(id=2, name="B", legs=20, worked=8, avail=20)]   # share 40%
        out = build_insights(self._cur(), 30, prior_rows=prior)
        text = next(f["text"] for f in out["findings"] if f["id"] == "ft_share_trend")
        self.assertIn("up", text)

    def test_silent_below_ten_points(self):
        prior = [row(id=1, name="A", legs=20, worked=13, avail=20),
                 row(id=2, name="B", legs=20, worked=13, avail=20)]  # share 65%
        out = build_insights(self._cur(), 30, prior_rows=prior)
        self.assertNotIn("ft_share_trend", finding_ids(out))

    def test_silent_without_prior_rows(self):
        out = build_insights(self._cur(), 30)
        self.assertNotIn("ft_share_trend", finding_ids(out))


class PartTimerOutworkingTests(SimpleTestCase):
    def test_fires_when_a_part_timer_beats_the_ft_median(self):
        rows = [
            row(id=1, name="FtA", legs=10, worked=8, avail=10),
            row(id=2, name="FtB", legs=12, worked=9, avail=11),
            row(id=3, name="Petra", emp="part_time", legs=20, worked=10, avail=12),
        ]
        out = build_insights(rows, 30)
        self.assertIn("pt_outworking_ft", finding_ids(out))
        text = next(f["text"] for f in out["findings"] if f["id"] == "pt_outworking_ft")
        self.assertIn("Petra", text)
        self.assertIn("20", text)
        self.assertIn("11", text)          # the full-time median

    def test_silent_when_part_timer_is_below_median(self):
        rows = [
            row(id=1, name="FtA", legs=10, worked=8, avail=10),
            row(id=2, name="FtB", legs=12, worked=9, avail=11),
            row(id=3, name="Petra", emp="part_time", legs=8, worked=5, avail=8),
        ]
        self.assertNotIn("pt_outworking_ft", finding_ids(build_insights(rows, 30)))


class UnlabelledFindingTests(SimpleTestCase):
    def test_counts_unlabelled_drivers(self):
        rows = [row(id=1, name="A", emp=""), row(id=2, name="B", emp=""),
                row(id=3, name="C")]
        out = build_insights(rows, 30)
        text = next(f["text"] for f in out["findings"] if f["id"] == "unlabelled")
        self.assertIn("2 chauffeurs have", text)
        self.assertFalse(next(f for f in out["findings"]
                              if f["id"] == "unlabelled")["admin_link"])

    def test_singular_wording_and_admin_link(self):
        rows = [row(id=1, name="A", emp="")]
        out = build_insights(rows, 30, include_admin_link=True)
        f = next(f for f in out["findings"] if f["id"] == "unlabelled")
        self.assertIn("1 chauffeur has", f["text"])
        self.assertTrue(f["admin_link"])

    def test_silent_when_everyone_is_labelled(self):
        rows = [row(id=1, name="A"), row(id=2, name="B", emp="part_time")]
        self.assertNotIn("unlabelled", finding_ids(build_insights(rows, 30)))


class StreakExceptionTests(SimpleTestCase):
    def _dates(self, start, n):
        return [start + timedelta(days=i) for i in range(n)]

    def test_fires_at_the_threshold(self):
        d0 = date(2026, 6, 2)
        rows = [row(id=1, name="Marathon", legs=30, worked=12, avail=14,
                    worked_dates=self._dates(d0, MIN_STREAK_DAYS))]
        out = build_insights(rows, 30)
        self.assertEqual(exception_rules(out), [("Marathon", "no_day_off_streak")])
        reason = out["exceptions"][0]["reason"]
        self.assertIn("10 days in a row", reason)
        self.assertIn("Jun 2–11", reason)

    def test_cross_month_span_label(self):
        d0 = date(2026, 6, 25)
        rows = [row(id=1, name="Marathon", legs=30, worked=12, avail=14,
                    worked_dates=self._dates(d0, 12))]
        reason = build_insights(rows, 30)["exceptions"][0]["reason"]
        self.assertIn("Jun 25 – Jul 6", reason)

    def test_a_day_off_breaks_the_run(self):
        d0 = date(2026, 6, 2)
        dates = self._dates(d0, 6) + self._dates(d0 + timedelta(days=7), 6)
        rows = [row(id=1, name="Rested", legs=30, worked=12, avail=14,
                    worked_dates=dates)]
        self.assertEqual(build_insights(rows, 30)["exceptions"], [])


class NeverDroveExceptionTests(SimpleTestCase):
    def test_fires_with_peer_comparison(self):
        rows = [
            row(id=1, name="Ghost", avail=10),
            row(id=2, name="Busy", legs=20, worked=10, avail=12),
        ]
        out = build_insights(rows, 30)
        self.assertIn(("Ghost", "never_drove"), exception_rules(out))
        reason = next(e["reason"] for e in out["exceptions"] if e["name"] == "Ghost")
        self.assertIn("Available 10 days, drove none", reason)
        self.assertIn("averaged 20 trips", reason)

    def test_availability_floor_scales_with_window(self):
        rows = [row(id=1, name="Ghost", avail=4),
                row(id=2, name="Busy", legs=20, worked=10, avail=12)]
        # 4 available days meets the 7-day floor (3) but not the 30-day floor (6).
        self.assertIn(("Ghost", "never_drove"),
                      exception_rules(build_insights(rows, 7)))
        self.assertNotIn(("Ghost", "never_drove"),
                         exception_rules(build_insights(rows, 30)))


class MostlyIdleExceptionTests(SimpleTestCase):
    def test_fires_when_clearly_below_peers(self):
        rows = [
            row(id=1, name="Quiet", legs=5, worked=4, avail=20),     # share 20%
            row(id=2, name="BusyA", legs=30, worked=16, avail=20),   # share 80%
            row(id=3, name="BusyB", legs=32, worked=18, avail=20),   # share 90%
        ]
        out = build_insights(rows, 30)
        self.assertIn(("Quiet", "ft_mostly_idle"), exception_rules(out))
        reason = next(e["reason"] for e in out["exceptions"] if e["name"] == "Quiet")
        self.assertIn("Drove 4 of 20 available days", reason)
        self.assertIn("17 of 20", reason)     # peer average

    def test_uniformly_quiet_fleet_fires_nothing(self):
        """Everyone at 30%: relative condition keeps the rule honest."""
        rows = [
            row(id=1, name="A", legs=6, worked=6, avail=20),
            row(id=2, name="B", legs=6, worked=6, avail=20),
            row(id=3, name="C", legs=6, worked=6, avail=20),
        ]
        self.assertEqual(build_insights(rows, 30)["exceptions"], [])

    def test_small_cohort_needs_the_extreme_absolute(self):
        rows = [
            row(id=1, name="Quiet", legs=5, worked=9, avail=20),     # share 45%
            row(id=2, name="Busy", legs=30, worked=18, avail=20),
        ]
        # Only one peer: 45% is not extreme enough (needs ≤ a third).
        self.assertNotIn(("Quiet", "ft_mostly_idle"),
                         exception_rules(build_insights(rows, 30)))
        rows[0] = row(id=1, name="Quiet", legs=5, worked=5, avail=20)  # share 25%
        self.assertIn(("Quiet", "ft_mostly_idle"),
                      exception_rules(build_insights(rows, 30)))


class PackedHarderExceptionTests(SimpleTestCase):
    def test_fires_when_days_are_half_again_heavier(self):
        rows = [
            row(id=1, name="Packed", legs=60, worked=10, avail=12),  # 6.0 per day
            row(id=2, name="EvenA", legs=30, worked=10, avail=12),   # 3.0
            row(id=3, name="EvenB", legs=30, worked=10, avail=12),   # 3.0
        ]
        out = build_insights(rows, 30)
        self.assertIn(("Packed", "days_packed_harder"), exception_rules(out))
        reason = next(e["reason"] for e in out["exceptions"] if e["name"] == "Packed")
        self.assertIn("Averages 6 trips", reason)
        self.assertIn("averages 3", reason)

    def test_silent_below_the_relative_bar(self):
        rows = [
            row(id=1, name="Warm", legs=40, worked=10, avail=12),    # 4.0 per day
            row(id=2, name="EvenA", legs=30, worked=10, avail=12),
            row(id=3, name="EvenB", legs=30, worked=10, avail=12),
        ]
        self.assertNotIn(("Warm", "days_packed_harder"),
                         exception_rules(build_insights(rows, 30)))

    def test_absolute_margin_required_when_median_is_low(self):
        """1.5× a tiny median is still a tiny number — the +1.5 floor stops that."""
        rows = [
            row(id=1, name="Mild", legs=20, worked=10, avail=12),    # 2.0 per day
            row(id=2, name="EvenA", legs=12, worked=10, avail=12),   # 1.2
            row(id=3, name="EvenB", legs=12, worked=10, avail=12),   # 1.2
        ]
        self.assertNotIn(("Mild", "days_packed_harder"),
                         exception_rules(build_insights(rows, 30)))


class WidespreadZeroTripsCollapseTests(SimpleTestCase):
    def _roster(self, total, zero):
        rows = [row(id=i, name=f"Ghost{i}", avail=10) for i in range(zero)]
        rows += [row(id=100 + i, name=f"Busy{i}", legs=20, worked=10, avail=12)
                 for i in range(total - zero)]
        return rows

    def test_a_third_of_the_roster_collapses_into_one_finding(self):
        """23 'never drove' rows are not 23 conversations — they are one fleet fact."""
        out = build_insights(self._roster(total=12, zero=6), 30)
        self.assertNotIn("never_drove", [e["rule"] for e in out["exceptions"]])
        f = next(f for f in out["findings"] if f["id"] == "widespread_zero_trips")
        self.assertIn("6 of the 12 chauffeurs", f["text"])

    def test_a_few_zero_trip_drivers_stay_individual(self):
        out = build_insights(self._roster(total=12, zero=3), 30)
        self.assertEqual(
            len([e for e in out["exceptions"] if e["rule"] == "never_drove"]), 3)
        self.assertNotIn("widespread_zero_trips", finding_ids(out))

    def test_other_rules_survive_the_collapse(self):
        rows = self._roster(total=12, zero=6)
        d0 = date(2026, 6, 2)
        rows.append(row(id=200, name="Marathon", legs=30, worked=12, avail=14,
                        worked_dates=[d0 + timedelta(days=i) for i in range(11)]))
        out = build_insights(rows, 30)
        self.assertEqual([e["rule"] for e in out["exceptions"]],
                         ["no_day_off_streak"])


class ExceptionListShapeTests(SimpleTestCase):
    def test_one_entry_per_driver_streak_wins(self):
        d0 = date(2026, 6, 2)
        dates = [d0 + timedelta(days=i) for i in range(12)]
        rows = [
            # Matches both the streak rule and days-packed-harder.
            row(id=1, name="Both", legs=72, worked=12, avail=14, worked_dates=dates),
            row(id=2, name="EvenA", legs=30, worked=10, avail=12),
            row(id=3, name="EvenB", legs=30, worked=10, avail=12),
        ]
        out = build_insights(rows, 30)
        both = [e for e in out["exceptions"] if e["name"] == "Both"]
        self.assertEqual(len(both), 1)
        self.assertEqual(both[0]["rule"], "no_day_off_streak")

    def test_ordering_is_rule_priority_then_magnitude(self):
        d0 = date(2026, 6, 2)
        rows = [
            row(id=1, name="Ghost", avail=10),
            row(id=2, name="Marathon", legs=30, worked=12, avail=14,
                worked_dates=[d0 + timedelta(days=i) for i in range(11)]),
            row(id=3, name="GhostLonger", avail=20),
            row(id=4, name="Busy", legs=40, worked=20, avail=22),
        ]
        out = build_insights(rows, 30)
        names = [e["name"] for e in out["exceptions"]]
        self.assertEqual(names, ["Marathon", "GhostLonger", "Ghost"])

    def test_entries_carry_what_the_template_needs(self):
        rows = [row(id=7, name="Ghost", avail=10),
                row(id=8, name="Busy", legs=20, worked=10, avail=12)]
        e = build_insights(rows, 30)["exceptions"][0]
        for key in ("driver_id", "name", "initials", "color",
                    "employment_type", "employment_label", "rule", "reason"):
            self.assertIn(key, e)
        self.assertNotIn("_magnitude", e)


class FiredPairsTests(SimpleTestCase):
    """``fired`` is the episode ground truth for dismissal spending: pre-collapse,
    and including rules outranked by a higher-priority one."""

    def test_fired_includes_outranked_rules(self):
        d0 = date(2026, 6, 2)
        rows = [
            # Streak wins the listing, but the density rule also matched.
            row(id=1, name="Both", legs=72, worked=12, avail=14,
                worked_dates=[d0 + timedelta(days=i) for i in range(12)]),
            row(id=2, name="EvenA", legs=30, worked=10, avail=12),
            row(id=3, name="EvenB", legs=30, worked=10, avail=12),
        ]
        out = build_insights(rows, 30)
        self.assertEqual(len([e for e in out["exceptions"]
                              if e["name"] == "Both"]), 1)
        self.assertIn((1, "no_day_off_streak"), out["fired"])
        self.assertIn((1, "days_packed_harder"), out["fired"])

    def test_fired_survives_the_collapse(self):
        rows = [row(id=i, name=f"Ghost{i}", avail=10) for i in range(6)]
        rows += [row(id=100 + i, name=f"Busy{i}", legs=20, worked=10, avail=12)
                 for i in range(6)]
        out = build_insights(rows, 30)
        self.assertNotIn("never_drove", [e["rule"] for e in out["exceptions"]])
        for i in range(6):
            self.assertIn((i, "never_drove"), out["fired"])

    def test_no_rules_no_fired_pairs(self):
        rows = [row(id=1, name="Fine", legs=40, worked=20, avail=22)]
        self.assertEqual(build_insights(rows, 30)["fired"], [])
