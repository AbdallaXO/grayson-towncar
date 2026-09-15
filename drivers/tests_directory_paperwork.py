"""Driver directory: paperwork states, the tiles, and the paperwork filter.

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_directory_paperwork
"""
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers import paperwork
from drivers.models import Driver

TODAY = date(2026, 9, 14)


def _photo(name="license.jpg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0 fake jpeg bytes", content_type="image/jpeg")


def _driver(username, first_name=None, **fields):
    user = User.objects.create_user(
        username=username, first_name=first_name or username.title(),
        email=fields.pop("email", ""),
    )
    fields.setdefault("driver_type", "inhouse")
    return Driver.objects.create(profile=user, **fields)


class PaperworkStateTests(TestCase):
    def test_nothing_on_file_is_missing(self):
        d = _driver("blank")
        s = paperwork.summarize(d, TODAY)
        self.assertEqual([doc["state"] for doc in s["docs"]], ["missing"] * 3)
        self.assertEqual(s["flags"], {"missing_license", "missing_permit"})
        self.assertEqual(s["accent"], "")
        self.assertEqual(s["owed"], ["driver's license", "chauffeur permit"])

    def test_photo_without_details_needs_a_look_not_missing(self):
        d = _driver("photo", license_scan=_photo())
        s = paperwork.summarize(d, TODAY)
        lic = s["docs"][0]
        self.assertEqual(lic["state"], "photo_only")
        self.assertEqual(lic["detail"], "photo sent, details not entered")
        self.assertTrue(lic["scan_url"])
        self.assertIn("attention", s["flags"])
        self.assertNotIn("missing_license", s["flags"])
        self.assertIn("missing_permit", s["flags"])
        self.assertEqual(s["accent"], "amber")
        # The driver did their part — nothing to text them for on the license.
        self.assertEqual(s["owed"], ["chauffeur permit"])

    def test_expiration_states_and_wording(self):
        d = _driver("dates", license_expiration=TODAY + timedelta(days=400))
        self.assertEqual(paperwork.summarize(d, TODAY)["docs"][0]["state"], "on_file")
        self.assertEqual(paperwork.summarize(d, TODAY)["docs"][0]["detail"], "expires Oct 19, 2027 · no photo")

        d.license_expiration = TODAY + timedelta(days=12)
        doc = paperwork.summarize(d, TODAY)["docs"][0]
        self.assertEqual(doc["state"], "expiring")
        self.assertEqual(doc["detail"], "expires in 12 days")

        d.license_expiration = TODAY
        self.assertEqual(paperwork.summarize(d, TODAY)["docs"][0]["detail"], "expires today")

        d.license_expiration = TODAY - timedelta(days=3)
        s = paperwork.summarize(d, TODAY)
        self.assertEqual(s["docs"][0]["state"], "expired")
        self.assertEqual(s["docs"][0]["detail"], "expired Sep 11, 2026")
        self.assertIn("expiring", s["flags"])
        self.assertEqual(s["accent"], "red")

    def test_complete_means_license_and_permit_dated_and_not_expired(self):
        d = _driver(
            "done",
            license_expiration=TODAY + timedelta(days=900),
            chauffeur_permit_expiration=TODAY + timedelta(days=20),  # expiring, still on file
        )
        s = paperwork.summarize(d, TODAY)
        self.assertIn("complete", s["flags"])
        self.assertIn("expiring", s["flags"])
        d.chauffeur_permit_expiration = TODAY - timedelta(days=1)
        self.assertNotIn("complete", paperwork.summarize(d, TODAY)["flags"])

    def test_permit_number_mismatch_needs_a_look(self):
        d = _driver(
            "mismatch",
            license_number="D123", chauffeur_permit_fdl_number="D999",
            license_expiration=TODAY + timedelta(days=900),
            chauffeur_permit_expiration=TODAY + timedelta(days=900),
        )
        s = paperwork.summarize(d, TODAY)
        permit = s["docs"][1]
        self.assertTrue(permit["mismatch"])
        self.assertIn("number doesn't match the license", permit["detail"])
        self.assertIn("attention", s["flags"])
        self.assertEqual(s["accent"], "red")

    def test_dot_card_is_shown_but_never_missing(self):
        d = _driver(
            "nodot",
            license_expiration=TODAY + timedelta(days=900),
            chauffeur_permit_expiration=TODAY + timedelta(days=900),
        )
        s = paperwork.summarize(d, TODAY)
        self.assertEqual(s["docs"][2]["state"], "missing")
        self.assertEqual(s["flags"], {"complete"})
        self.assertEqual(s["owed"], [])

    def test_ask_link_names_what_is_owed_and_needs_a_phone(self):
        d = _driver("george", first_name="George", phone_number="407-555-0100")
        href = paperwork.ask_sms_href(d, paperwork.summarize(d, TODAY))
        self.assertTrue(href.startswith("sms:+14075550100?body="))
        self.assertIn("Hi%20George", href)
        self.assertIn("driver%27s%20license%20and%20chauffeur%20permit", href)
        self.assertIn("tap%20Documents", href)

        d.phone_number = ""
        self.assertEqual(paperwork.ask_sms_href(d, paperwork.summarize(d, TODAY)), "")

        d.phone_number = "407-555-0100"
        d.license_expiration = TODAY + timedelta(days=900)
        d.chauffeur_permit_expiration = TODAY + timedelta(days=900)
        self.assertEqual(paperwork.ask_sms_href(d, paperwork.summarize(d, TODAY)), "")

    def test_first_name_falls_back_to_a_capitalised_username(self):
        user = User.objects.create_user(username="neuma")
        d = Driver.objects.create(profile=user, driver_type="inhouse", phone_number="4075550100")
        self.assertEqual(paperwork.first_name_for(d), "Neuma")
        self.assertIn("Hi%20Neuma", paperwork.ask_sms_href(d, paperwork.summarize(d, TODAY)))
        user2 = User.objects.create_user(username="AldoH")
        d2 = Driver.objects.create(profile=user2, driver_type="inhouse")
        self.assertEqual(paperwork.first_name_for(d2), "AldoH")

    def test_roster_counts(self):
        far = TODAY + timedelta(days=900)
        rows = [
            _driver("a"),  # missing both
            _driver("b", license_expiration=far),  # missing permit only
            _driver("c", license_expiration=far, chauffeur_permit_expiration=far),  # complete
            _driver("d", license_scan=_photo(), chauffeur_permit_expiration=TODAY - timedelta(days=2)),
        ]
        counts = paperwork.roster_counts([paperwork.summarize(r, TODAY) for r in rows])
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["filters"], {
            "missing_license": 1, "missing_permit": 2, "expiring": 1,
            "attention": 1, "complete": 1,
        })
        self.assertEqual(counts["on_file"], {"license": 2, "permit": 2, "dot": 0})


class DirectoryViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(username="dispatcher", password="x", is_staff=True)
        far = timezone.localdate() + timedelta(days=900)
        cls.complete = _driver(
            "complete", first_name="Miguel",
            license_expiration=far, chauffeur_permit_expiration=far, phone_number="321-555-0101",
        )
        cls.missing = _driver("missing", first_name="Neuma", phone_number="407-555-0102")
        cls.photo_only = _driver("photoonly", first_name="Syed", license_scan=_photo())
        cls.inactive_missing = _driver("gone", first_name="Alex", is_active=False)
        cls.operator = _driver(
            "operator", first_name="Anthony", driver_type="affiliate", portal_role="operator",
        )

    def setUp(self):
        self.client.force_login(self.staff)

    def _get(self, **params):
        return self.client.get(reverse("drivers_extend"), params)

    def test_staff_only(self):
        self.client.logout()
        civilian = User.objects.create_user(username="civ", password="x")
        self.client.force_login(civilian)
        self.assertEqual(self._get().status_code, 302)

    def test_tiles_count_active_chauffeurs_only(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        tiles = {t["key"]: t["count"] for t in r.context["paperwork_tiles"]}
        # `missing` only — the inactive driver and the operator are not counted.
        self.assertEqual(tiles["missing_license"], 1)
        self.assertEqual(tiles["missing_permit"], 2)  # missing + photo_only
        self.assertEqual(tiles["attention"], 1)
        self.assertEqual(tiles["complete"], 1)
        self.assertEqual(tiles["expiring"], 0)
        self.assertEqual(r.context["chauffeur_total"], 3)
        self.assertEqual(r.context["paperwork_on_file"]["license"], 1)

    def test_tile_link_shows_exactly_the_drivers_it_counted(self):
        r = self._get(paperwork="missing_license", active_only="1")
        self.assertEqual([d.profile.username for d in r.context["drivers"]], ["missing"])

    def test_paperwork_filter_never_lists_operators_but_can_list_inactive(self):
        r = self._get(paperwork="missing_license")
        self.assertEqual(
            sorted(d.profile.username for d in r.context["drivers"]), ["gone", "missing"],
        )

    def test_attention_and_complete_filters(self):
        r = self._get(paperwork="attention")
        self.assertEqual([d.profile.username for d in r.context["drivers"]], ["photoonly"])
        r = self._get(paperwork="complete")
        self.assertEqual([d.profile.username for d in r.context["drivers"]], ["complete"])

    def test_unknown_paperwork_value_is_ignored(self):
        r = self._get(paperwork="bogus")
        self.assertEqual(r.context["paperwork_filter"], "")
        self.assertEqual(len(r.context["drivers"]), 5)

    def test_ask_link_only_for_active_chauffeurs_who_owe_something(self):
        r = self._get()
        by_name = {d.profile.username: d for d in r.context["drivers"]}
        self.assertTrue(by_name["missing"].ask_href.startswith("sms:+14075550102"))
        self.assertEqual(by_name["complete"].ask_href, "")
        self.assertEqual(by_name["gone"].ask_href, "")
        self.assertEqual(by_name["operator"].ask_href, "")
        self.assertContains(r, "Text Neuma to ask")

    def test_search_finds_a_phone_number(self):
        r = self._get(search="555-0101")
        self.assertEqual([d.profile.username for d in r.context["drivers"]], ["complete"])

    def test_page_shows_each_documents_state(self):
        r = self._get()
        self.assertContains(r, "photo sent, details not entered")
        self.assertContains(r, "not on file")
        self.assertContains(r, "Missing a license")

    def test_empty_filter_result_says_so_in_plain_words(self):
        r = self._get(paperwork="expiring")
        self.assertEqual(len(r.context["drivers"]), 0)
        self.assertContains(r, "Nothing is expired or expiring in the next 30 days.")
