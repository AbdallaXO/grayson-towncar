"""Driver facts, shift templates and the regular-shift switch (structured shifts, Stage 1).

Covers the model layer: the seeded Morning / Midday / Evening / Float
templates and their 12-hour ceiling, the minute helpers on DriverWeeklySchedule and Driver, and
the guard that keeps SchedulerSettings.regular_shift_windows out of the generic
settings endpoint and "Reset to defaults" (only the Regular Shifts page may write it).

And the driver profile (Task 6): the read-only Shift facts card every staff user
sees, and the manager's Shift facts fields in edit mode — hard limits, days a
week, extra-shift days and the regular car — which refuse a limit that a
confirmed regular day breaks, naming the day (Review Focus 5).

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_shift_facts
"""
import importlib
import json
import re
from datetime import datetime, time
from unittest import mock

from django.apps import apps as django_apps
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dispatching.models import REGULAR_WINDOWS_CACHE_KEY, SchedulerSettings
from drivers import regular_shifts as rs
from drivers.admin import DriverWeeklyScheduleInline
from drivers.models import Driver, DriverWeeklySchedule, FleetVehicle, ShiftTemplate
from drivers.regular_shifts import DayShift
from drivers.test_support import RegularShiftCacheMixin


def _driver(username="sam", **kw):
    user = User.objects.create_user(username=username, first_name=username.title())
    return Driver.objects.create(profile=user, driver_type="inhouse", **kw)


class ShiftTemplateTests(RegularShiftCacheMixin, TestCase):
    def test_seeded_templates(self):
        self.assertEqual(
            list(ShiftTemplate.objects.values_list("kind", "start_earliest", "start_latest",
                                                   "end_earliest", "end_latest", "max_span_minutes")),
            [("morning", time(3), time(6), time(12), time(16), 720),
             ("midday", time(6), time(9), time(15), time(21), 720),
             ("evening", time(12), time(16), time(20), time(2, 15), 720),
             ("float", time(3), time(16), time(12), time(2, 15), 720)])
        self.assertEqual(list(ShiftTemplate.objects.values_list("name", flat=True)),
                         ["Morning", "Midday", "Evening", "Float"])
        self.assertTrue(all(ShiftTemplate.objects.values_list("notes", flat=True)))

    def test_template_span_ceiling(self):
        t = ShiftTemplate.objects.get(kind="morning")
        t.max_span_minutes = 721
        with self.assertRaises(ValidationError) as cm:
            t.full_clean()
        self.assertIn("max_span_minutes", cm.exception.message_dict)

    def test_template_span_floor(self):
        t = ShiftTemplate.objects.get(kind="morning")
        t.max_span_minutes = 59
        with self.assertRaises(ValidationError) as cm:
            t.full_clean()
        self.assertIn("max_span_minutes", cm.exception.message_dict)
        t.max_span_minutes = 60
        t.full_clean()                                       # one hour is the shortest allowed

    def test_template_kind_is_unique(self):
        morning = ShiftTemplate.objects.get(kind="morning")
        twin = ShiftTemplate(kind="morning", name="Morning 2",
                             start_earliest=morning.start_earliest, start_latest=morning.start_latest,
                             end_earliest=morning.end_earliest, end_latest=morning.end_latest)
        with self.assertRaises(ValidationError) as cm:
            twin.validate_unique()
        self.assertIn("kind", cm.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            twin.save()

    def test_template_in_use_cannot_be_deleted(self):
        # PROTECT, not CASCADE: deleting a shape must never wipe drivers' regular shifts.
        morning = ShiftTemplate.objects.get(kind="morning")
        row = DriverWeeklySchedule.objects.create(driver=_driver(), day_of_week=0,
                                                  shift_template=morning,
                                                  shift_start=time(4, 10), shift_end=time(15, 30))
        with self.assertRaises(ProtectedError):
            morning.delete()
        row.refresh_from_db()
        self.assertEqual(row.shift_template_id, morning.id)

    def test_template_start_band_must_run_forwards(self):
        t = ShiftTemplate.objects.get(kind="morning")
        t.start_earliest, t.start_latest = time(7), time(6)
        with self.assertRaises(ValidationError) as cm:
            t.full_clean()
        self.assertIn("start_latest", cm.exception.message_dict)

    def test_evening_end_band_crosses_midnight(self):
        self.assertEqual(ShiftTemplate.objects.get(kind="evening").end_band_minutes(), (1200, 1575))
        morning = ShiftTemplate.objects.get(kind="morning")
        self.assertEqual(morning.start_band_minutes(), (180, 360))
        self.assertEqual(morning.end_band_minutes(), (720, 960))

    def test_band_label(self):
        self.assertEqual(ShiftTemplate.objects.get(kind="morning").band_label(),
                         "leaves 3 AM–6 AM, back 12 PM–4 PM")
        self.assertEqual(ShiftTemplate.objects.get(kind="evening").band_label(),
                         "leaves 12 PM–4 PM, back 8 PM–2:15 AM")
        self.assertEqual(str(ShiftTemplate.objects.get(kind="midday")), "Midday")


class FloatSeedMigrationTests(RegularShiftCacheMixin, TestCase):
    """drivers 0065's two steps, run on today's models (the same fields as the
    migration's state)."""
    mig = importlib.import_module("drivers.migrations.0065_seed_float_template")

    def _float_exists(self):
        return ShiftTemplate.objects.filter(kind="float").exists()

    def test_reverse_drops_an_unused_float_and_forwards_brings_it_back(self):
        self.mig.unseed(django_apps, None)
        self.assertFalse(self._float_exists())
        self.mig.unseed(django_apps, None)                   # nothing to remove: fine
        self.mig.seed(django_apps, None)
        self.mig.seed(django_apps, None)                     # re-running is a no-op
        self.assertEqual(ShiftTemplate.objects.filter(kind="float").count(), 1)

    def test_reverse_keeps_a_float_someone_uses(self):
        floater = ShiftTemplate.objects.get(kind="float")
        driver = _driver(shift_role=floater)                 # as the usual shift
        self.mig.unseed(django_apps, None)
        self.assertTrue(self._float_exists())
        Driver.objects.filter(pk=driver.pk).update(shift_role=None)
        row = DriverWeeklySchedule.objects.create(driver=driver, day_of_week=0,
                                                  alt_template=floater)   # as a second shift
        self.mig.unseed(django_apps, None)
        self.assertTrue(self._float_exists())
        DriverWeeklySchedule.objects.filter(pk=row.pk).update(alt_template=None,
                                                              shift_template=floater)
        self.mig.unseed(django_apps, None)                   # as the day's shift
        self.assertTrue(self._float_exists())
        row.delete()
        self.mig.unseed(django_apps, None)
        self.assertFalse(self._float_exists())


class DriverShiftFactsTests(RegularShiftCacheMixin, TestCase):
    def test_weekly_regular_minutes(self):
        driver = _driver()
        evening = ShiftTemplate.objects.get(kind="evening")
        morning = ShiftTemplate.objects.get(kind="morning")
        late = DriverWeeklySchedule(driver=driver, day_of_week=0, shift_template_id=evening.id,
                                    shift_start=time(14, 15), shift_end=time(2, 15))
        early = DriverWeeklySchedule(driver=driver, day_of_week=1, shift_template_id=morning.id,
                                     shift_start=time(4, 10), shift_end=time(15, 30))
        with self.assertNumQueries(0):          # reads shift_template_id, never the template row
            self.assertEqual(late.regular_minutes(), (855, 1575))
            self.assertEqual(early.regular_minutes(), (250, 930))
        same = DriverWeeklySchedule(driver=driver, day_of_week=2, shift_template_id=morning.id,
                                    shift_start=time(4), shift_end=time(4))
        self.assertEqual(same.regular_minutes(), (240, 1680))   # end at the start = next day
        off = DriverWeeklySchedule(driver=driver, day_of_week=3,
                                   shift_start=time(4, 10), shift_end=time(15, 30))
        self.assertIsNone(off.regular_minutes())
        half = DriverWeeklySchedule(driver=driver, day_of_week=4, shift_template_id=morning.id,
                                    shift_start=time(4, 10))
        self.assertIsNone(half.regular_minutes())

    def test_hard_window_minutes(self):
        self.assertEqual(_driver("a", hard_earliest_start=time(5), hard_latest_finish=time(1),
                                 hard_latest_finish_next_day=True).hard_window_minutes(),
                         (300, 1500))
        self.assertEqual(_driver("b", hard_earliest_start=time(5),
                                 hard_latest_finish=time(23)).hard_window_minutes(),
                         (300, 1380))
        self.assertEqual(_driver("c").hard_window_minutes(), (None, None))
        self.assertEqual(_driver("d", hard_latest_finish=time(0),
                                 hard_latest_finish_next_day=True).hard_window_minutes(),
                         (None, 1440))

    def test_new_driver_defaults(self):
        driver = _driver()
        driver.refresh_from_db()
        self.assertEqual(driver.extra_shift_days, [])
        self.assertFalse(driver.has_regular_shift)
        self.assertIsNone(driver.max_days_per_week)
        self.assertFalse(driver.hard_latest_finish_next_day)

    def test_usual_shift_and_day_options_default_blank_and_protect_the_shape(self):
        floater = ShiftTemplate.objects.get(kind="float")
        evening = ShiftTemplate.objects.get(kind="evening")
        driver = _driver(shift_role=floater)
        row = DriverWeeklySchedule.objects.create(driver=_driver("pat"), day_of_week=0)
        self.assertEqual((row.alt_template_id, row.day_earliest_start, row.day_latest_finish,
                          row.day_latest_finish_next_day), (None, None, None, False))
        self.assertIsNone(_driver("lee").shift_role_id)
        row.alt_template = evening
        row.save()
        for shape in (floater, evening):                     # a usual shift / second shape
            with self.subTest(kind=shape.kind), self.assertRaises(ProtectedError):
                shape.delete()
        driver.refresh_from_db()
        self.assertEqual(driver.shift_role_id, floater.id)

    def test_has_regular_shift_once_confirmed(self):
        from django.utils import timezone
        driver = _driver(regular_shift_confirmed_at=timezone.now())
        self.assertTrue(driver.has_regular_shift)

    def test_days_a_week_limits(self):
        driver = _driver(max_days_per_week=8)
        with self.assertRaises(ValidationError) as cm:
            driver.full_clean()
        self.assertIn("max_days_per_week", cm.exception.message_dict)


class RegularShiftSwitchGuardTests(RegularShiftCacheMixin, TestCase):
    def setUp(self):
        super().setUp()
        staff = User.objects.create_user("boss", password="x", is_staff=True)
        self.client.force_login(staff)

    def _post(self, payload):
        return self.client.post(reverse("update_scheduler_settings"),
                                data=json.dumps(payload), content_type="application/json")

    def test_switch_defaults_off(self):
        self.assertFalse(SchedulerSettings.get_settings().regular_shift_windows)

    def test_generic_settings_endpoint_cannot_flip_switch(self):
        resp = self._post({"regular_shift_windows": 1, "min_turn_buffer": 7})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("regular_shift_windows", resp.json()["updated"])
        self.assertEqual(resp.json()["updated"], ["min_turn_buffer"])
        row = SchedulerSettings.objects.get(pk=1)
        self.assertFalse(row.regular_shift_windows)
        self.assertEqual(row.min_turn_buffer, 7)

    def test_stale_cached_row_cannot_write_switch_back(self):
        SchedulerSettings.get_settings()                    # cache the row (switch False)
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True)
        self._post({"min_turn_buffer": 7})
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)
        self.assertEqual(SchedulerSettings.objects.get(pk=1).min_turn_buffer, 7)

    def test_endpoint_saves_only_the_fields_it_was_sent(self):
        # Another worker flips the switch between this request's read and its save.
        stale = SchedulerSettings.get_settings()
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True)
        with mock.patch.object(SchedulerSettings, "get_settings", return_value=stale):
            self._post({"min_turn_buffer": 7})
        self.assertTrue(SchedulerSettings.objects.get(pk=1).regular_shift_windows)

    def test_bad_value_leaves_cached_row_untouched(self):
        # A good field ahead of a bad one must not stay set on this worker's cached row.
        resp = self._post({"min_turn_buffer": 7, "buffer_perfect": "lots"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(SchedulerSettings.get_settings().min_turn_buffer, 5)
        self.assertEqual(SchedulerSettings.objects.get(pk=1).min_turn_buffer, 5)

    def test_reset_keeps_switch(self):
        SchedulerSettings.get_settings()
        SchedulerSettings.objects.filter(pk=1).update(regular_shift_windows=True, min_turn_buffer=9)
        resp = self._post({"reset": True})
        self.assertEqual(resp.status_code, 200)
        row = SchedulerSettings.objects.get(pk=1)
        self.assertTrue(row.regular_shift_windows)
        self.assertEqual(row.min_turn_buffer, 5)            # everything else still resets

    def test_reset_to_defaults_skips_guarded_fields(self):
        row = SchedulerSettings.get_settings()
        row.regular_shift_windows = True                     # a stale in-memory value
        row.min_turn_buffer = 9
        row.reset_to_defaults()
        fresh = SchedulerSettings.objects.get(pk=1)
        self.assertFalse(fresh.regular_shift_windows)        # never written either way
        self.assertEqual(fresh.min_turn_buffer, 5)
        self.assertIn("regular_shift_windows", SchedulerSettings.GUARDED_FIELDS)

    def test_clear_cache_drops_switch_key(self):
        cache.set(REGULAR_WINDOWS_CACHE_KEY, True, 60)
        SchedulerSettings.clear_cache()
        self.assertIsNone(cache.get(REGULAR_WINDOWS_CACHE_KEY))


@override_settings(GOOGLE_MAPS_API_KEY="")
class DriverProfileShiftFactsTests(RegularShiftCacheMixin, TestCase):
    """The profile's read-only Shift facts card (every staff user) and the
    manager's Shift facts fields in edit mode (Task 6)."""

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user("boss", password="x", is_staff=True,
                                                is_superuser=True, first_name="Ada",
                                                last_name="Boss")
        self.dispatcher = User.objects.create_user("desk", password="x", is_staff=True)
        self.driver = _driver()
        self.unit = FleetVehicle.objects.create(vehicle_number="008", year=2022,
                                                make="Chevrolet", model="Suburban")
        self.shapes = {t.kind: t.id for t in ShiftTemplate.objects.all()}

    def _url(self):
        return reverse("driver_profile", args=[self.driver.id])

    def _confirm(self, days, role="morning"):
        rs.save_regular_shift(self.driver, days, self.manager,
                              role_template_id=self.shapes[role])
        self.driver.refresh_from_db()

    def _post(self, **fields):
        # The whole edit form, as the browser sends it (DriverProfileEditTests).
        data = {"phone_number": "", "vehicle": "", "payment_method": "", "night_bonus": "0",
                "employment_type": "", "is_active": "on", "notes": "",
                "license_number": "", "license_state": "", "license_class": "",
                "license_expiration": "", "chauffeur_permit_number": "",
                "chauffeur_permit_expiration": "", "dot_medical_card_expiration": "",
                **fields}
        return self.client.post(self._url(), data)

    @staticmethod
    def _card(html):
        """The read-only card, up to the next profile card (the Strengths & habits
        card when it is there, else the Weekly Schedule card)."""
        start = html.index('id="shift-facts"')
        end = html.index('class="profile-card', start + 1)
        return html[start:end]

    @staticmethod
    def _input(html, name, value=None):
        """The rendered <input> tag for ``name`` (and ``value``), or None."""
        value_attr = "" if value is None else rf'[^>]*value="{value}"'
        found = re.search(rf'<input[^>]*name="{name}"{value_attr}[^>]*>', html)
        return found.group(0) if found else None

    def test_profile_shows_shift_facts_card_to_dispatcher(self):
        m, e, f = self.shapes["morning"], self.shapes["evening"], self.shapes["float"]
        Driver.objects.filter(pk=self.driver.pk).update(
            hard_earliest_start=time(4), hard_latest_finish=time(1),
            hard_latest_finish_next_day=True, max_days_per_week=5, extra_shift_days=[5, 6])
        self.driver.refresh_from_db()
        self._confirm([DayShift(0, m, time(4, 10), time(15, 30)),
                       DayShift(3, m, None, None, day_latest=time(15)),
                       DayShift(4, m, None, None, alt_template_id=e),
                       DayShift(5, f, None, None)])
        Driver.objects.filter(pk=self.driver.pk).update(
            regular_shift_confirmed_at=timezone.make_aware(datetime(2026, 10, 4, 14, 0)))
        self.driver.preferred_vehicles.add(self.unit)
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["regular_rows"], [
            ("Monday", "Morning 4:10 AM – 3:30 PM"), ("Tuesday", "Off"), ("Wednesday", "Off"),
            ("Thursday", "Morning (usual times) · done by 3 PM"),
            ("Friday", "Morning or Evening"), ("Saturday", "Float"), ("Sunday", "Off")])
        self.assertEqual(resp.context["regular_summary"],
                         "Mon Morning 4:10 AM–3:30 PM · Thu Morning (usual times), done by 3 PM"
                         " · Fri Morning or Evening · Sat Float")
        card = self._card(resp.content.decode())
        for text in ("Shift facts", "Morning driver", "Morning 4:10 AM – 3:30 PM",
                     "Morning (usual times) · done by 3 PM", "Morning or Evening", "Float",
                     "Confirmed by Ada Boss on Oct 4", "Never starts before", "4 AM",
                     "Never finishes after", "1 AM (next day)", "Days a week", ">5<",
                     "Open to extra shifts on", "Sat, Sun", "Regular car", "#008",
                     "Day Setup offers this car first."):
            self.assertIn(text, card)
        self.assertNotIn("No regular shift yet", card)
        self.assertNotIn("—", card)                          # every fact is filled in
        self.assertNotContains(resp, 'name="hard_earliest_start"')   # view only

    def test_card_without_a_regular_shift_shows_dashes(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self._url())
        self.assertEqual((resp.context["regular_rows"], resp.context["regular_summary"]),
                         ([], ""))
        card = self._card(resp.content.decode())
        self.assertIn("No regular shift yet", card)
        self.assertNotIn("Confirmed by", card)
        self.assertNotIn("driver</span>", card)              # no usual-shift pill
        self.assertNotIn("Day Setup offers this car first.", card)
        self.assertEqual(card.count("—"), 5)                 # each fact, blank

    def test_card_names_the_car_day_setup_offers(self):
        # Day Setup locks the first unit by number (5 before 008) and offers
        # only units still in service.
        five = FleetVehicle.objects.create(vehicle_number="5", year=2020, make="Ford",
                                           model="Expedition")
        self.driver.preferred_vehicles.add(self.unit, five)
        self.client.force_login(self.dispatcher)
        card = self._card(self.client.get(self._url()).content.decode())
        self.assertLess(card.index("#5 "), card.index("#008 "))
        self.assertIn("Day Setup offers #5 first.", card)
        self.assertNotIn("this car", card)
        self.assertNotIn("(inactive)", card)
        # His first unit is retired: it is marked, and Day Setup offers neither.
        FleetVehicle.objects.filter(pk=five.pk).update(is_active=False)
        card = self._card(self.client.get(self._url()).content.decode())
        self.assertIn("Expedition (inactive)", card)
        self.assertNotIn("Suburban (inactive)", card)
        self.assertNotIn("Day Setup offers", card)
        # Only the retired unit: marked, and nothing offered.
        self.driver.preferred_vehicles.remove(self.unit)
        card = self._card(self.client.get(self._url()).content.decode())
        self.assertIn("#5 ", card)
        self.assertIn("(inactive)", card)
        self.assertNotIn("Day Setup offers", card)

    def test_float_driver_off_every_day(self):
        self._confirm([], role="float")                      # extra shifts only (S2)
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self._url())
        self.assertEqual([label for _, label in resp.context["regular_rows"]], ["Off"] * 7)
        self.assertEqual(resp.context["regular_summary"], "Off every day")
        card = self._card(resp.content.decode())
        self.assertIn("Float — any shift", card)
        self.assertIn("Confirmed by Ada Boss on", card)

    def test_manager_saves_facts(self):
        self.client.force_login(self.manager)
        html = self.client.get(self._url(), {"edit": "1"}).content.decode()
        for name in ("hard_earliest_start", "hard_latest_finish", "hard_latest_finish_next_day",
                     "max_days_per_week", "extra_shift_days", "preferred_vehicles"):
            self.assertIsNotNone(self._input(html, name), name)
        self.assertIn('id="shift-facts"', html)              # the card shows in edit mode too
        resp = self._post(hard_earliest_start="05:00", hard_latest_finish="01:00",
                          hard_latest_finish_next_day="on", max_days_per_week="5",
                          extra_shift_days=["6", "2"], preferred_vehicles=[str(self.unit.id)])
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual((self.driver.hard_earliest_start, self.driver.hard_latest_finish,
                          self.driver.hard_latest_finish_next_day,
                          self.driver.max_days_per_week, self.driver.extra_shift_days),
                         (time(5), time(1), True, 5, [2, 6]))
        self.assertEqual(list(self.driver.preferred_vehicles.all()), [self.unit])
        html = self.client.get(self._url(), {"edit": "1"}).content.decode()
        start = self._input(html, "hard_earliest_start")
        self.assertIn('type="time"', start)
        self.assertIn('value="05:00"', start)
        days = self._input(html, "max_days_per_week")
        for attr in ('type="number"', 'value="5"', 'min="1"', 'max="7"'):
            self.assertIn(attr, days)
        for day in (2, 6):
            self.assertIn("checked", self._input(html, "extra_shift_days", day))
        self.assertNotIn("checked", self._input(html, "extra_shift_days", 0))
        # Blank fields and unticked boxes clear what was there.
        self._post(preferred_vehicles=[str(self.unit.id)])
        self.driver.refresh_from_db()
        self.assertEqual((self.driver.hard_earliest_start, self.driver.hard_latest_finish,
                          self.driver.hard_latest_finish_next_day,
                          self.driver.max_days_per_week, self.driver.extra_shift_days),
                         (None, None, False, None, []))

    def test_saving_other_field_keeps_preferred_vehicles(self):
        # His regular car was taken out of service. Its box still shows, ticked,
        # so saving a new phone number can't drop it; nobody else's retired car shows.
        FleetVehicle.objects.filter(pk=self.unit.pk).update(is_active=False)
        spare = FleetVehicle.objects.create(vehicle_number="099", year=2019, make="Ford",
                                            model="Expedition", is_active=False)
        self.driver.preferred_vehicles.add(self.unit)
        self.client.force_login(self.manager)
        html = self.client.get(self._url(), {"edit": "1"}).content.decode()
        box = self._input(html, "preferred_vehicles", self.unit.id)
        self.assertIsNotNone(box)
        self.assertIn("checked", box)
        self.assertIsNone(self._input(html, "preferred_vehicles", spare.id))
        resp = self._post(phone_number="4075559999", preferred_vehicles=[str(self.unit.id)])
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.phone_number, "+14075559999")
        self.assertEqual(list(self.driver.preferred_vehicles.all()), [self.unit])

    def test_hard_limit_conflict_names_the_day(self):
        self._confirm([DayShift(0, self.shapes["morning"], time(4, 10), time(15, 30))])
        self.client.force_login(self.manager)
        resp = self._post(phone_number="4075550000", hard_earliest_start="05:00")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["driver_form"].non_field_errors(),
                         ["Monday: starts at 4:10 AM — before this driver's earliest start "
                          "(5 AM)."])
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.hard_earliest_start)
        self.assertIsNone(self.driver.phone_number)              # the rest isn't saved either
        # The same kind of limit saves once it no longer cuts Monday.
        resp = self._post(hard_earliest_start="04:10")
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hard_earliest_start, time(4, 10))

    def test_latest_finish_and_days_a_week_checked_too(self):
        m = self.shapes["morning"]
        self._confirm([DayShift(i, m, time(4, 10), time(15, 30)) for i in range(4)])
        self.client.force_login(self.manager)
        form = self._post(hard_latest_finish="15:00", max_days_per_week="3").context["driver_form"]
        self.assertEqual(form.non_field_errors(), [
            "Monday: ends at 3:30 PM — after this driver's latest finish (3 PM).",
            "Tuesday: ends at 3:30 PM — after this driver's latest finish (3 PM).",
            "Wednesday: ends at 3:30 PM — after this driver's latest finish (3 PM).",
            "Thursday: ends at 3:30 PM — after this driver's latest finish (3 PM).",
            "4 working days — more than this driver's limit of 3 a week."])
        # Out of range: the field says so, and the week isn't judged against it.
        form = self._post(max_days_per_week="8").context["driver_form"]
        self.assertIn("max_days_per_week", form.errors)
        self.assertEqual(form.non_field_errors(), [])
        # A finish flagged next day is after midnight, so it doesn't cut a morning.
        resp = self._post(hard_latest_finish="01:00", hard_latest_finish_next_day="on")
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.max_days_per_week)
        self.assertEqual(self.driver.hard_latest_finish, time(1))

    def test_day_past_midnight_against_a_next_day_finish(self):
        self._confirm([DayShift(4, self.shapes["evening"], time(16), time(2, 15))],
                      role="evening")
        self.client.force_login(self.manager)
        resp = self._post(hard_latest_finish="02:00", hard_latest_finish_next_day="on")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["driver_form"].non_field_errors(),
                         ["Friday: ends at 2:15 AM — after this driver's latest finish "
                          "(2 AM)."])
        resp = self._post(hard_latest_finish="03:00", hard_latest_finish_next_day="on")
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual((self.driver.hard_latest_finish, self.driver.hard_latest_finish_next_day),
                         (time(3), True))

    def test_conflict_already_there_does_not_block_other_fields(self):
        # Monday was confirmed at 4:10 AM; then a 5 AM earliest start was set in admin.
        self._confirm([DayShift(0, self.shapes["morning"], time(4, 10), time(15, 30))])
        Driver.objects.filter(pk=self.driver.pk).update(hard_earliest_start=time(5))
        self.client.force_login(self.manager)
        # The edit form sends the limit back unchanged with a new phone number.
        resp = self._post(phone_number="4075559999", hard_earliest_start="05:00")
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.phone_number, "+14075559999")
        self.assertEqual(self.driver.hard_earliest_start, time(5))
        # Editing that limit is still judged against the week.
        resp = self._post(hard_earliest_start="05:30")
        self.assertEqual(resp.context["driver_form"].non_field_errors(),
                         ["Monday: starts at 4:10 AM — before this driver's earliest start "
                          "(5:30 AM)."])

    def test_latest_finish_must_come_after_earliest_start(self):
        # No regular shift yet: nothing else would catch a pair that leaves no time.
        self.client.force_login(self.manager)
        for fields, error in (
                ({"hard_earliest_start": "14:00", "hard_latest_finish": "02:00"},
                 "has to be later than the earliest start (2 PM). "
                 "Tick Next day if it's after midnight."),
                ({"hard_earliest_start": "14:00", "hard_latest_finish": "14:00"},
                 "has to be later than the earliest start (2 PM). "
                 "Tick Next day if it's after midnight."),
                ({"hard_latest_finish": "00:00"},
                 "12 AM on the same day leaves no time for a shift. "
                 "Tick Next day if it's after midnight.")):
            with self.subTest(**fields):
                resp = self._post(phone_number="4075550000", **fields)
                self.assertEqual(resp.status_code, 200)
                form = resp.context["driver_form"]
                self.assertEqual(form.errors["hard_latest_finish"], [error])
                self.assertContains(resp, f"Never finishes after: {error}".replace("'", "&#x27;"))
                self.driver.refresh_from_db()
                self.assertEqual((self.driver.hard_earliest_start, self.driver.hard_latest_finish,
                                  self.driver.phone_number), (None, None, None))
        # Ticking Next day makes each of them a real window.
        for fields in ({"hard_earliest_start": "14:00", "hard_latest_finish": "02:00"},
                       {"hard_latest_finish": "00:00"}):
            with self.subTest(next_day=fields):
                resp = self._post(hard_latest_finish_next_day="on", **fields)
                self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hard_window_minutes(), (None, 1440))

    def test_impossible_limits_name_the_field_once_for_a_confirmed_week(self):
        m = self.shapes["morning"]
        self._confirm([DayShift(i, m, None, None) for i in range(5)])
        self.client.force_login(self.manager)
        form = self._post(hard_earliest_start="14:00",
                          hard_latest_finish="02:00").context["driver_form"]
        self.assertEqual(form.errors["hard_latest_finish"],
                         ["has to be later than the earliest start (2 PM). "
                          "Tick Next day if it's after midnight."])
        self.assertEqual(form.non_field_errors(), [])        # not once per working day

    def test_limits_not_checked_without_a_confirmed_regular_shift(self):
        DriverWeeklySchedule.objects.create(driver=self.driver, day_of_week=0,
                                            shift_template_id=self.shapes["morning"],
                                            shift_start=time(4, 10), shift_end=time(15, 30))
        self.client.force_login(self.manager)
        resp = self._post(hard_earliest_start="05:00")
        self.assertRedirects(resp, self._url())
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.hard_earliest_start, time(5))

    def test_dispatcher_post_forbidden(self):
        self.client.force_login(self.dispatcher)
        resp = self._post(hard_earliest_start="05:00", max_days_per_week="4",
                          extra_shift_days=["5"], preferred_vehicles=[str(self.unit.id)])
        self.assertEqual(resp.status_code, 403)
        self.driver.refresh_from_db()
        self.assertEqual((self.driver.hard_earliest_start, self.driver.max_days_per_week,
                          self.driver.extra_shift_days), (None, None, []))
        self.assertFalse(self.driver.preferred_vehicles.exists())


class DriverAdminWeeklyRowTests(RegularShiftCacheMixin, TestCase):
    """The Driver admin's old weekly-schedule rows also hold each day's
    confirmed regular shift, which that inline can't put back: deleting the
    row, or moving it to another weekday, is refused for a regular day."""

    def setUp(self):
        super().setUp()
        self.manager = User.objects.create_user("boss", password="x", is_staff=True,
                                                is_superuser=True)
        self.driver = _driver()
        morning = ShiftTemplate.objects.get(kind="morning").id
        rs.save_regular_shift(self.driver, [DayShift(i, morning, time(5), time(15))
                                            for i in range(5)],
                              self.manager, role_template_id=morning)
        # Saturday has no row, so a row can move there; Sunday is an old-style row.
        DriverWeeklySchedule.objects.filter(driver=self.driver, day_of_week__gte=5).delete()
        DriverWeeklySchedule.objects.create(driver=self.driver, day_of_week=6,
                                            is_available=False)
        request = RequestFactory().get("/")
        request.user = self.manager
        inline = DriverWeeklyScheduleInline(Driver, django_admin.site)
        self.FormSet = inline.get_formset(request, self.driver)

    def _post(self, changes=None):
        """The inline as the admin page sends it back, with ``changes`` keyed by
        weekday: {0: {"DELETE": "on"}} ticks Delete on Monday's row."""
        blank = self.FormSet(instance=self.driver)
        data = {f"{blank.prefix}-TOTAL_FORMS": str(len(blank.forms)),
                f"{blank.prefix}-INITIAL_FORMS": str(blank.initial_form_count()),
                f"{blank.prefix}-MIN_NUM_FORMS": "0", f"{blank.prefix}-MAX_NUM_FORMS": "7"}
        for form in blank.forms:
            for name in form.fields:
                value = form[name].value()
                if value is not None and value is not False:
                    data[form.add_prefix(name)] = "on" if value is True else str(value)
            for name, value in (changes or {}).get(form.instance.day_of_week, {}).items():
                data[form.add_prefix(name)] = value
        return self.FormSet(data, instance=self.driver)

    def _working_days(self):
        return [d.day for d in rs.current_days(self.driver) if d.template_id is not None]

    def test_deleting_a_regular_day_is_refused(self):
        formset = self._post({0: {"DELETE": "on"}, 2: {"DELETE": "on"}})
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.non_form_errors(), [
            "Monday is part of this driver's regular shift. "
            "Set it to Off on Regular Shifts instead of deleting it.",
            "Wednesday is part of this driver's regular shift. "
            "Set it to Off on Regular Shifts instead of deleting it."])
        self.assertEqual(self._working_days(), [0, 1, 2, 3, 4])

    def test_moving_a_regular_day_is_refused(self):
        formset = self._post({0: {"day_of_week": "5"}})
        self.assertFalse(formset.is_valid())
        self.assertEqual(formset.non_form_errors(), [
            "Monday is part of this driver's regular shift, so it can't move to another "
            "day here. Change it on Regular Shifts."])
        self.assertEqual(self._working_days(), [0, 1, 2, 3, 4])

    def test_old_style_rows_and_old_fields_save_as_before(self):
        # Sunday has no regular shift: deleting it still works. Monday's old
        # fields still save, and its regular shift stays.
        formset = self._post({6: {"DELETE": "on"}, 0: {"scheduling_notes": "Late Mondays"}})
        self.assertTrue(formset.is_valid(), formset.non_form_errors())
        formset.save()
        self.assertFalse(DriverWeeklySchedule.objects.filter(driver=self.driver,
                                                             day_of_week=6).exists())
        monday = DriverWeeklySchedule.objects.get(driver=self.driver, day_of_week=0)
        self.assertEqual((monday.scheduling_notes, monday.shift_start, monday.shift_end),
                         ("Late Mondays", time(5), time(15)))
        self.assertEqual(self._working_days(), [0, 1, 2, 3, 4])

    def test_change_page_shows_each_rows_regular_shift(self):
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("admin:drivers_driver_change", args=[self.driver.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Regular shift")
        self.assertContains(resp, "Morning 5 AM – 3 PM", count=5)
