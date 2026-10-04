"""Driver knowledge (structured shifts, Stage 1, Tasks 9 and 10): strengths,
habits, languages and the areas a driver knows, as tags on the staff profile;
and the log of compliments, complaints, incidents and notes, with strikes.

This is knowledge for people (S19): the engine does not read it. Any staff
user can add a tag with a note, or an entry to the log; only managers remove
a tag, create a new one, mark a strike, or edit or delete a log entry. It is
staff-only and must never reach a driver-facing page.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_driver_knowledge
"""
import importlib
from datetime import time, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from business.datefmt import strf
from drivers import driver_knowledge
from drivers.forms import DriverLogEntryForm
from drivers.models import (STRIKE_WINDOW_DAYS, Driver, DriverLogEntry, DriverTag,
                            DriverTagAssignment)
from drivers.test_support import RegularShiftCacheMixin
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

SEEDED = {
    "strength": ["Airport pro", "Cruise port pro", "VIP & corporate", "Large groups",
                 "Car seats & families", "Long-distance trips", "Calm under pressure",
                 "Great guest reviews"],
    "habit": ["Always early", "Taps every status", "Picks up extra shifts",
              "Keeps the car spotless", "Runs late", "Slow with luggage",
              "Misses status taps", "Hard to reach by phone", "Prefers no late nights"],
    "language": ["Spanish", "Portuguese", "French", "Haitian Creole", "Arabic"],
    "area": ["Disney", "Universal", "Port Canaveral", "Downtown Orlando", "Tampa"],
}
CAUTIONS = ["Runs late", "Slow with luggage", "Misses status taps",
            "Hard to reach by phone", "Prefers no late nights"]


@override_settings(GOOGLE_MAPS_API_KEY="")
class _Base(RegularShiftCacheMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("kb_manager", password="x", first_name="Abdalla",
                                               is_staff=True, is_superuser=True)
        cls.dispatcher = User.objects.create_user("kb_dispatcher", password="x", first_name="Luis",
                                                  is_staff=True)
        cls.driver_user = User.objects.create_user("kb_driver", first_name="Sam",
                                                   last_name="Rivera")
        cls.driver = Driver.objects.create(profile=cls.driver_user, driver_type="inhouse")

    def tag(self, name):
        return DriverTag.objects.get(name=name)

    def profile_url(self):
        return reverse("driver_profile", args=[self.driver.id])

    def back_url(self):
        return self.profile_url() + "#strengths-habits"

    def add(self, name, note=""):
        return self.client.post(reverse("driver_tag_add", args=[self.driver.id]),
                                {"tag": self.tag(name).id, "note": note})

    def remove_url(self, name):
        return reverse("driver_tag_remove", args=[self.driver.id, self.tag(name).id])

    def create_url(self):
        return reverse("driver_tag_create", args=[self.driver.id])

    def assign(self, name, note="", by=None):
        return DriverTagAssignment.objects.create(driver=self.driver, tag=self.tag(name),
                                                  note=note, added_by=by)

    def last_message(self, resp):
        """The message this request added. The test client never follows the
        redirect, so messages from earlier requests are still queued ahead."""
        return [str(m) for m in get_messages(resp.wsgi_request)][-1]


class SeedTests(_Base):
    def test_seeded_tags(self):
        for category, names in SEEDED.items():
            self.assertEqual(
                list(DriverTag.objects.filter(category=category)
                     .order_by("sort_order").values_list("name", flat=True)),
                names, category)
        self.assertEqual(DriverTag.objects.count(), 27)
        self.assertEqual(
            list(DriverTag.objects.filter(polarity="caution")
                 .order_by("sort_order").values_list("name", flat=True)),
            CAUTIONS)
        airport = self.tag("Airport pro")
        self.assertEqual((airport.category, airport.polarity, airport.is_active),
                         ("strength", "positive", True))
        self.assertEqual(str(airport), "Airport pro")

    def test_seed_reverse_keeps_tags_in_use_and_reruns_cleanly(self):
        mig = importlib.import_module("drivers.migrations.0067_seed_driver_tags")
        self.assign("Airport pro")
        mig.unseed(django_apps, None)
        self.assertEqual(list(DriverTag.objects.values_list("name", flat=True)), ["Airport pro"])
        mig.seed(django_apps, None)
        mig.seed(django_apps, None)                          # re-running is a no-op
        self.assertEqual(DriverTag.objects.count(), 27)


class TagEditingTests(_Base):
    def test_dispatcher_adds_tag_with_note(self):
        self.client.force_login(self.dispatcher)
        resp = self.add("Airport pro", note="  Knows every MCO terminal ")
        self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
        a = DriverTagAssignment.objects.get(driver=self.driver)
        self.assertEqual((a.tag.name, a.note, a.added_by), ("Airport pro",
                                                             "Knows every MCO terminal",
                                                             self.dispatcher))
        self.assertIsNotNone(a.added_at)
        self.assertEqual(self.last_message(resp), "Airport pro added.")

    def test_add_twice_updates_note(self):
        self.client.force_login(self.dispatcher)
        self.add("Runs late", note="Mondays mostly")
        resp = self.add("Runs late", note="Twice in October")
        self.assertEqual(self.last_message(resp), "Updated the note on Runs late.")
        a = DriverTagAssignment.objects.get(driver=self.driver)
        self.assertEqual(a.note, "Twice in October")
        self.assertEqual(a.added_by, self.dispatcher)
        self.add("Runs late")                                # a blank note keeps the one there
        self.assertEqual(DriverTagAssignment.objects.get(driver=self.driver).note,
                         "Twice in October")
        # the module call itself is idempotent too
        again = driver_knowledge.add_tag(self.driver, self.tag("Runs late"), "Fixed", self.manager)
        self.assertEqual((again.pk, again.note), (a.pk, "Fixed"))
        self.assertEqual(DriverTagAssignment.objects.filter(driver=self.driver).count(), 1)

    def test_add_needs_a_tag_and_a_short_note(self):
        self.client.force_login(self.dispatcher)
        url = reverse("driver_tag_add", args=[self.driver.id])
        resp = self.client.post(url, {"tag": "", "note": "x"})
        self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
        self.assertEqual(self.last_message(resp), "Pick a tag to add.")
        resp = self.client.post(url, {"tag": self.tag("Disney").id, "note": "x" * 201})
        self.assertEqual(self.last_message(resp), "Keep the note to 200 characters or fewer.")
        self.assertFalse(DriverTagAssignment.objects.exists())
        self.assertEqual(self.client.get(url).status_code, 405)
        resp = self.client.post(url, {"tag": self.tag("Disney").id, "note": "x" * 200})
        self.assertEqual(self.last_message(resp), "Disney added.")       # 200 exactly is fine

    def test_dispatcher_cannot_remove(self):
        self.assign("Runs late")
        self.client.force_login(self.dispatcher)
        resp = self.client.post(self.remove_url("Runs late"))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(DriverTagAssignment.objects.filter(driver=self.driver).exists())

    def test_manager_removes(self):
        self.assign("Runs late", note="Mondays")
        self.client.force_login(self.manager)
        resp = self.client.post(self.remove_url("Runs late"))
        self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
        self.assertFalse(DriverTagAssignment.objects.filter(driver=self.driver).exists())
        self.assertTrue(DriverTag.objects.filter(name="Runs late").exists())
        self.assertEqual(self.last_message(resp), "Runs late removed.")

    def test_manager_creates_tag_case_insensitive_unique(self):
        self.client.force_login(self.manager)
        resp = self.client.post(self.create_url(), {"name": "  Night   owl ",
                                                    "category": "habit", "polarity": "caution"})
        self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
        owl = DriverTag.objects.get(name="Night owl")
        self.assertEqual((owl.category, owl.polarity, owl.is_active, owl.created_by),
                         ("habit", "caution", True, self.manager))
        self.assertGreater(owl.sort_order, self.tag("Prefers no late nights").sort_order)
        a = DriverTagAssignment.objects.get(driver=self.driver)
        self.assertEqual((a.tag, a.added_by), (owl, self.manager))
        self.assertEqual(self.last_message(resp), "Night owl created and added.")

        count = DriverTag.objects.count()
        for clash in ("night OWL", "AIRPORT pro"):
            resp = self.client.post(self.create_url(), {"name": clash, "category": "strength",
                                                        "polarity": "positive"})
            self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
            self.assertEqual(self.last_message(resp), "That tag already exists.")
        self.assertEqual(DriverTag.objects.count(), count)
        with self.assertRaisesMessage(ValueError, "That tag already exists."):
            driver_knowledge.create_tag("airport Pro", "strength", "positive", self.manager)

    def test_create_refuses_a_blank_name_or_unknown_choice(self):
        self.client.force_login(self.manager)
        count = DriverTag.objects.count()
        for data, refusal in (
                ({"name": "  ", "category": "habit", "polarity": "positive"},
                 "Give the new tag a name."),
                ({"name": "Night owl", "category": "mood", "polarity": "positive"},
                 "Pick what kind of tag it is."),
                ({"name": "Night owl", "category": "habit", "polarity": "meh"},
                 "Pick whether it's a plus or a caution."),
                ({"name": "N" * 61, "category": "habit", "polarity": "positive"},
                 "Keep the tag name to 60 characters or fewer.")):
            resp = self.client.post(self.create_url(), data)
            self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
            self.assertEqual(self.last_message(resp), refusal)
        self.assertEqual(DriverTag.objects.count(), count)
        self.assertFalse(DriverTagAssignment.objects.exists())
        tag = driver_knowledge.create_tag("N" * 60, "habit", "positive", self.manager)
        self.assertEqual(len(tag.name), 60)                  # 60 exactly is fine

    def test_database_refuses_the_same_name_in_another_case(self):
        """Two managers saving "Night owl" and "night owl" at the same moment
        both pass the Python check; the index on lower(name) stops the second."""
        DriverTag.objects.create(name="Night owl", category="habit")
        with self.assertRaises(IntegrityError), transaction.atomic():
            DriverTag.objects.create(name="NIGHT OWL", category="habit")
        self.assertEqual(DriverTag.objects.filter(name__iexact="night owl").count(), 1)

    def test_create_says_when_the_clash_is_switched_off(self):
        DriverTag.objects.filter(name="Tampa").update(is_active=False)
        self.client.force_login(self.manager)
        resp = self.client.post(self.create_url(), {"name": "tampa", "category": "area",
                                                    "polarity": "positive"})
        self.assertEqual(self.last_message(resp), driver_knowledge.TAG_SWITCHED_OFF)
        self.assertIn("switched off", driver_knowledge.TAG_SWITCHED_OFF)
        self.assertEqual(DriverTag.objects.filter(name__iexact="tampa").count(), 1)
        self.assertFalse(DriverTagAssignment.objects.exists())

    def test_dispatcher_cannot_create_tag(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.post(self.create_url(), {"name": "Night owl", "category": "habit",
                                                    "polarity": "caution"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(DriverTag.objects.filter(name="Night owl").exists())

    def test_driver_cannot_post_tags(self):
        self.client.force_login(self.driver_user)
        self.assertEqual(self.add("Airport pro").status_code, 403)
        self.assertFalse(DriverTagAssignment.objects.exists())


class TagCardTests(_Base):
    def test_card_groups_and_caution_style(self):
        self.assign("Disney")
        self.assign("Runs late", note="Mondays mostly", by=self.dispatcher)
        self.assign("Airport pro", note="Knows every terminal", by=self.manager)
        self.assign("Spanish")
        self.assign("Always early")
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        groups = [(label, [a.tag.name for a in rows]) for label, rows in resp.context["tag_groups"]]
        self.assertEqual(groups, [("Strengths", ["Airport pro"]),
                                  ("Habits", ["Always early", "Runs late"]),
                                  ("Languages", ["Spanish"]),
                                  ("Knows the area", ["Disney"])])
        self.assertContains(resp, "Strengths &amp; habits")
        self.assertContains(resp, 'class="kb-chip kb-positive" data-tag="Airport pro"')
        self.assertContains(resp, 'class="kb-chip kb-caution" data-tag="Runs late"')
        self.assertContains(resp, 'role="img" aria-label="Caution"', count=1)
        self.assertContains(resp, "Mondays mostly")
        self.assertContains(resp, "Added by Luis on")
        # a dispatcher adds but never removes or invents tags
        self.assertContains(resp, reverse("driver_tag_add", args=[self.driver.id]))
        self.assertNotContains(resp, self.remove_url("Runs late"))
        self.assertNotContains(resp, self.create_url())

        self.client.force_login(self.manager)
        resp = self.client.get(self.profile_url())
        self.assertContains(resp, self.remove_url("Runs late"))
        self.assertContains(resp, self.create_url())
        # in edit mode too, and never inside the profile edit form
        html = self.client.get(self.profile_url(), {"edit": "1"}).content.decode()
        self.assertIn("Strengths &amp; habits", html)
        edit_form = html[html.index('<form method="post" enctype="multipart/form-data"'):]
        edit_form = edit_form[:edit_form.index("</form>")]
        self.assertNotIn("kb-chip", edit_form)
        self.assertNotIn(reverse("driver_tag_add", args=[self.driver.id]), edit_form)

    def test_empty_card(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        self.assertEqual(resp.context["tag_groups"], [])
        self.assertContains(resp, "Nothing noted yet.")

    def test_tags_by_category_is_one_query(self):
        self.assign("Runs late", by=self.dispatcher)
        self.assign("Spanish", by=self.manager)
        with self.assertNumQueries(1):
            groups = driver_knowledge.tags_by_category(self.driver)
            [(a.tag.name, a.added_by and a.added_by.first_name) for _, rows in groups for a in rows]

    def test_inactive_tag_not_offered_but_still_shown(self):
        self.assign("Spanish", note="Fluent")
        DriverTag.objects.filter(name__in=["Spanish", "Tampa"]).update(is_active=False)
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        self.assertContains(resp, 'data-tag="Spanish"')
        offered = [t.name for _, tags in resp.context["available_tags"] for t in tags]
        self.assertNotIn("Spanish", offered)                 # inactive, and already on him
        self.assertNotIn("Tampa", offered)                   # inactive
        self.assertIn("Disney", offered)
        self.assertEqual([label for label, _ in resp.context["available_tags"]],
                         ["Strengths", "Habits", "Languages", "Knows the area"])
        resp = self.add("Tampa")
        self.assertEqual(self.last_message(resp), "That tag is no longer in use.")
        self.assertFalse(DriverTagAssignment.objects.filter(tag__name="Tampa").exists())

    def test_tags_never_on_driver_app(self):
        secret = driver_knowledge.create_tag("Zq office-only tag", "habit", "caution",
                                             self.manager)
        driver_knowledge.add_tag(self.driver, secret, "Zq office-only note", self.manager)
        driver_knowledge.add_tag(self.driver, self.tag("Slow with luggage"),
                                 "Zq second note", self.dispatcher)
        self.client.force_login(self.driver_user)
        for name in ("drivers_dashboard", "schedule", "completed_trips", "driver_my_details"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            for text in ("Zq office-only", "Zq second note", "Slow with luggage",
                         "Strengths &amp; habits", "kb-chip"):
                self.assertNotContains(resp, text, msg_prefix=name)


class TagAdminTests(_Base):
    """Managers fix, switch off or put back a tag in the Django admin; the
    profile card has no way to do that. Dispatchers can't reach it there."""

    def test_manager_lists_and_switches_off_a_tag(self):
        self.assign("Runs late")
        self.client.force_login(self.manager)
        resp = self.client.get(reverse("admin:drivers_drivertag_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Airport pro")
        rows = {t.name: t.n_drivers for t in resp.context["cl"].result_list}
        self.assertEqual((rows["Runs late"], rows["Airport pro"]), (1, 0))
        tampa = self.tag("Tampa")
        resp = self.client.post(reverse("admin:drivers_drivertag_change", args=[tampa.id]), {
            "name": "Tampa", "category": "area", "polarity": "positive", "description": "",
            "sort_order": tampa.sort_order,                  # is_active left unticked
        })
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.tag("Tampa").is_active)

    def test_admin_refuses_a_name_in_another_case(self):
        self.client.force_login(self.manager)
        resp = self.client.post(reverse("admin:drivers_drivertag_add"), {
            "name": "AIRPORT PRO", "category": "strength", "polarity": "positive",
            "description": "", "is_active": "on", "sort_order": 0,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("That tag already exists.", str(resp.context["adminform"].form.errors))
        self.assertEqual(DriverTag.objects.filter(name__iexact="airport pro").count(), 1)

    def test_admin_add_records_who_made_it(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("admin:drivers_drivertag_add"), {
            "name": "Night owl", "category": "habit", "polarity": "caution",
            "description": "", "is_active": "on", "sort_order": 0,
        })
        self.assertEqual(self.tag("Night owl").created_by, self.manager)

    def test_dispatcher_cannot_open_it(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.get(reverse("admin:drivers_drivertag_changelist"))
        self.assertEqual(resp.status_code, 403)


# ── The log (Task 10) ───────────────────────────────────────────────────────

MCO = "Orlando International Airport (MCO), 1 Jeff Fuqua Blvd, Orlando, FL, USA"
POLY = "Disney's Polynesian Village Resort, 1600 Seven Seas Dr, Lake Buena Vista, FL, USA"


class _LogBase(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.route = Route.objects.create(origin=Location.objects.create(name="MCO"),
                                         destination=Location.objects.create(name="Disney"),
                                         inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(vehicle=cls.suv, route=cls.route,
                                       oneway_price=Decimal("100.00"),
                                       round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(first_name="John", last_name="Doe",
                                               email="j@example.com", phone_number="5551234567")
        other_user = User.objects.create_user("kb_other", first_name="Ana", last_name="Lopez")
        cls.other_driver = Driver.objects.create(profile=other_user, driver_type="inhouse")

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def days_ago(self, n):
        return self.today - timedelta(days=n)

    def leg(self, day, hh=5, mm=0, driver=None, status="confirmed", res_status="confirmed",
            pickup=MCO, dropoff=POLY):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate, vehicle=self.suv,
            base_price=Decimal("100.00"), total_price=Decimal("100.00"), status=res_status)
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=time(hh, mm),
            driver=driver or self.driver, pickup_location=pickup, dropoff_location=dropoff,
            route=self.route, status=status)

    def entry(self, kind="note", days_ago=1, strike=False, summary=None, driver=None, **kw):
        return DriverLogEntry.objects.create(
            driver=driver or self.driver, kind=kind, occurred_on=self.days_ago(days_ago),
            is_strike=strike, summary=summary or f"A {kind}", **kw)

    def log_back_url(self):
        return self.profile_url() + "#driver-log"

    def add_url(self):
        return reverse("driver_log_add", args=[self.driver.id])

    def edit_url(self, entry):
        return reverse("driver_log_edit", args=[self.driver.id, entry.id])

    def delete_url(self, entry):
        return reverse("driver_log_delete", args=[self.driver.id, entry.id])

    def post_entry(self, **data):
        data.setdefault("kind", "note")
        data.setdefault("occurred_on", self.today.isoformat())
        data.setdefault("summary", "Something to remember")
        data.setdefault("severity", "")
        data.setdefault("details", "")
        return self.client.post(self.add_url(), data)


class LogEntryTests(_LogBase):
    def test_dispatcher_logs_compliment_with_trip(self):
        leg = self.leg(self.days_ago(3))
        self.client.force_login(self.dispatcher)
        resp = self.post_entry(kind="compliment", occurred_on=self.days_ago(2).isoformat(),
                               leg=leg.id, summary="Guest wrote in to thank him",
                               details="Carried every bag to the room.")
        self.assertRedirects(resp, self.log_back_url(), fetch_redirect_response=False)
        self.assertEqual(self.last_message(resp), "Added to the log.")
        e = DriverLogEntry.objects.get()
        self.assertEqual((e.driver, e.kind, e.occurred_on, e.leg, e.summary, e.details),
                         (self.driver, "compliment", self.days_ago(2), leg,
                          "Guest wrote in to thank him", "Carried every bag to the room."))
        self.assertEqual((e.is_strike, e.severity, e.logged_by, e.updated_by),
                         (False, "", self.dispatcher, None))
        self.assertIsNotNone(e.logged_at)

        resp = self.client.get(self.profile_url())
        self.assertEqual([x.pk for x in resp.context["log_entries"]], [e.pk])
        self.assertContains(resp, 'id="driver-log"')
        self.assertContains(resp, "Guest wrote in to thank him")
        self.assertContains(resp, "Carried every bag to the room.")
        self.assertContains(resp, "Logged by Luis")
        self.assertContains(resp, reverse("reservation_details", args=[leg.reservation.uuid]))
        self.assertContains(resp, 'class="kb-kind kb-kind-compliment"')
        # a dispatcher adds but never edits or deletes
        self.assertNotContains(resp, self.edit_url(e))
        self.assertNotContains(resp, self.delete_url(e))

    def test_occurred_on_defaults_to_today(self):
        self.client.force_login(self.dispatcher)
        form = self.client.get(self.profile_url()).context["log_form"]
        self.assertEqual(form["occurred_on"].value(), self.today)
        self.assertEqual(list(form.fields)[:4], ["kind", "occurred_on", "severity", "leg"])
        self.assertEqual(form.fields["leg"].label, "Trip (optional)")

    def test_dispatcher_cannot_mark_strike(self):
        self.client.force_login(self.dispatcher)
        form = self.client.get(self.profile_url()).context["log_form"]
        self.assertNotIn("is_strike", form.fields)
        resp = self.post_entry(kind="complaint", is_strike="on", summary="Late to MCO")
        self.assertRedirects(resp, self.log_back_url(), fetch_redirect_response=False)
        e = DriverLogEntry.objects.get()
        self.assertEqual((e.kind, e.is_strike), ("complaint", False))
        self.assertEqual(driver_knowledge.strike_count(self.driver, self.today), 0)

    def test_manager_marks_strike_counted_for_12_months(self):
        self.assertEqual(STRIKE_WINDOW_DAYS, 365)
        self.client.force_login(self.manager)
        self.assertIn("is_strike", self.client.get(self.profile_url()).context["log_form"].fields)
        resp = self.post_entry(kind="incident", severity="serious", is_strike="on",
                               summary="Clipped a mirror at the port")
        self.assertRedirects(resp, self.log_back_url(), fetch_redirect_response=False)
        e = DriverLogEntry.objects.get()
        self.assertEqual((e.kind, e.severity, e.is_strike, e.logged_by),
                         ("incident", "serious", True, self.manager))
        self.assertEqual(driver_knowledge.strike_count(self.driver, self.today), 1)

        self.entry("complaint", days_ago=400, strike=True)       # too old
        self.entry("complaint", days_ago=365, strike=True)       # exactly a year: out
        self.entry("complaint", days_ago=364, strike=True)       # still in
        self.entry("complaint", days_ago=10)                     # not a strike
        self.entry("complaint", days_ago=10, strike=True, driver=self.other_driver)
        self.assertEqual(driver_knowledge.strike_count(self.driver, self.today), 2)
        resp = self.client.get(self.profile_url())
        self.assertEqual(resp.context["strike_count"], 2)

    def test_strike_must_be_complaint_or_incident(self):
        self.client.force_login(self.manager)
        for kind in ("compliment", "note"):
            resp = self.post_entry(kind=kind, is_strike="on", summary="Kept")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.context["form"].errors,
                             {"is_strike": ["A strike must be a complaint or an incident."]})
        self.assertFalse(DriverLogEntry.objects.exists())
        self.assertContains(resp, 'value="Kept"')                # what was typed comes back
        for kind in ("complaint", "incident"):
            self.assertEqual(self.post_entry(kind=kind, is_strike="on").status_code, 302)
        self.assertEqual(DriverLogEntry.objects.filter(is_strike=True).count(), 2)

    def test_severity_only_on_complaints_and_incidents(self):
        self.client.force_login(self.dispatcher)
        self.post_entry(kind="compliment", severity="serious", summary="Kind words")
        self.post_entry(kind="complaint", severity="minor", summary="Music too loud")
        self.assertEqual(dict(DriverLogEntry.objects.values_list("summary", "severity")),
                         {"Kind words": "", "Music too loud": "minor"})

    def test_future_date_refused(self):
        self.client.force_login(self.dispatcher)
        resp = self.post_entry(occurred_on=(self.today + timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].errors,
                         {"occurred_on": ["That date is in the future."]})
        self.assertFalse(DriverLogEntry.objects.exists())
        self.assertEqual(self.post_entry(occurred_on=self.today.isoformat()).status_code, 302)

    def test_summary_is_needed(self):
        self.client.force_login(self.dispatcher)
        resp = self.post_entry(summary="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary", resp.context["form"].errors)
        self.assertFalse(DriverLogEntry.objects.exists())
        self.assertEqual(self.client.get(self.add_url()).status_code, 405)

    def test_trip_picker_only_this_drivers_recent_trips(self):
        newest = self.leg(self.today, 9, 30)
        recent = self.leg(self.days_ago(3), 5, 0)
        edge = self.leg(self.days_ago(60), 14, 15)
        self.leg(self.days_ago(61))                                   # too old
        self.leg(self.today + timedelta(days=1))                      # not driven yet
        self.leg(self.days_ago(2), status="cancelled")                # leg cancelled
        self.leg(self.days_ago(2), res_status="cancelled")            # trip cancelled
        theirs = self.leg(self.days_ago(2), driver=self.other_driver)  # someone else's
        self.assertEqual(list(driver_knowledge.recent_legs_for(self.driver, self.today)),
                         [newest, recent, edge])

        form = DriverLogEntryForm(self.driver, self.dispatcher)
        self.assertEqual(list(form.fields["leg"].queryset), [newest, recent, edge])
        labels = dict((c.value, label) for c, label in list(form.fields["leg"].choices)[1:])
        self.assertEqual(labels[recent.pk],
                         f"{strf(recent.pickup_date, '%b %-d')} · 5:00 AM · "
                         "MCO → Disney's Polynesian Village Resort")
        self.assertEqual(labels[edge.pk].split(" · ")[1], "2:15 PM")

        self.client.force_login(self.dispatcher)
        resp = self.post_entry(leg=theirs.id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("leg", resp.context["form"].errors)
        self.assertFalse(DriverLogEntry.objects.exists())

        # editing keeps an entry's own trip even once it is older than 60 days
        old = self.leg(self.days_ago(90))
        e = self.entry("compliment", days_ago=90, leg=old)
        form = DriverLogEntryForm(self.driver, self.manager, instance=e)
        self.assertEqual(list(form.fields["leg"].queryset), [newest, recent, edge, old])

    def test_filter_by_kind(self):
        for kind in ("compliment", "complaint", "incident", "note"):
            self.entry(kind, summary=f"Zq {kind} entry")
        self.entry("complaint", summary="Zq other driver", driver=self.other_driver)
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        self.assertEqual(resp.context["log_filter"], "")
        self.assertEqual(len(resp.context["log_entries"]), 4)
        self.assertNotContains(resp, "Zq other driver")
        for kind in ("compliment", "complaint", "incident", "note"):
            self.assertContains(resp, f'href="?log={kind}#driver-log"')
        for kind in ("compliment", "complaint", "incident", "note"):
            resp = self.client.get(self.profile_url(), {"log": kind})
            self.assertEqual(resp.context["log_filter"], kind)
            self.assertEqual([e.kind for e in resp.context["log_entries"]], [kind])
            self.assertContains(resp, f"Zq {kind} entry")
        self.assertNotContains(resp, "Zq compliment entry")
        resp = self.client.get(self.profile_url(), {"log": "bogus"})
        self.assertEqual((resp.context["log_filter"], len(resp.context["log_entries"])), ("", 4))
        # in edit mode the filter links keep the page in edit mode
        self.client.force_login(self.manager)
        resp = self.client.get(self.profile_url(), {"edit": "1"})
        self.assertContains(resp, 'href="?edit=1&amp;log=incident#driver-log"')

    def test_timeline_is_newest_first(self):
        old = self.entry(days_ago=20, summary="Old")
        new = self.entry(days_ago=2, summary="New")
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        self.assertEqual([e.pk for e in resp.context["log_entries"]], [new.pk, old.pk])
        self.assertEqual(list(driver_knowledge.log_entries(self.driver, "note")), [new, old])

    def test_manager_edits_and_deletes(self):
        leg = self.leg(self.days_ago(4))
        e = self.entry("complaint", days_ago=5, summary="Late to MCO", logged_by=self.dispatcher)
        self.client.force_login(self.manager)
        resp = self.client.get(self.profile_url())
        self.assertContains(resp, self.edit_url(e))
        self.assertContains(resp, self.delete_url(e))

        resp = self.client.get(self.edit_url(e))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["form"].instance, e)
        self.assertContains(resp, "Late to MCO")
        resp = self.client.post(self.edit_url(e), {
            "kind": "incident", "occurred_on": self.days_ago(4).isoformat(), "severity": "minor",
            "leg": leg.id, "summary": "Late to MCO, guest missed check-in",
            "details": "Traffic on 528.", "is_strike": "on"})
        self.assertRedirects(resp, self.log_back_url(), fetch_redirect_response=False)
        self.assertEqual(self.last_message(resp), "Log entry updated.")
        e.refresh_from_db()
        self.assertEqual((e.kind, e.occurred_on, e.severity, e.leg, e.summary, e.is_strike),
                         ("incident", self.days_ago(4), "minor", leg,
                          "Late to MCO, guest missed check-in", True))
        self.assertEqual((e.logged_by, e.updated_by), (self.dispatcher, self.manager))
        self.assertContains(self.client.get(self.profile_url()), "edited by Abdalla")

        # an invalid edit comes back with its errors and changes nothing
        resp = self.client.post(self.edit_url(e), {
            "kind": "note", "occurred_on": self.today.isoformat(), "severity": "",
            "summary": "x", "details": "", "is_strike": "on"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("is_strike", resp.context["form"].errors)
        e.refresh_from_db()
        self.assertEqual(e.kind, "incident")

        self.assertEqual(self.client.get(self.delete_url(e)).status_code, 405)
        resp = self.client.post(self.delete_url(e))
        self.assertRedirects(resp, self.log_back_url(), fetch_redirect_response=False)
        self.assertEqual(self.last_message(resp), "Log entry deleted.")
        self.assertFalse(DriverLogEntry.objects.exists())

    def test_entry_must_belong_to_the_driver_in_the_address(self):
        theirs = self.entry(driver=self.other_driver)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.edit_url(theirs)).status_code, 404)
        self.assertEqual(self.client.post(self.delete_url(theirs)).status_code, 404)
        self.assertTrue(DriverLogEntry.objects.filter(pk=theirs.pk).exists())

    def test_dispatcher_cannot_edit_or_delete(self):
        e = self.entry("complaint", summary="Late to MCO")
        self.client.force_login(self.dispatcher)
        self.assertEqual(self.client.get(self.edit_url(e)).status_code, 403)
        resp = self.client.post(self.edit_url(e), {
            "kind": "note", "occurred_on": self.today.isoformat(), "summary": "Changed"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.post(self.delete_url(e)).status_code, 403)
        e.refresh_from_db()
        self.assertEqual((e.kind, e.summary), ("complaint", "Late to MCO"))

    def test_driver_cannot_post_to_the_log(self):
        self.client.force_login(self.driver_user)
        self.assertEqual(self.post_entry().status_code, 403)
        self.assertFalse(DriverLogEntry.objects.exists())

    def test_log_card_in_both_modes_outside_the_edit_form(self):
        self.entry("note", summary="Zq in the log")
        self.client.force_login(self.manager)
        html = self.client.get(self.profile_url(), {"edit": "1"}).content.decode()
        self.assertIn('id="driver-log"', html)
        self.assertIn("Zq in the log", html)
        edit_form = html[html.index('<form method="post" enctype="multipart/form-data"'):]
        edit_form = edit_form[:edit_form.index("</form>")]
        self.assertNotIn("Zq in the log", edit_form)
        self.assertNotIn(self.add_url(), edit_form)

    def test_empty_log(self):
        self.client.force_login(self.dispatcher)
        resp = self.client.get(self.profile_url())
        self.assertEqual(list(resp.context["log_entries"]), [])
        self.assertContains(resp, "Nothing in the log yet.")
        self.assertContains(resp, "Add to the log")
        self.assertNotContains(resp, "data-strikes=")

    def test_log_never_on_driver_app(self):
        leg = self.leg(self.days_ago(1))
        self.entry("complaint", strike=True, summary="Zq office-only summary",
                   details="Zq office-only details", leg=leg)
        self.entry("compliment", summary="Zq kind words")
        self.client.force_login(self.driver_user)
        for name in ("drivers_dashboard", "schedule", "completed_trips", "driver_my_details"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            for text in ("Zq office-only", "Zq kind words", "data-strikes=", 'id="driver-log"',
                         "in the last 12 months", "kb-kind"):
                self.assertNotContains(resp, text, msg_prefix=name)

    def test_hero_shows_strike_pill(self):
        def hero(resp):
            html = resp.content.decode()
            start = html.index('class="profile-hero')
            return html[start:html.index('class="stat-tile', start)]

        self.client.force_login(self.dispatcher)
        self.entry("complaint", days_ago=400, strike=True)       # too old to count
        resp = self.client.get(self.profile_url())
        self.assertNotIn("data-strikes=", hero(resp))
        self.assertNotContains(resp, "in the last 12 months")

        self.entry("complaint", days_ago=30, strike=True)
        resp = self.client.get(self.profile_url())
        self.assertIn('class="strike-pill strike-amber" data-strikes="1"', hero(resp))
        self.assertContains(resp, "1 strike in the last 12 months", count=2)   # hero + card

        self.entry("incident", days_ago=3, strike=True)
        resp = self.client.get(self.profile_url())
        self.assertIn('class="strike-pill strike-amber" data-strikes="2"', hero(resp))
        self.assertContains(resp, "2 strikes in the last 12 months", count=2)

        self.entry("incident", days_ago=1, strike=True)
        resp = self.client.get(self.profile_url())
        self.assertIn('class="strike-pill strike-red" data-strikes="3"', hero(resp))
        self.assertContains(resp, 'class="strike-pill strike-red" data-strikes="3"', count=2)
        self.assertContains(resp, 'class="kb-strike-badge"', count=4)  # each strike entry
