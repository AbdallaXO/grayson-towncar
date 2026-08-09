"""The turn-buffer control must actually RENDER on both build surfaces.

Founder, 2026-08-09: "for when i am doing auto-assign all let me do the mode too, its only
for driver by driver schedule". It was in fact wired to both — he was looking at a cached
template — but a knob that exists only in the payload is a knob nobody can reach, and
nothing was stopping a future template edit from dropping one of them silently.

Both live in dispatching/templates/dispatching/daily_capacity_planner.html:
  * sbBufferMode / sbBufferCustom — Schedule Builder (one driver at a time)
  * aaBufferMode / aaBufferCustom — Auto-Assign All
"""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from dispatching.models import SchedulerSettings

DAY = date(2026, 8, 10)


class BufferControlRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="buf_dispatcher", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)
        # SchedulerSettings caches the singleton at MODULE level, which outlives the
        # per-test transaction rollback: a row created/edited inside this test vanishes on
        # rollback while the cached Python object keeps pointing at it, and the next test
        # class gets a stale instance. Clear on both sides.
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)

    def _page(self):
        resp = self.client.get(reverse("capacity_planner") + f"?date={DAY.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_schedule_builder_has_the_control(self):
        html = self._page()
        self.assertIn('id="sbBufferMode"', html)
        self.assertIn('id="sbBufferCustom"', html)

    def test_auto_assign_all_has_the_control(self):
        """The one the founder went looking for and could not find."""
        html = self._page()
        self.assertIn('id="aaBufferMode"', html)
        self.assertIn('id="aaBufferCustom"', html)

    def test_both_offer_the_same_named_modes(self):
        html = self._page()
        for label in ("Aggressive", "Standard", "Relaxed", "Custom"):
            self.assertGreaterEqual(
                html.count(label), 2,
                f"'{label}' should appear on BOTH the builder and auto-assign controls")

    def test_the_default_is_shown_so_use_default_is_not_a_mystery(self):
        cfg = SchedulerSettings.get_settings()
        cfg.min_turn_buffer = 7
        cfg.save()
        SchedulerSettings.clear_cache()
        html = self._page()
        self.assertGreaterEqual(html.count("Use default (7 min)"), 2)

    def test_both_payloads_send_min_buffer(self):
        """Guards the JS wiring, not just the markup — the control has to reach the API."""
        html = self._page()
        self.assertIn("readBufferControl('sbBufferMode', 'sbBufferCustom')", html)
        self.assertIn("readBufferControl('aaBufferMode', 'aaBufferCustom')", html)
        # auto-assign posts twice: preview and apply. Both must carry it.
        self.assertGreaterEqual(
            html.count("readBufferControl('aaBufferMode', 'aaBufferCustom')"), 2,
            "auto-assign preview AND apply must both send min_buffer")
