"""Vehicle bookings, dispatch side: what the pages print and load.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_bookings_ui

The server half (409 until the dispatcher answers, hard never overridable) is
pinned in tests_fleet_bookings. This module pins the other half — that every
page a dispatcher assigns from actually loads the one booking question
(static/js/booking-prompt.js), and that a booking tag reads the way the spec
asks: a soft booking no trip sits inside is plain information, a trip inside
one is a ⚠, a hard one is a lock — with fleet's free-text reason escaped.

Trip ends are pinned to pickup + 90 minutes (the tests_fleet_day trick): a
9:00 trip occupies 9:00–10:30.
"""
import re
from datetime import time, timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils import timezone

from dispatching.tests_fleet_bookings import _BookingFixture
from dispatching.tests_fleet_day import _ninety_minutes
from dispatching.tests_fleet_desk import DAY

PROMPT_JS = "js/booking-prompt.js"


def _tag(html, cls_fragment, icon, text):
    """The rendered booking tag with this class list, icon and visible text."""
    pattern = (r'<span class="(?P<cls>[^"]*)"[^>]*>\s*<i class="bi bi-(?P<icon>[\w-]+)"></i>'
               + re.escape(text) + r'</span>')
    for m in re.finditer(pattern, html):
        if m.group("cls") == cls_fragment and m.group("icon") == icon:
            return m.group(0)
    return None


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class BookingTagTests(_BookingFixture):
    """Three cars, three readings: #6 soft with its holder's 9:00 trip inside
    (⚠), #7 soft with nothing on it (calendar, calm), #8 hard (lock)."""

    def setUp(self):
        super().setUp()
        self.u6, self.u7, self.u8 = self.unit("6"), self.unit("7"), self.unit("8")
        self.d6, self.d8 = self.driver("tag_six"), self.driver("tag_eight")
        self.hold(self.u6, self.d6)
        self.hold(self.u8, self.d8)
        self.job(self.d6, 9)                      # 9:00–10:30, inside #6's 10–12
        self.job(self.d8, 15)                     # clear of #8's 10–12
        self.book(self.u6, time(10, 0), time(12, 0), reason="Tire <b>service</b>")
        self.book(self.u7, time(14, 0), time(15, 0), reason="Detailing")
        self.book(self.u8, time(10, 0), time(12, 0), hard=True, reason="Lift")

    def get(self, name):
        resp = self.client.get(reverse(name), {"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def assert_three_readings(self, html, base, window_soft, window_calm, window_hard):
        self.assertIsNotNone(
            _tag(html, f"{base} conflict", "exclamation-triangle-fill",
                 f"Booked {window_soft} · Tire &lt;b&gt;service&lt;/b&gt;"),
            "a soft booking with a trip inside it reads as a ⚠ conflict")
        self.assertIsNotNone(
            _tag(html, base, "calendar-event", f"Booked {window_calm} · Detailing"),
            "a soft booking nothing lands on reads as plain information")
        self.assertIsNotNone(
            _tag(html, f"{base} hard", "lock-fill", f"Hard-booked {window_hard} · Lift"),
            "a hard booking carries the lock")
        # Fleet's reason is free text any staff member can type.
        self.assertNotIn("<b>service</b>", html)

    def test_the_legs_dashboard_pool_and_assigned_cars(self):
        html = self.get("dashboard")
        self.assertIn(PROMPT_JS, html)
        self.assert_three_readings(html, "va-book-tag", "10:00 AM–12:00 PM",
                                   "2:00 PM–3:00 PM", "10:00 AM–12:00 PM")
        # The car a chauffeur holds carries its bookings under the chip too.
        lines = re.findall(r'<div class="va-book-line">(.*?)</div>', html, re.S)
        self.assertTrue(any("Tire &lt;b&gt;service" in line and "conflict" in line for line in lines))
        self.assertTrue(any("Hard-booked" in line and "lock-fill" in line for line in lines))

    def test_the_planner_pool_and_assigned_cars(self):
        html = self.get("capacity_planner")
        self.assert_three_readings(html, "va-book-tag", "10:00 AM–12:00 PM",
                                   "2:00 PM–3:00 PM", "10:00 AM–12:00 PM")
        self.assertIn('<div class="va-book-line">', html)
        # The question is on the page before the drag-drop that asks it.
        self.assertLess(html.index(PROMPT_JS), html.index("js/timeline-dnd.js"))

    def test_the_schedule_board_row(self):
        html = self.get("schedule_board")
        self.assertIn(PROMPT_JS, html)
        self.assertIsNotNone(_tag(html, "veh-book-tag conflict", "exclamation-triangle-fill",
                                  "Booked 10a–12p · Tire &lt;b&gt;service&lt;/b&gt;"))
        self.assertIsNotNone(_tag(html, "veh-book-tag hard", "lock-fill", "Hard-booked 10a–12p · Lift"))
        self.assertNotIn("<b>service</b>", html)


@patch("dispatching.scheduler.estimate_job_end_time", _ninety_minutes)
class EveryAssignPageAsksTests(_BookingFixture):
    """A page that can put a trip or a car on a chauffeur without loading the
    question would write a soft clash with no Continue / Cancel — or, since
    the server now answers 409 until asked, fail with no way through."""

    def setUp(self):
        super().setUp()
        self.leg_on_day = self.leg(DAY, hour=9)

    def assert_asks(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PROMPT_JS)

    def test_legs_list(self):
        self.assert_asks(self.client.get(reverse("legs_list")))

    def test_reservation_view(self):
        self.assert_asks(self.client.get(
            reverse("reservation_details", args=[self.leg_on_day.reservation.uuid])))

    def test_swap_tester(self):
        self.assert_asks(self.client.get(reverse("swap_tester"), {"date": DAY.isoformat()}))

    def test_legs_dashboard_driver_dropdown_answers_first(self):
        html = self.client.get(reverse("dashboard"), {"date": DAY.isoformat()}).content.decode()
        # The dropdown reads the booking answer from the schedule check and
        # puts the select back on every refusal.
        self.assertIn("fData.hard_block", html)
        self.assertIn("fData.booking_warning", html)
        self.assertIn("restoreSelect(selectEl)", html)
        # A car dragged between chauffeurs goes to the receiver first.
        self.assertIn("from_driver_id", html)


class BookingPromptAssetTests(_BookingFixture):
    """The shared script itself: found by the static finders, worded the
    founder's way, and never touching Bootstrap while the page is loading
    (Bootstrap loads after the page's content)."""

    def read(self, rel):
        path = finders.find(rel)
        self.assertIsNotNone(path, f"{rel} is not collectable")
        return Path(path).read_text(encoding="utf-8")

    def test_the_prompt_script(self):
        js = self.read(PROMPT_JS)
        for words in ("Vehicle booked by fleet", "Car hard-booked by fleet",
                      "Continue Anyway", "Cancel", "override_booking",
                      "window.BookingPrompt", "postWithBookingPrompt"):
            self.assertIn(words, js)
        # Server text is put in as text, never markup.
        self.assertIn(".bp-text').textContent", js)
        # window.bootstrap is read inside the asking function only (comments
        # aside — the header explains exactly this).
        code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        before, _, rest = code.partition("function askNow")
        self.assertNotIn("bootstrap", before)
        self.assertIn("window.bootstrap", rest.split("function enqueue", 1)[0])

    def test_the_board_drag_answers_once(self):
        js = self.read("js/timeline-dnd.js")
        self.assertIn("override_booking", js)
        self.assertIn("Vehicle booked by fleet", js)
        self.assertIn("booking_warning", js)
