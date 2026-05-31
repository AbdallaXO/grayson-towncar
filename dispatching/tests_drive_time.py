"""Tests for scheduler.resolve_drive_minutes — live distance for unknown routes only."""
from unittest.mock import patch
from django.test import TestCase

from dispatching.scheduler import resolve_drive_minutes, get_drive_time, preload_timing_cache

MAPS = "drivers.utils.get_drive_time"  # the real Google Distance Matrix helper (imported inside resolver)


class ResolveDriveMinutesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # empty RouteTimingMetric in test DB -> category get_drive_time falls back to the
        # hardcoded table without per-call DB hits.
        preload_timing_cache()

    def test_known_route_uses_table_no_api_call(self):
        # Both endpoints are known Orlando landmarks -> instant table estimate, no Maps call.
        with patch(MAPS) as m:
            mins = resolve_drive_minutes("Orlando International Airport", "Disney Contemporary",
                                         "MCO Terminal", "Disney Resort")
        m.assert_not_called()
        self.assertEqual(mins, get_drive_time("MCO Terminal", "Disney Resort"))  # 30 from the table

    def test_unknown_route_uses_live_distance(self):
        # A Tampa address (-> 'Other') triggers the live, traffic-aware Maps lookup.
        # USE_LIVE_DISTANCE defaults OFF in prod (see scheduler.py) — force it on here so
        # the live branch is exercised regardless of the default.
        with patch("dispatching.scheduler.USE_LIVE_DISTANCE", True), \
                patch(MAPS, return_value={"duration_seconds": 5400}) as m:  # 90 min
            mins = resolve_drive_minutes("123 Bayshore Blvd, Tampa FL", "Disney Contemporary",
                                         "Other", "Disney Resort")
        m.assert_called_once()
        self.assertEqual(mins, 90)  # 5400s -> 90 min, NOT the ~35 table guess

    def test_unknown_route_falls_back_when_api_fails(self):
        # Maps unavailable/failed -> fall back to the category estimate, never crash.
        with patch("dispatching.scheduler.USE_LIVE_DISTANCE", True), \
                patch(MAPS, return_value=None) as m:
            mins = resolve_drive_minutes("123 Somewhere", "456 Elsewhere", "Other", "Residential")
        m.assert_called_once()
        self.assertEqual(mins, get_drive_time("Other", "Residential"))

    def test_disabled_flag_uses_table(self):
        with patch("dispatching.scheduler.USE_LIVE_DISTANCE", False), patch(MAPS) as m:
            mins = resolve_drive_minutes("Tampa FL", "Disney", "Other", "Disney Resort")
        m.assert_not_called()
        self.assertEqual(mins, get_drive_time("Other", "Disney Resort"))

    def test_other_hotel_endpoint_triggers_live(self):
        # 'Other Hotel' is in the unknown set (a hotel keyword can match a Tampa hotel).
        with patch("dispatching.scheduler.USE_LIVE_DISTANCE", True), \
                patch(MAPS, return_value={"duration_seconds": 3600}) as m:  # 60 min
            mins = resolve_drive_minutes("MCO", "Some Far Hotel", "MCO Terminal", "Other Hotel")
        m.assert_called_once()
        self.assertEqual(mins, 60)
