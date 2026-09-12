"""Picking a date on the capacity planner has to land on that date.

Founder, 2026-09-08: "sometimes i select a date and it does not jump to it or open to
that date on the capacity planner."

Three things were wrong with the date box in the planner header, all of them
intermittent, which is why it only happened "sometimes":

  1. The handler fired on every completed edit of the box. Typing 09/12 is a valid
     09/01 the instant the "1" lands, so keyboard entry left for the 1st.
  2. Browsers restore the typed value of a date box across a reload or a Back, so the
     box could show one day while the page under it showed another. The handler
     compared the box against its own restored value, so re-picking the day you wanted
     did nothing at all — and the ten buttons that read the box to decide which day
     they act on (Suggest Day Setup, Reset All, Save, the schedule builder) were aimed
     at a day nobody was looking at.
  3. The page takes seconds to rebuild with nothing on screen saying so.

data-server-date is the fix for (2) and the thing worth guarding: it is the only
element on the page that states, in the markup, which day the server actually built.
"""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

DAY = date(2026, 9, 12)


class PlannerDatePickerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="picker_dispatcher", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _page(self, day=DAY):
        resp = self.client.get(reverse("capacity_planner") + f"?date={day.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_the_page_opens_on_the_date_that_was_asked_for(self):
        html = self._page()
        self.assertIn('value="2026-09-12"', html)
        self.assertIn("Saturday, September 12, 2026", html)

    def test_the_box_states_which_day_the_server_built(self):
        """Without this the browser's restored value wins and the box lies."""
        html = self._page()
        self.assertIn('data-server-date="2026-09-12"', html)

    def test_the_browser_is_told_not_to_restore_a_stale_value(self):
        html = self._page()
        tag = html[html.index('id="datePicker"'):][:400]
        tag = tag[:tag.index('>')]
        self.assertIn('autocomplete="off"', tag)

    def test_navigation_compares_against_the_server_date_not_the_box(self):
        html = self._page()
        self.assertIn("getAttribute('data-server-date')", html)
        self.assertIn("wanted === serverDate", html)

    def test_typing_a_date_is_debounced_so_09_12_never_leaves_for_09_01(self):
        html = self._page()
        self.assertIn("setTimeout(goToPickedDate", html)
