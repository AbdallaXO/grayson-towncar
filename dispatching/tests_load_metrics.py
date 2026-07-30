"""Tests for dispatching/load_metrics.py and the two chauffeur pages.

Focus is on the things that would quietly produce wrong numbers rather than crash:
the availability day-count, the idle-day arithmetic, and the two page guarantees —
no money and no jargon anywhere in the rendered HTML, and the exceptions list
existing only on the superuser page.
"""

from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from dispatching.load_metrics import (
    FLEX_DAY_HOURS,
    available_hours_for,
    build_fleet_summary,
    build_load_rows,
    serialize_rows,
)
from drivers.models import Driver, DriverDateOverride, DriverWeeklySchedule
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

User = get_user_model()

#: Tokens that must never appear in either page's rendered HTML, lowercased.
#: "$" guards money; the words are jargon SOP-003 explains but the page must not use.
BANNED_PAGE_TOKENS = ("$", "utilisation", "utilization", "share of work", "gap")

#: Keys that must never appear in a serialized row — money belongs to the future
#: Driver economics page, and the hour-based estimates no longer ship at all.
BANNED_ROW_KEYS = ("revenue", "driver_pay", "margin", "revenue_per_trip",
                   "share_of_work", "utilisation", "avail_hours", "worked_hours",
                   "coverage_gap_days")


def _eff(**over):
    base = {
        "is_available": True, "shift_type": "full_day", "start_hour": 6, "end_hour": 18,
        "flexible": False, "max_hours": None, "exception_type": None,
        "exception_start_time": None, "exception_end_time": None, "exception_reason": "",
    }
    base.update(over)
    return base


class AvailableHoursTests(TestCase):
    """Kept although the load pages no longer render hours: this is the one honest
    hours denominator, retained for the future Driver economics page."""

    def test_fixed_window_is_measured(self):
        self.assertEqual(available_hours_for(_eff(start_hour=6, end_hour=18)), 12.0)

    def test_unavailable_day_is_zero(self):
        self.assertEqual(available_hours_for(_eff(is_available=False)), 0.0)

    def test_open_flex_day_uses_the_constant(self):
        got = available_hours_for(_eff(shift_type="full_day", flexible=True))
        self.assertEqual(got, FLEX_DAY_HOURS)

    def test_open_flex_day_prefers_explicit_max_hours(self):
        got = available_hours_for(
            _eff(shift_type="full_day", flexible=True, max_hours=Decimal("8.00"))
        )
        self.assertEqual(got, 8.0)

    def test_window_wrapping_midnight(self):
        # 22:00 -> 06:00 is 8 hours, not a negative number.
        self.assertEqual(available_hours_for(_eff(start_hour=22, end_hour=6)), 8.0)

    def test_available_until_narrows_the_window(self):
        """The resolver leaves base hours alone when the day was already available, so
        without this narrowing 'available until 2pm' would be billed as a full day."""
        got = available_hours_for(_eff(
            start_hour=6, end_hour=18,
            exception_type="available_until", exception_end_time=time(14, 0),
        ))
        self.assertEqual(got, 8.0)

    def test_available_after_narrows_the_window(self):
        got = available_hours_for(_eff(
            start_hour=6, end_hour=18,
            exception_type="available_after", exception_start_time=time(12, 0),
        ))
        self.assertEqual(got, 6.0)

    def test_unavailable_window_is_subtracted(self):
        got = available_hours_for(_eff(
            start_hour=6, end_hour=18,
            exception_type="unavailable_window",
            exception_start_time=time(10, 0), exception_end_time=time(13, 0),
        ))
        self.assertEqual(got, 9.0)

    def test_partial_exception_beats_the_flex_estimate(self):
        """A flex day with a hard cutoff is measured, not estimated at 12h."""
        got = available_hours_for(_eff(
            shift_type="full_day", flexible=True, start_hour=6, end_hour=18,
            exception_type="available_until", exception_end_time=time(10, 0),
        ))
        self.assertEqual(got, 4.0)


class _FixtureMixin:
    """Shared reservation scaffolding for row- and view-level tests."""

    def _base_fixtures(self):
        self.customer = Customer.objects.create(
            first_name="Test", last_name="Guest", email="g@example.com",
            phone_number="5551234567",
        )
        self.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        self.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"),
        )
        self.rate = Rate.objects.create(
            vehicle=self.vehicle, route=self.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )

    def _driver(self, username, employment_type="", available_days=7, hours=(6, 18),
                first_name=None):
        user = User.objects.create_user(
            username=username, password="x",
            first_name=first_name if first_name is not None else username.title(),
        )
        d = Driver.objects.create(
            profile=user, driver_type="inhouse", is_active=True,
            employment_type=employment_type,
        )
        for dow in range(7):
            DriverWeeklySchedule.objects.create(
                driver=d, day_of_week=dow,
                is_available=dow < available_days,
                start_hour=hours[0], end_hour=hours[1],
                shift_type="custom", flexible=False,
            )
        return d

    def _legs(self, driver, days):
        """One completed leg per date in ``days``."""
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, status="confirmed",
            rate=self.rate, vehicle=self.vehicle,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"),
        )
        for day in days:
            Leg.objects.create(
                reservation=res, driver=driver, route=self.route,
                pickup_date=day, pickup_time=time(9, 0),
                pickup_location="MCO Airport", dropoff_location="Disney Resort",
                status="completed", vehicle=self.vehicle,
                revenue_share=Decimal("100.00"), driver_pay_amount=Decimal("40.00"),
            )


class LoadRowTests(_FixtureMixin, TestCase):
    def setUp(self):
        self.today = date(2026, 7, 29)
        self.start = self.today - timedelta(days=13)
        self.end = self.today - timedelta(days=1)
        self._base_fixtures()

    def _offsets(self, driver, day_offsets):
        self._legs(driver, [self.start + timedelta(days=o) for o in day_offsets])

    # ── counted facts ────────────────────────────────────────────────────────

    def test_worked_days_counts_distinct_dates_not_legs(self):
        d = self._driver("dense")
        self._offsets(d, [0, 0, 0, 1])        # 3 legs one day, 1 the next
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(row["legs"], 4)
        self.assertEqual(row["worked_days"], 2)
        self.assertEqual(row["per_worked_day"], 2.0)

    def test_idle_days_is_available_days_not_worked(self):
        d = self._driver("idle_math", available_days=7)  # available all 13 window days
        self._offsets(d, [0, 1, 2])
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(row["avail_days"], 13)
        self.assertEqual(row["worked_days"], 3)
        self.assertEqual(row["idle_days"], 10)

    def test_worked_dates_are_sorted_distinct_dates(self):
        d = self._driver("streaky")
        self._offsets(d, [2, 0, 0, 1])
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(row["worked_dates"],
                         [self.start + timedelta(days=o) for o in (0, 1, 2)])

    def test_driver_with_no_legs_still_appears(self):
        """An idle driver is the whole point of the page — they must not be filtered out
        by the leg aggregate returning no rows for them."""
        self._driver("idle")
        rows = build_load_rows(self.start, self.end, today=self.today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["legs"], 0)
        self.assertEqual(rows[0]["worked_days"], 0)
        self.assertEqual(rows[0]["per_worked_day"], 0.0)

    def test_cancelled_reservation_legs_excluded(self):
        d = self._driver("cancelled")
        self._offsets(d, [0, 1])
        Reservation.objects.update(status="cancelled")
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(row["legs"], 0)

    def test_approved_time_off_removes_available_days(self):
        d = self._driver("onleave")
        baseline = build_load_rows(self.start, self.end, today=self.today)[0]
        DriverDateOverride.objects.create(
            driver=d, date=self.start, end_date=self.start + timedelta(days=2),
            exception_type="off", status="approved", reason="vacation",
        )
        after = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(after["avail_days"], baseline["avail_days"] - 3)

    def test_pending_time_off_does_not_change_availability(self):
        """Pending requests must be inert until approved — mirrors the resolver's rule."""
        d = self._driver("pending")
        baseline = build_load_rows(self.start, self.end, today=self.today)[0]
        DriverDateOverride.objects.create(
            driver=d, date=self.start, exception_type="off", status="pending",
        )
        after = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(after["avail_days"], baseline["avail_days"])

    def test_full_time_flag_surfaces_on_the_row(self):
        self._driver("ft", employment_type="full_time")
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertTrue(row["is_full_time"])
        self.assertEqual(row["employment_label"], "Full time")

    def test_unlabelled_driver_gets_unlabelled_label(self):
        self._driver("nolabel")
        row = build_load_rows(self.start, self.end, today=self.today)[0]
        self.assertEqual(row["employment_label"], "Unlabelled")

    # ── lite mode ────────────────────────────────────────────────────────────

    def test_lite_rows_skip_presentation_extras_but_keep_counts(self):
        d = self._driver("litely")
        self._offsets(d, [0, 1])
        row = build_load_rows(self.start, self.end, today=self.today, lite=True)[0]
        self.assertEqual(row["legs"], 2)
        self.assertEqual(row["worked_days"], 2)
        self.assertIn("idle_days", row)
        for key in ("cells", "vehicle_classes", "vehicle_car", "next_time_off"):
            self.assertNotIn(key, row)

    def test_lite_and_full_agree_on_the_numbers(self):
        d = self._driver("agree")
        self._offsets(d, [0, 0, 3])
        full = build_load_rows(self.start, self.end, today=self.today)[0]
        lite = build_load_rows(self.start, self.end, today=self.today, lite=True)[0]
        for key in ("legs", "worked_days", "avail_days", "idle_days",
                    "per_worked_day", "per_week"):
            self.assertEqual(full[key], lite[key], key)

    # ── serialization ────────────────────────────────────────────────────────

    def test_serialized_rows_contain_no_banned_keys(self):
        d = self._driver("cleanrow")
        self._offsets(d, [0, 1, 2])
        rows = build_load_rows(self.start, self.end, today=self.today)
        item = serialize_rows(rows)[0]
        for key in BANNED_ROW_KEYS:
            self.assertNotIn(key, item, f"{key} leaked into the serialized payload")
        self.assertIn("idle_days", item)

    def test_serialize_flags_only_listed_drivers(self):
        a = self._driver("flag_a")
        b = self._driver("flag_b")
        rows = build_load_rows(self.start, self.end, today=self.today)
        out = serialize_rows(rows, flagged_ids={a.id})
        by_id = {r["id"]: r for r in out}
        self.assertTrue(by_id[a.id]["flagged"])
        self.assertFalse(by_id[b.id]["flagged"])


class FleetSummaryTests(TestCase):
    def _row(self, emp, *, idle, legs, worked, avail, pwd, name="X"):
        return {"employment_type": emp, "idle_days": idle, "legs": legs,
                "worked_days": worked, "avail_days": avail,
                "per_worked_day": pwd, "name": name}

    def test_empty_roster_does_not_explode(self):
        self.assertEqual(build_fleet_summary([])["drivers"], 0)

    def test_ft_idle_days_counts_full_timers_only(self):
        rows = [
            self._row("full_time", idle=5, legs=10, worked=8, avail=13, pwd=1.25),
            self._row("part_time", idle=9, legs=2, worked=2, avail=11, pwd=1.0),
        ]
        s = build_fleet_summary(rows, window_days=13)

        # Idle days are reported per cohort. There is deliberately NO combined fleet
        # total: full-time idle days are a finding, part-time idle days are normal,
        # and the sum of the two means nothing.
        self.assertNotIn("idle_days", s)

        self.assertEqual(s["ft_idle_days"], 5)
        self.assertEqual(s["ft_avail_days"], 13)
        self.assertAlmostEqual(s["ft_idle_share"], 5 / 13, places=6)
        self.assertEqual(s["cohorts"]["full_time"]["idle_days"], 5)
        self.assertEqual(s["cohorts"]["part_time"]["idle_days"], 9)
        self.assertEqual(s["full_time"], 1)
        self.assertEqual(s["part_time"], 1)

    def test_zero_work_drivers_lists_available_but_idle(self):
        """Available in the window yet drove nothing — concrete and actionable."""
        rows = [
            self._row("full_time", idle=13, legs=0, worked=0, avail=13, pwd=0,
                      name="Idle Ivan"),
            self._row("full_time", idle=1, legs=20, worked=12, avail=13, pwd=1.7,
                      name="Busy Bea"),
            # Not available at all, so not "idle" — must not be listed.
            self._row("", idle=0, legs=0, worked=0, avail=0, pwd=0, name="Absent Al"),
        ]
        s = build_fleet_summary(rows, window_days=13)
        self.assertEqual(s["zero_work_drivers"], ["Idle Ivan"])


class ChauffeurLoadViewTests(_FixtureMixin, TestCase):
    """Permission gating plus the two page guarantees: banned tokens absent from the
    rendered HTML of both pages, and the exceptions list superuser-only.

    These tests seed real data so the payload, tiles, findings and exceptions all
    render — an empty page passing a token check proves nothing.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="cl_staff", password="pw", is_staff=True
        )
        self.boss = User.objects.create_user(
            username="cl_boss", password="pw", is_staff=True, is_superuser=True
        )
        self.nobody = User.objects.create_user(username="cl_nobody", password="pw")
        self._base_fixtures()

    def _seed_roster(self):
        """A working driver and an available-but-idle full-timer, placed inside the
        default 30-day window relative to the real localdate the views use."""
        today = timezone.localdate()
        worker = self._driver("cl_worker", employment_type="full_time",
                              first_name="Wanda")
        self._legs(worker, [today - timedelta(days=o) for o in (2, 3, 4, 5)])
        idle = self._driver("cl_idle", employment_type="full_time", first_name="Ivo")
        # Unlabelled, so the findings panel renders too during the token sweep.
        third = self._driver("cl_third", first_name="Uma")
        return {"worker": worker, "idle": idle, "third": third}

    # ── permissions ──────────────────────────────────────────────────────────

    def test_dispatcher_page_renders_for_staff(self):
        self.client.force_login(self.staff)
        r = self.client.get("/dispatching/chauffeur-load/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Availability &amp; Load")

    def test_dispatcher_page_rejects_non_staff(self):
        self.client.force_login(self.nobody)
        r = self.client.get("/dispatching/chauffeur-load/")
        self.assertEqual(r.status_code, 302)

    def test_kpi_page_rejects_plain_staff(self):
        """The exceptions list lives behind is_superuser, not is_staff."""
        self.client.force_login(self.staff)
        r = self.client.get("/dispatching/chauffeur-kpis/")
        self.assertEqual(r.status_code, 302)

    def test_kpi_page_renders_for_superuser(self):
        self.client.force_login(self.boss)
        r = self.client.get("/dispatching/chauffeur-kpis/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Chauffeur KPIs")

    # ── the token guarantee ──────────────────────────────────────────────────

    def test_both_pages_render_none_of_the_banned_tokens(self):
        """The whole rendered response — payload, CSS, JS, copy — must be free of
        money and of the jargon SOP-003 explains. Case-insensitive, and checked with
        real rows, findings, exceptions AND the handled list on the page so nothing
        is trivially clean."""
        from dispatching.models import ChauffeurExceptionDismissal

        roster = self._seed_roster()
        ChauffeurExceptionDismissal.objects.create(
            driver=roster["third"], rule="never_drove",
            dismissed_by=self.boss, note="spoke on the phone",
        )
        for url, user in (("/dispatching/chauffeur-load/", self.staff),
                          ("/dispatching/chauffeur-kpis/", self.boss)):
            self.client.force_login(user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode().lower()
            for token in BANNED_PAGE_TOKENS:
                self.assertNotIn(token, html, f"{token!r} rendered on {url}")

    def test_dispatcher_payload_has_no_banned_keys_and_no_flags(self):
        """Belt and braces: parse the embedded JSON and inspect the real row dicts."""
        import json as _json
        import re as _re

        self._seed_roster()
        self.client.force_login(self.staff)
        html = self.client.get("/dispatching/chauffeur-load/").content.decode()
        m = _re.search(r'<script id="cl-data" type="application/json">(.*?)</script>',
                       html, _re.S)
        self.assertIsNotNone(m, "cl-data payload not found in the page")
        rows = _json.loads(m.group(1))
        self.assertTrue(rows, "expected rows for the seeded drivers")
        for row in rows:
            for key in BANNED_ROW_KEYS:
                self.assertNotIn(key, row)
            # The dispatcher payload carries no judgement — flags come from the
            # exceptions list, which this page does not have.
            self.assertFalse(row["flagged"])

    # ── findings & exceptions gating ─────────────────────────────────────────

    def test_exceptions_render_only_on_the_kpi_page(self):
        """Same data, both pages: the idle full-timer is worth a conversation on the
        KPI page and invisible as a judgement on the dispatcher page."""
        self._seed_roster()

        self.client.force_login(self.boss)
        kpi = self.client.get("/dispatching/chauffeur-kpis/")
        self.assertContains(kpi, "Worth a conversation")
        self.assertContains(kpi, "drove none")

        self.client.force_login(self.staff)
        load = self.client.get("/dispatching/chauffeur-load/")
        self.assertNotContains(load, "Worth a conversation")
        self.assertNotContains(load, "drove none")

    def test_findings_render_on_both_pages(self):
        """An unlabelled driver fires the label finding for everyone — dispatchers
        see findings too, just never the conversation list."""
        self._driver("cl_nolabel", first_name="Nia")
        for url, user in (("/dispatching/chauffeur-load/", self.staff),
                          ("/dispatching/chauffeur-kpis/", self.boss)):
            self.client.force_login(user)
            r = self.client.get(url)
            self.assertContains(r, "What stands out", msg_prefix=url)
            self.assertContains(r, "full-time / part-time label", msg_prefix=url)

    def test_admin_link_on_unlabelled_finding_is_kpi_only(self):
        self._driver("cl_nolabel2", first_name="Noa")
        self.client.force_login(self.boss)
        self.assertContains(self.client.get("/dispatching/chauffeur-kpis/"),
                            "/admin/drivers/driver/")
        self.client.force_login(self.staff)
        self.assertNotContains(self.client.get("/dispatching/chauffeur-load/"),
                               "/admin/drivers/driver/")

    def test_sections_absent_when_no_rule_fires(self):
        """Empty roster: no findings panel, no exceptions panel — sections render
        nothing rather than an empty frame."""
        self.client.force_login(self.boss)
        r = self.client.get("/dispatching/chauffeur-kpis/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "What stands out")
        self.assertNotContains(r, "Worth a conversation")

    # ── window handling ──────────────────────────────────────────────────────

    def test_window_param_is_whitelisted(self):
        """An arbitrary window must fall back, not reach the query."""
        self.client.force_login(self.staff)
        r = self.client.get("/dispatching/chauffeur-load/?window=9999")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["window_days"], 30)

    def test_window_param_accepts_known_values(self):
        self.client.force_login(self.staff)
        for key, days in (("7", 7), ("30", 30), ("90", 90)):
            r = self.client.get(f"/dispatching/chauffeur-load/?window={key}")
            self.assertEqual(r.context["window_days"], days)

    def test_empty_roster_renders(self):
        """No active in-house drivers must not 500 the page."""
        self.client.force_login(self.staff)
        r = self.client.get("/dispatching/chauffeur-load/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["rows_data"], [])


class ExceptionDismissalTests(_FixtureMixin, TestCase):
    """Mark-handled flow: episode semantics, the default-window spending guard,
    and permission gating."""

    DISMISS_URL = "/dispatching/chauffeur-kpis/handled/"
    UNDO_URL = "/dispatching/chauffeur-kpis/handled/undo/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="xd_staff", password="pw", is_staff=True
        )
        self.boss = User.objects.create_user(
            username="xd_boss", password="pw", is_staff=True, is_superuser=True
        )
        self._base_fixtures()
        self.today = timezone.localdate()
        # Steady worker: every OTHER day, so no streak and no density rule fires
        # for them and the idle driver's rules stay predictable.
        self.worker = self._driver("xd_worker", employment_type="full_time",
                                   first_name="Wanda")
        self._legs(self.worker,
                   [self.today - timedelta(days=o) for o in range(2, 42, 2)])
        self.idle = self._driver("xd_idle", employment_type="full_time",
                                 first_name="Ivo")

    def _dismissals(self):
        from dispatching.models import ChauffeurExceptionDismissal
        return ChauffeurExceptionDismissal.objects

    def _dismiss(self, driver, rule, note=""):
        return self.client.post(self.DISMISS_URL, {
            "driver_id": driver.id, "rule": rule, "note": note, "window": "30",
        })

    def _flagged(self, html, name):
        import json as _json
        import re as _re
        m = _re.search(r'<script id="cl-data" type="application/json">(.*?)</script>',
                       html, _re.S)
        rows = _json.loads(m.group(1))
        return next(r["flagged"] for r in rows if name in r["name"])

    def test_dismiss_moves_entry_to_handled_and_unflags_the_row(self):
        self.client.force_login(self.boss)
        before = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("drove none", before)
        self.assertTrue(self._flagged(before, "Ivo"))

        r = self._dismiss(self.idle, "never_drove", note="on leave until next month")
        self.assertEqual(r.status_code, 302)

        after = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("Handled (1)", after)
        self.assertIn("on leave until next month", after)
        self.assertFalse(self._flagged(after, "Ivo"))

    def test_dismiss_rejected_for_plain_staff(self):
        self.client.force_login(self.staff)
        r = self._dismiss(self.idle, "never_drove")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._dismissals().count(), 0)

    def test_unknown_rule_rejected(self):
        self.client.force_login(self.boss)
        self._dismiss(self.idle, "not_a_rule")
        self.assertEqual(self._dismissals().count(), 0)

    def test_undo_puts_the_entry_back(self):
        self.client.force_login(self.boss)
        self._dismiss(self.idle, "never_drove")
        dismissal = self._dismissals().get()

        r = self.client.post(self.UNDO_URL,
                             {"dismissal_id": dismissal.id, "window": "30"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._dismissals().count(), 0)

        html = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertNotIn("Handled (", html)
        self.assertTrue(self._flagged(html, "Ivo"))

    def test_episode_clearing_spends_the_dismissal(self):
        """Handled hides the entry while the situation lasts; once it clears, the
        dismissal is spent, so a recurrence surfaces fresh."""
        from reservations.models import Leg

        self.client.force_login(self.boss)
        self._dismiss(self.idle, "never_drove")

        # The idle driver starts driving: the rule stops firing, and the next
        # default-window render spends the dismissal.
        self._legs(self.idle, [self.today - timedelta(days=3)])
        self.client.get("/dispatching/chauffeur-kpis/")
        dismissal = self._dismissals().get()
        self.assertIsNotNone(dismissal.cleared_at)

        # The problem comes back (their legs vanish from the window): a spent
        # dismissal must not suppress the fresh episode.
        Leg.objects.filter(driver=self.idle).delete()
        html = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("drove none", html)
        self.assertNotIn("Handled (", html)
        self.assertTrue(self._flagged(html, "Ivo"))

    def test_browsing_other_windows_never_spends_a_dismissal(self):
        """Rule floors scale with the window, so a 12-day streak invisible in the
        7-day view must not clear its dismissal there — only the default window
        decides that an episode is over."""
        runner = self._driver("xd_runner", employment_type="full_time",
                              first_name="Remy")
        self._legs(runner,
                   [self.today - timedelta(days=o) for o in range(9, 21)])

        self.client.force_login(self.boss)
        self.assertIn("days in a row",
                      self.client.get("/dispatching/chauffeur-kpis/").content.decode())
        self._dismiss(runner, "no_day_off_streak")

        # 7-day view: the streak sits outside the window, so the rule is silent
        # there — the dismissal must survive untouched.
        self.client.get("/dispatching/chauffeur-kpis/?window=7")
        self.assertIsNone(self._dismissals().get().cleared_at)

        # Back on the default window the streak still fires, so it shows handled.
        html = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("Handled (", html)

    def test_handled_never_reaches_the_dispatcher_page(self):
        from dispatching.models import ChauffeurExceptionDismissal
        ChauffeurExceptionDismissal.objects.create(
            driver=self.idle, rule="never_drove", dismissed_by=self.boss,
            note="internal management note",
        )
        self.client.force_login(self.staff)
        html = self.client.get("/dispatching/chauffeur-load/").content.decode()
        self.assertNotIn("Handled (", html)
        self.assertNotIn("internal management note", html)

    def test_deactivating_a_driver_does_not_spend_the_dismissal(self):
        """A driver missing from the roster is an unevaluated episode, not an ended
        one — spending there would silently discard the note with no undo path."""
        self.client.force_login(self.boss)
        self._dismiss(self.idle, "never_drove", note="on long leave")

        self.idle.is_active = False
        self.idle.save(update_fields=["is_active"])
        self.client.get("/dispatching/chauffeur-kpis/")
        self.assertIsNone(self._dismissals().get().cleared_at)

        # Reactivated with the situation unchanged: still handled, note intact.
        self.idle.is_active = True
        self.idle.save(update_fields=["is_active"])
        html = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("Handled (1)", html)
        self.assertIn("on long leave", html)

    def test_empty_roster_does_not_mass_spend(self):
        from drivers.models import Driver
        self.client.force_login(self.boss)
        self._dismiss(self.idle, "never_drove")
        Driver.objects.update(is_active=False)
        self.client.get("/dispatching/chauffeur-kpis/")
        self.assertIsNone(self._dismissals().get().cleared_at)

    def test_dismissal_is_judged_on_the_window_it_was_made_on(self):
        """A streak that only the 90-day view can see: dismissing it there must
        survive default-window renders, and only a 90-day render may spend it."""
        from reservations.models import Leg

        runner = self._driver("xd_far", employment_type="full_time",
                              first_name="Fara")
        self._legs(runner,
                   [self.today - timedelta(days=o) for o in range(40, 52)])

        self.client.force_login(self.boss)
        self.assertIn("days in a row",
                      self.client.get("/dispatching/chauffeur-kpis/?window=90")
                      .content.decode())
        self.client.post(self.DISMISS_URL, {
            "driver_id": runner.id, "rule": "no_day_off_streak", "window": "90",
        })

        # Default-window renders see no streak for them — must not spend.
        self.client.get("/dispatching/chauffeur-kpis/")
        self.assertIsNone(self._dismissals().get().cleared_at)
        self.assertIn("Handled (",
                      self.client.get("/dispatching/chauffeur-kpis/?window=90")
                      .content.decode())

        # Episode over (the legs vanish): the 90-day render spends it.
        Leg.objects.filter(driver=runner).delete()
        self.client.get("/dispatching/chauffeur-kpis/?window=90")
        self.assertIsNotNone(self._dismissals().get().cleared_at)

    def test_malformed_ids_redirect_instead_of_500(self):
        self.client.force_login(self.boss)
        r = self.client.post(self.DISMISS_URL, {
            "driver_id": "abc", "rule": "never_drove", "window": "30",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._dismissals().count(), 0)
        r = self.client.post(self.UNDO_URL, {"dismissal_id": "abc", "window": "30"})
        self.assertEqual(r.status_code, 302)

    def test_outranked_dismissal_stays_visible_as_a_fallback_row(self):
        """Dismiss the density rule for someone whose listing is won by a streak:
        the handled line must still show (and allow undoing) the dismissal."""
        packed = self._driver("xd_both", employment_type="full_time",
                              first_name="Bora")
        days = [self.today - timedelta(days=o) for o in range(2, 14)]
        self._legs(packed, [d for d in days for _ in range(6)])   # 6 trips a day
        for name, uname in (("Eva", "xd_even_a"), ("Edd", "xd_even_b")):
            d = self._driver(uname, employment_type="full_time", first_name=name)
            spread = [self.today - timedelta(days=o) for o in range(2, 21, 2)]
            self._legs(d, [dt for dt in spread for _ in range(3)])

        self.client.force_login(self.boss)
        before = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("days in a row", before)      # the streak wins Bora's spot

        self._dismiss(packed, "days_packed_harder")
        after = self.client.get("/dispatching/chauffeur-kpis/").content.decode()
        self.assertIn("days in a row", after)       # streak entry still active
        self.assertIn("Handled (1)", after)
        self.assertIn("still applies", after)
        self.assertIsNone(self._dismissals().get().cleared_at)
