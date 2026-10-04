"""Tests for the driver's own time-off screens: the request form, My Requests,
and the approve/deny text — mostly that the wording says plainly which hours
are OFF, since "From / To" alone left drivers guessing.

Run with:  ./manage.py test drivers.tests_timeoff
"""
from datetime import time, timedelta
from html import unescape
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from business.datefmt import strf
from drivers.models import Driver, DriverDateOverride
from drivers.timeoff_notifications import notify_driver_of_decision


def _day(d):
    """The driver screens' date wording: 'Tue, Oct 6' (year only when not this one)."""
    label = strf(d, "%a, %b %-d")
    if d.year != timezone.localdate().year:
        label += f", {d.year}"
    return label


class TimeOffWordingTestBase(TestCase):
    def setUp(self):
        user = User.objects.create_user("timeoff_driver", first_name="Tim", last_name="Off")
        self.driver = Driver.objects.create(profile=user, driver_type="inhouse")
        self.client.force_login(user)
        self.day = timezone.localdate() + timedelta(days=2)

    def _override(self, **kwargs):
        defaults = dict(
            driver=self.driver, date=self.day, status="pending", submitted_by_driver=True,
        )
        defaults.update(kwargs)
        return DriverDateOverride.objects.create(**defaults)

    def _hours_off(self, **kwargs):
        return self._override(
            exception_type="unavailable_window",
            start_time=time(9, 0), end_time=time(13, 0), **kwargs,
        )

    def _post_hours(self, start="09:00", end="13:00"):
        return self.client.post(reverse("driver_request_timeoff"), {
            "kind": "partial_day", "start_date": self.day.isoformat(),
            "start_time": start, "end_time": end, "reason": "appointment",
        }, follow=True)


class RequestFormWordingTests(TimeOffWordingTestBase):
    def test_form_says_which_hours_are_off(self):
        html = unescape(self.client.get(reverse("driver_request_timeoff")).content.decode())
        self.assertIn("Request time off", html)
        self.assertIn("A few hours off", html)
        self.assertIn("I can't work from", html)
        self.assertIn("Send request", html)
        self.assertIn('id="timeoffSummary"', html)
        self.assertNotIn("Time you can't work", html)

    def test_active_request_box_uses_plain_status_and_hours(self):
        self._hours_off()
        html = unescape(self.client.get(reverse("driver_request_timeoff")).content.decode())
        self.assertIn(_day(self.day), html)
        self.assertIn("off 9 AM – 1 PM", html)
        self.assertIn("waiting for approval", html)
        self.assertNotIn("pending review", html)


class RequestSubmitTests(TimeOffWordingTestBase):
    def test_cant_work_from_9_to_1_is_saved_as_hours_off(self):
        """The whole point of the wording: the two times are the hours the
        driver is OFF, not the hours they can work."""
        self._post_hours("09:00", "13:00")
        o = DriverDateOverride.objects.get(driver=self.driver)
        self.assertEqual(o.exception_type, "unavailable_window")
        self.assertEqual((o.start_time, o.end_time), (time(9, 0), time(13, 0)))
        self.assertEqual(o.status, "pending")

    def test_sent_message_is_plain(self):
        html = unescape(self._post_hours().content.decode())
        self.assertIn("Request sent.", html)

    def test_backwards_times_explain_the_fix(self):
        html = unescape(self._post_hours("13:00", "09:00").content.decode())
        self.assertIn("Check the times: the second one has to be later than the first.", html)
        self.assertFalse(DriverDateOverride.objects.exists())

    def test_duplicate_hours_request_says_it_is_already_waiting(self):
        self._hours_off()
        html = unescape(self._post_hours().content.decode())
        self.assertIn(
            f"You already sent a request for {_day(self.day)} (off 9 AM – 1 PM). "
            "It's waiting for approval, so no need to send it again.",
            html,
        )
        self.assertEqual(DriverDateOverride.objects.count(), 1)

    def test_duplicate_whole_day_request_says_it_is_already_approved(self):
        self._override(exception_type="off", status="approved")
        html = unescape(self.client.post(reverse("driver_request_timeoff"), {
            "kind": "full_day", "start_date": self.day.isoformat(), "reason": "day_off",
        }, follow=True).content.decode())
        self.assertIn(
            f"You already sent a request for {_day(self.day)} (off all day). "
            "It's already approved, so no need to send it again.",
            html,
        )


class MyRequestsWordingTests(TimeOffWordingTestBase):
    def test_cards_say_off_and_waiting_for_approval(self):
        self._hours_off()
        self._override(exception_type="off", date=self.day + timedelta(days=1), status="approved")
        html = unescape(self.client.get(reverse("driver_my_timeoff_requests")).content.decode())
        self.assertIn("Off 9 AM – 1 PM", html)
        self.assertIn("Off all day", html)
        self.assertIn(_day(self.day), html)
        self.assertIn("Waiting for approval", html)
        self.assertIn("1 waiting for approval", html)
        self.assertNotIn("Pending review", html)
        self.assertNotIn("pending dispatch review", html)


class DecisionTextTests(TimeOffWordingTestBase):
    def test_approval_text_says_the_hours_are_off(self):
        self.driver.phone_number = "+14075550100"
        self.driver.save(update_fields=["phone_number"])
        o = self._hours_off(status="approved")
        with mock.patch("drivers.timeoff_notifications._send") as send:
            notify_driver_of_decision(o)
        body = send.call_args[0][1]
        self.assertIn("(off 9:00 AM - 1:00 PM)", body)
