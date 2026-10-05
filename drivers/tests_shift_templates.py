"""Drivers -> Regular Shifts -> Shift Templates (structured shifts, Stage 1,
Task 8): the four shapes (Morning, Midday, Evening, Float) with their usual
leave and return times, the longest shift each may run, and notes. Every staff
user may look; only a manager (is_superuser) saves (S10). The longest shift is
never more than 12 hours, and never below a confirmed regular day on that
shape.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_shift_templates
"""
import re
from datetime import time
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from dispatching import day_setup
from drivers import regular_shifts as rs
from drivers.models import (
    SHIFT_TEMPLATES_CACHE_KEY,
    Driver,
    DriverWeeklySchedule,
    ShiftTemplate,
)
from drivers.regular_shifts import DayShift
from drivers.test_support import RegularShiftCacheMixin

_FIELDS = ("start_earliest", "start_latest", "end_earliest", "end_latest")
_seq = 0


def _driver(first, last, **kw):
    global _seq
    _seq += 1
    user = User.objects.create_user(username=f"st_{first.lower()}_{_seq}",
                                    first_name=first, last_name=last)
    kw.setdefault("driver_type", "inhouse")
    return Driver.objects.create(profile=user, **kw)


class ShiftTemplatePageTests(RegularShiftCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("st_manager", password="x",
                                               is_staff=True, is_superuser=True)
        cls.dispatcher = User.objects.create_user("st_dispatcher", password="x", is_staff=True)

    def setUp(self):
        super().setUp()
        self.url = reverse("shift_templates")
        self.t = {t.kind: t for t in ShiftTemplate.objects.all()}
        # Day Setup leaves driver id 6 out by id; a test driver may get that id.
        no_ids = mock.patch.object(day_setup, "DAY_SETUP_EXCLUDE_DRIVER_IDS", set())
        no_ids.start()
        self.addCleanup(no_ids.stop)

    # ── helpers ──
    def payload(self, **changes):
        """The page's POST as the browser sends it: every shape as stored, then
        per kind the fields to change (times as "HH:MM", max_span_hours as
        typed)."""
        shapes = list(ShiftTemplate.objects.all())
        data = {"shapes-TOTAL_FORMS": str(len(shapes)),
                "shapes-INITIAL_FORMS": str(len(shapes)),
                "shapes-MIN_NUM_FORMS": "0", "shapes-MAX_NUM_FORMS": "1000"}
        for i, t in enumerate(shapes):
            row = {"id": str(t.id), "max_span_hours": f"{t.max_span_minutes / 60:g}",
                   "notes": t.notes,
                   **{name: getattr(t, name).strftime("%H:%M") for name in _FIELDS}}
            row.update(changes.get(t.kind, {}))
            for name, value in row.items():
                data[f"shapes-{i}-{name}"] = value
        return data

    def post(self, user=None, **changes):
        self.client.force_login(user or self.manager)
        return self.client.post(self.url, self.payload(**changes))

    @staticmethod
    def forms_by_kind(resp):
        return {f.instance.kind: f for f in resp.context["formset"].forms}

    def stored(self, kind):
        return ShiftTemplate.objects.get(kind=kind)

    def confirm(self, driver, days, role="morning"):
        rs.save_regular_shift(driver, days, self.manager, role_template_id=self.t[role].id)

    def morning(self, day, start, end, **extra):
        return DayShift(day, self.t["morning"].id, start, end, **extra)

    # ── looking ──
    def test_staff_sees_read_only(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for text in ("Shift Templates", "Morning", "Midday", "Evening", "Float",
                     "These are targets, not limits — a regular shift outside them is "
                     "allowed with a warning.", "Longest shift (hours)",
                     "Only a manager can change these."):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn("<fieldset disabled", html)
        self.assertNotIn('id="save-shapes"', html)
        self.assertEqual([f.instance.kind for f in resp.context["formset"].forms],
                         ["morning", "midday", "evening", "float"])
        self.assertFalse(resp.context["can_manage"])
        # A manager gets the same page, editable.
        self.client.force_login(self.manager)
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("<fieldset disabled", html)
        self.assertIn('id="save-shapes"', html)

    def test_page_never_says_template_window_or_stub(self):
        self.client.force_login(self.manager)
        html = self.client.get(self.url).content.decode()
        main = html[html.index('class="rs-page"'):]
        main = re.sub(r"<script\b.*?</script>|<!--.*?-->", " ", main, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", main)             # visible words only
        text = text.replace("Shift Templates", "")      # the page title may
        for word in ("template", "window", "stub"):
            with self.subTest(word=word):
                self.assertNotIn(word, text.lower())

    def test_only_staff(self):
        self.client.force_login(User.objects.create_user("st_guest", password="x"))
        self.assertEqual(self.client.get(self.url).status_code, 302)
        resp = self.client.post(self.url, self.payload(morning={"start_latest": "06:30"}))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.stored("morning").start_latest, time(6, 0))

    def test_initial_shows_hours_not_minutes(self):
        ShiftTemplate.objects.filter(kind="midday").update(max_span_minutes=690)
        self.client.force_login(self.manager)
        resp = self.client.get(self.url)
        forms = self.forms_by_kind(resp)
        self.assertEqual(forms["morning"]["max_span_hours"].initial, 12)
        self.assertEqual(forms["midday"]["max_span_hours"].initial, 11.5)
        html = resp.content.decode()
        morning = forms["morning"]["max_span_hours"]
        self.assertRegex(html, r'<input[^>]*name="%s"[^>]*value="12"' % morning.html_name)
        self.assertRegex(html, r'<input[^>]*name="%s"[^>]*step="0\.5"' % morning.html_name)

    def test_used_by_counts_usual_shift(self):
        amy, bob = _driver("Amy", "Alpha"), _driver("Bob", "Bravo")
        cat = _driver("Cat", "Charlie")
        _driver("Dan", "Delta")                                    # no usual shift yet
        self.confirm(amy, [], role="morning")
        self.confirm(bob, [], role="morning")
        self.confirm(cat, [], role="float")
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.url)
        used = {card["kind"]: card["used_by"] for card in resp.context["cards"]}
        self.assertEqual(used, {"morning": 2, "midday": 0, "evening": 0, "float": 1})
        html = resp.content.decode()
        self.assertIn("Used by 2 drivers as their usual shift", html)
        self.assertIn("Used by 1 driver as their usual shift", html)
        self.assertIn("Used by no one as their usual shift yet", html)
        self.assertIn(f'href="{reverse("regular_shifts")}"', html)

    def test_regular_shifts_links_here(self):
        self.client.force_login(self.dispatcher)
        html = self.client.get(reverse("regular_shifts")).content.decode()
        self.assertRegex(html, r'href="%s"[^>]*>[^<]*(<i[^>]*></i>)?\s*Shift Templates'
                         % re.escape(self.url))

    def test_navbar_active_on_shift_templates(self):
        link = (r'class="dropdown-item ?(active)?"\s+href="%s"><i class="bi bi-calendar2-check me-2">'
                r'</i>Regular Shifts</a>' % re.escape(reverse("regular_shifts")))
        toggle = r'dropdown-toggle text-white active"[^>]*>\s*<i class="bi bi-people-fill me-1">'
        self.client.force_login(self.dispatcher)
        html = self.client.get(self.url).content.decode()
        self.assertEqual(re.search(link, html).group(1), "active")
        self.assertRegex(html, toggle)

    # ── saving ──
    def test_manager_edits_band(self):
        resp = self.post(morning={"start_latest": "06:30", "end_latest": "16:30",
                                  "notes": "Most leave by 6:30."})
        self.assertRedirects(resp, self.url, fetch_redirect_response=False)
        morning = self.stored("morning")
        self.assertEqual((morning.start_latest, morning.end_latest, morning.notes),
                         (time(6, 30), time(16, 30), "Most leave by 6:30."))
        self.assertEqual(morning.max_span_minutes, 720)
        self.assertEqual(morning.updated_by, self.manager)
        # Shapes nobody changed are left alone.
        self.assertIsNone(self.stored("evening").updated_by)
        page = self.client.get(self.url)
        self.assertEqual([str(m) for m in page.context["messages"]], ["Shift templates saved."])

    def test_manager_sets_longest_shift_in_half_hours(self):
        resp = self.post(evening={"max_span_hours": "11.5"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.stored("evening").max_span_minutes, 690)
        resp = self.post(evening={"max_span_hours": "11.3"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.forms_by_kind(resp)["evening"].errors["max_span_hours"],
                         ["Use whole or half hours, like 11 or 11.5."])
        resp = self.post(evening={"max_span_hours": "11.25"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Use whole or half hours, like 11 or 11.5.",
                      self.forms_by_kind(resp)["evening"].errors["max_span_hours"])
        resp = self.post(evening={"max_span_hours": "0.5"})
        self.assertEqual(self.forms_by_kind(resp)["evening"].errors["max_span_hours"],
                         ["A shift must be at least 1 hour."])
        self.assertEqual(self.stored("evening").max_span_minutes, 690)

    def test_over_12_hours_refused(self):
        resp = self.post(morning={"max_span_hours": "12.5", "start_latest": "06:30"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.forms_by_kind(resp)["morning"].errors["max_span_hours"],
                         ["No shift can be longer than 12 hours."])
        self.assertContains(resp, "No shift can be longer than 12 hours.")
        morning = self.stored("morning")
        self.assertEqual((morning.max_span_minutes, morning.start_latest), (720, time(6, 0)))
        self.assertIsNone(morning.updated_by)

    def test_start_band_backwards_refused(self):
        resp = self.post(midday={"start_earliest": "09:30"})        # latest is 09:00
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.forms_by_kind(resp)["midday"].errors["start_latest"],
                         ["The latest start can't be earlier than the earliest start."])
        self.assertEqual(self.stored("midday").start_earliest, time(6, 0))

    def test_lowering_below_confirmed_shift_refused(self):
        amy, bob = _driver("Amy", "Alpha"), _driver("Bob", "Bravo")
        cat, dan = _driver("Cat", "Charlie"), _driver("Dan", "Delta")
        # Amy: 11h 30m on Monday and Tuesday; Wednesday blank (the usual times).
        self.confirm(amy, [self.morning(0, time(4, 10), time(15, 40)),
                           self.morning(1, time(4, 10), time(15, 40)),
                           self.morning(2, None, None)])
        # Bob: 11h 40m on Friday.
        self.confirm(bob, [self.morning(4, time(4, 0), time(15, 40))])
        # Cat: Sunday may be Morning or Evening; its times are only a note.
        self.confirm(cat, [self.morning(6, time(4, 0), time(16, 0),
                                        alt_template_id=self.t["evening"].id)])
        # Dan's long Saturday was never confirmed.
        DriverWeeklySchedule.objects.create(
            driver=dan, day_of_week=5, shift_template=self.t["morning"],
            shift_start=time(4, 0), shift_end=time(15, 55))

        resp = self.post(morning={"max_span_hours": "11"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.forms_by_kind(resp)["morning"].errors["max_span_hours"], [
            "3 confirmed regular shifts on Morning are longer than that — edit them first.",
            "Longer than 11 hours: Amy Alpha on Monday and Tuesday; Bob Bravo on Friday.",
        ])
        self.assertEqual(self.stored("morning").max_span_minutes, 720)

        resp = self.post(morning={"max_span_hours": "11.5"})        # Amy's 11h 30m fits
        self.assertEqual(self.forms_by_kind(resp)["morning"].errors["max_span_hours"], [
            "1 confirmed regular shift on Morning is longer than that — edit it first.",
            "Longer than 11.5 hours: Bob Bravo on Friday.",
        ])
        self.assertEqual(self.stored("morning").max_span_minutes, 720)

        # Once Bob's Friday is shorter, 11.5 saves.
        self.confirm(bob, [self.morning(4, time(4, 0), time(15, 30))])
        self.assertEqual(self.post(morning={"max_span_hours": "11.5"}).status_code, 302)
        self.assertEqual(self.stored("morning").max_span_minutes, 690)

    def test_lowering_below_confirmed_overnight_shift_refused(self):
        # 2:15 PM to 2:15 AM is 12 hours across midnight, not -12.
        amy = _driver("Amy", "Alpha")
        self.confirm(amy, [DayShift(0, self.t["evening"].id, time(14, 15), time(2, 15))],
                     role="evening")
        resp = self.post(evening={"max_span_hours": "11.5"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.forms_by_kind(resp)["evening"].errors["max_span_hours"], [
            "1 confirmed regular shift on Evening is longer than that — edit it first.",
            "Longer than 11.5 hours: Amy Alpha on Monday.",
        ])
        self.assertEqual(self.stored("evening").max_span_minutes, 720)

    def test_lowering_counts_only_drivers_on_regular_shifts(self):
        # Regular Shifts lists active in-house chauffeurs only, so nobody else
        # can block the change: the manager couldn't find them there.
        gone = _driver("Gus", "Gone")
        self.confirm(gone, [self.morning(0, time(4, 0), time(16, 0))])
        Driver.objects.filter(pk=gone.pk).update(is_active=False)
        partner = _driver("Pat", "Partner", driver_type="affiliate")
        self.confirm(partner, [self.morning(1, time(4, 0), time(16, 0))])
        self.assertEqual(self.post(morning={"max_span_hours": "11"}).status_code, 302)
        self.assertEqual(self.stored("morning").max_span_minutes, 660)

    def test_off_half_hour_value_from_admin_still_saves(self):
        # 715 minutes (set in admin) shows as 12, the nearest half hour, so the
        # page saves; 700 shows as 11.5.
        ShiftTemplate.objects.filter(kind="midday").update(max_span_minutes=715)
        ShiftTemplate.objects.filter(kind="evening").update(max_span_minutes=700)
        self.client.force_login(self.manager)
        forms = self.forms_by_kind(self.client.get(self.url))
        self.assertEqual(forms["midday"]["max_span_hours"].initial, 12)
        self.assertEqual(forms["evening"]["max_span_hours"].initial, 11.5)
        shown = {"midday": {"max_span_hours": "12"}, "evening": {"max_span_hours": "11.5"}}
        # A notes-only change on Morning saves; Midday, untouched, keeps 715.
        resp = self.post(morning={"notes": "Only Morning changed."}, **shown)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.stored("morning").notes, "Only Morning changed.")
        self.assertEqual(self.stored("midday").max_span_minutes, 715)
        self.assertEqual(self.stored("evening").max_span_minutes, 700)
        # A shape that is changed saves the hours it showed.
        resp = self.post(midday={"max_span_hours": "12", "notes": "Midday changed."},
                         evening=shown["evening"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.stored("midday").max_span_minutes, 720)

    def test_longer_longest_shift_that_cuts_rest_refused(self):
        # Morning runs from its start for its longest shift: at 12 hours,
        # Monday's 12 PM start reaches midnight, 6h 30m before Tuesday's 6:30.
        ShiftTemplate.objects.filter(kind="morning").update(max_span_minutes=600)
        rs.clear_template_cache()
        amy = _driver("Amy", "Alpha")
        self.confirm(amy, [self.morning(0, time(12, 0), time(22, 0)),
                           self.morning(1, time(6, 30), time(16, 30))])
        resp = self.post(morning={"max_span_hours": "12"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["formset"].non_form_errors(), [
            "1 driver's confirmed regular shift wouldn't fit this change — edit it on "
            "Regular Shifts first.",
            "Amy Alpha — Monday to Tuesday: only 6h 30m off between shifts; the minimum "
            "is 8h 30m.",
        ])
        self.assertContains(resp, "Monday to Tuesday: only 6h 30m off between shifts")
        self.assertEqual(self.stored("morning").max_span_minutes, 600)

    def test_usual_times_that_leave_no_time_refused(self):
        # Bob is done by 2 PM and his Monday takes Morning's usual times.
        amy = _driver("Amy", "Alpha")
        bob = _driver("Bob", "Bravo", hard_latest_finish=time(14, 0))
        self.confirm(amy, [self.morning(0, None, None)])
        self.confirm(bob, [self.morning(0, None, None), self.morning(2, None, None)])
        resp = self.post(morning={"start_latest": "14:30"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["formset"].non_form_errors(), [
            "1 driver's confirmed regular shift wouldn't fit this change — edit it on "
            "Regular Shifts first.",
            "Bob Bravo — Monday: the start and finish limits leave no time for a shift.",
            "Bob Bravo — Wednesday: the start and finish limits leave no time for a shift.",
        ])
        self.assertEqual(self.stored("morning").start_latest, time(6, 0))
        # Once his days have their own times, the change saves.
        self.confirm(bob, [self.morning(0, time(5, 0), time(13, 0))])
        self.assertEqual(self.post(morning={"start_latest": "14:30"}).status_code, 302)
        self.assertEqual(self.stored("morning").start_latest, time(14, 30))

    def test_problem_a_week_already_had_does_not_block(self):
        # Bob's limit was cut in admin after he was confirmed: his blank Monday
        # already has no time left. That isn't this change's doing.
        bob = _driver("Bob", "Bravo")
        self.confirm(bob, [self.morning(0, None, None)])
        Driver.objects.filter(pk=bob.pk).update(hard_latest_finish=time(3, 0))
        self.assertEqual(self.post(morning={"start_latest": "06:30"}).status_code, 302)
        self.assertEqual(self.stored("morning").start_latest, time(6, 30))

    def test_confirmed_shift_on_another_shape_does_not_count(self):
        amy = _driver("Amy", "Alpha")
        self.confirm(amy, [self.morning(0, time(4, 0), time(15, 55))])
        self.assertEqual(self.post(midday={"max_span_hours": "10"}).status_code, 302)
        self.assertEqual(self.stored("midday").max_span_minutes, 600)

    def test_save_clears_template_cache(self):
        self.assertEqual(rs.templates_by_id()[self.t["morning"].id].max_span_minutes, 720)
        self.assertIsNotNone(cache.get(SHIFT_TEMPLATES_CACHE_KEY))
        self.assertEqual(self.post(morning={"max_span_hours": "11.5"}).status_code, 302)
        self.assertIsNone(cache.get(SHIFT_TEMPLATES_CACHE_KEY))
        self.assertEqual(rs.templates_by_id()[self.t["morning"].id].max_span_minutes, 690)

    def test_dispatcher_post_forbidden(self):
        resp = self.post(self.dispatcher, morning={"start_latest": "06:30",
                                                   "max_span_hours": "11"})
        self.assertEqual(resp.status_code, 403)
        morning = self.stored("morning")
        self.assertEqual((morning.start_latest, morning.max_span_minutes), (time(6, 0), 720))

    def test_page_cannot_add_a_shape(self):
        data = self.payload()
        data["shapes-TOTAL_FORMS"] = "5"
        data.update({"shapes-4-id": "", "shapes-4-start_earliest": "01:00",
                     "shapes-4-start_latest": "02:00", "shapes-4-end_earliest": "10:00",
                     "shapes-4-end_latest": "12:00", "shapes-4-max_span_hours": "10",
                     "shapes-4-notes": "A fifth shape"})
        self.client.force_login(self.manager)
        self.client.post(self.url, data)
        self.assertEqual(ShiftTemplate.objects.count(), 4)
        # One that doesn't even clean is refused on the page, not a crash.
        data["shapes-4-start_earliest"] = "soon"
        data["shapes-0-notes"] = "Changed"
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ShiftTemplate.objects.count(), 4)
        self.assertNotEqual(self.stored("morning").notes, "Changed")
