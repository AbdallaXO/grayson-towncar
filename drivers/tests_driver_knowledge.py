"""Driver knowledge (structured shifts, Stage 1, Task 9): strengths, habits,
languages and the areas a driver knows, as tags on the staff profile.

This is knowledge for people (S19): the engine does not read it. Any staff
user can add a tag with a note; only managers remove a tag or create a new
one. It is staff-only and must never reach a driver-facing page.

Run with:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_driver_knowledge
"""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from drivers import driver_knowledge
from drivers.models import Driver, DriverTag, DriverTagAssignment
from drivers.test_support import RegularShiftCacheMixin

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
        self.assertEqual(self.last_message(resp), "Keep the note under 200 characters.")
        self.assertFalse(DriverTagAssignment.objects.exists())
        self.assertEqual(self.client.get(url).status_code, 405)

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
                 "Keep the tag name under 60 characters.")):
            resp = self.client.post(self.create_url(), data)
            self.assertRedirects(resp, self.back_url(), fetch_redirect_response=False)
            self.assertEqual(self.last_message(resp), refusal)
        self.assertEqual(DriverTag.objects.count(), count)
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
