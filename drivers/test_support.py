"""Shared test helpers for the regular-shift work (structured shifts, Stage 1).

Django's LocMemCache and dispatching.models._settings_cache both outlive the
per-test DB rollback, so a switch value or a template list cached by one test
would leak into the next. Every test class that touches regular shifts mixes
RegularShiftCacheMixin in, ahead of TestCase:

    class MyTests(RegularShiftCacheMixin, TestCase): ...

A subclass that defines its own setUp/tearDown must call super().
"""
from django.core.cache import cache

from dispatching.models import SchedulerSettings
from drivers.models import SHIFT_TEMPLATES_CACHE_KEY


def clear_regular_shift_caches():
    """Drop the cached settings row, the cached switch and the cached templates.
    SchedulerSettings.clear_cache() deletes the switch key itself."""
    SchedulerSettings.clear_cache()
    cache.delete(SHIFT_TEMPLATES_CACHE_KEY)


class RegularShiftCacheMixin:
    """Starts and ends every test with the regular-shift caches empty."""

    def setUp(self):
        super().setUp()
        clear_regular_shift_caches()

    def tearDown(self):
        clear_regular_shift_caches()
        super().tearDown()
