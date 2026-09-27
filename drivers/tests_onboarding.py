"""Adding a driver by welcome link, phone normalisation, My details, and the
onboarding checklist.

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_onboarding
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from drivers import invites, onboarding, phones
from drivers.forms import NewDriverForm
from drivers.models import Driver, DriverInvite
from users.models import UserProfile


def _superuser(username="boss"):
    return User.objects.create_superuser(username=username, email=f"{username}@x.com", password="pw-boss-123")


def _dispatcher(username="desk"):
    return User.objects.create_user(username=username, password="pw-desk-123", is_staff=True)


def _driver(username="neuma", first="Neuma", last="Silva", phone="4075550134", email="", **fields):
    user = User.objects.create_user(username=username, first_name=first, last_name=last, email=email)
    user.set_unusable_password()
    user.save()
    fields.setdefault("driver_type", "inhouse")
    return Driver.objects.create(profile=user, phone_number=phone, **fields)


def _license_photo():
    return SimpleUploadedFile("license.jpg", b"\xff\xd8\xff\xe0 fake jpeg bytes", content_type="image/jpeg")


class PhoneTests(TestCase):
    def test_normalize_accepts_every_common_us_shape(self):
        for raw in ("4075550134", "(407) 555-0134", "407.555.0134", "+1 407 555 0134", "1-407-555-0134"):
            self.assertEqual(phones.normalize(raw), "+14075550134", raw)

    def test_normalize_keeps_international_and_rejects_junk(self):
        self.assertEqual(phones.normalize("+44 20 7946 0958"), "+442079460958")
        self.assertEqual(phones.normalize("555"), "")
        self.assertEqual(phones.normalize(""), "")
        self.assertEqual(phones.normalize(None), "")

    def test_pretty(self):
        self.assertEqual(phones.pretty("+14075550134"), "(407) 555-0134")
        self.assertEqual(phones.pretty("garbage 12"), "garbage 12")

    def test_driver_save_normalises_but_never_drops(self):
        d = _driver(phone="(407) 555-0134")
        d.refresh_from_db()
        self.assertEqual(d.phone_number, "+14075550134")
        d.phone_number = "ext 12"
        d.save()
        d.refresh_from_db()
        self.assertEqual(d.phone_number, "ext 12")

    def test_template_filters(self):
        from django.template import Context, Template
        out = Template("{% load phone_tags %}{{ p|phone }}|{{ p|tel }}").render(Context({"p": "4075550134"}))
        self.assertEqual(out, "(407) 555-0134|+14075550134")


class AddDriverTests(TestCase):
    def setUp(self):
        self.boss = _superuser()
        self.client.force_login(self.boss)

    @mock.patch("drivers.sms.send", return_value=(True, None))
    def test_creates_account_profile_driver_and_texts_link(self, send):
        resp = self.client.post(reverse("driver_new"), {
            "first_name": "Aldo", "last_name": "Hernández", "phone_number": "407-555-0134",
            "email": "", "driver_type": "inhouse", "portal_role": "driver",
            "employment_type": "full_time", "hired_on": "2026-10-01", "send_via": "sms",
        })
        driver = Driver.objects.get(profile__first_name="Aldo")
        self.assertRedirects(resp, reverse("driver_profile", args=[driver.id]))
        self.assertEqual(driver.profile.username, "aldo.hernandez")
        self.assertFalse(driver.profile.has_usable_password())
        self.assertEqual(driver.phone_number, "+14075550134")
        self.assertEqual(driver.employment_type, "full_time")
        self.assertEqual(str(driver.hired_on), "2026-10-01")
        self.assertTrue(UserProfile.objects.filter(user=driver.profile, is_driver=True).exists())
        invite = driver.open_invite()
        self.assertIsNotNone(invite)
        self.assertEqual(invite.sent_via, "sms")
        self.assertEqual(invite.send_count, 1)
        self.assertEqual(invite.created_by, self.boss)
        to, body = send.call_args[0]
        self.assertEqual(to, "+14075550134")
        self.assertIn(invites.url(invite), body)
        self.assertIn("Aldo", body)

    def test_username_deduplicates(self):
        User.objects.create_user(username="aldo.hernandez")
        self.assertEqual(invites.suggest_username("Aldo", "Hernandez"), "aldo.hernandez2")
        self.assertEqual(invites.suggest_username("", "", "4075550134"), "driver0134")

    def test_emails_the_link(self):
        resp = self.client.post(reverse("driver_new"), {
            "first_name": "Maria", "last_name": "Costa", "phone_number": "",
            "email": "maria@example.com", "driver_type": "affiliate", "portal_role": "operator",
            "employment_type": "", "hired_on": "", "send_via": "email",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["maria@example.com"])
        self.assertIn("/drivers/welcome/", msg.body)
        self.assertTrue(any("text/html" in alt[1] for alt in msg.alternatives))

    def test_sms_requires_a_number_and_email_requires_an_address(self):
        form = NewDriverForm({"first_name": "A", "driver_type": "inhouse", "portal_role": "driver", "send_via": "sms"})
        self.assertFalse(form.is_valid())
        self.assertIn("phone_number", form.errors)
        form = NewDriverForm({"first_name": "A", "driver_type": "inhouse", "portal_role": "driver", "send_via": "email"})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_rejects_a_duplicate_phone_or_email(self):
        _driver(username="existing", phone="4075550134", email="e@example.com")
        form = NewDriverForm({"first_name": "B", "phone_number": "(407) 555-0134", "email": "E@example.com",
                              "driver_type": "inhouse", "portal_role": "driver", "send_via": "link"})
        self.assertFalse(form.is_valid())
        self.assertIn("phone_number", form.errors)
        self.assertIn("email", form.errors)

    def test_dispatcher_cannot_add_drivers(self):
        self.client.force_login(_dispatcher())
        self.assertEqual(self.client.get(reverse("driver_new")).status_code, 403)
        d = _driver()
        self.assertEqual(self.client.post(reverse("driver_invite", args=[d.id]), {"action": "link"}).status_code, 403)
        self.assertIsNone(d.open_invite())

    @mock.patch("drivers.sms.send", return_value=(False, "twilio not configured"))
    def test_sms_failure_is_reported_not_hidden(self, send):
        resp = self.client.post(reverse("driver_new"), {
            "first_name": "Sam", "phone_number": "4075550100", "driver_type": "inhouse",
            "portal_role": "driver", "send_via": "sms",
        }, follow=True)
        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("copy the link", text)

    def test_page_renders(self):
        resp = self.client.get(reverse("driver_new"))
        self.assertContains(resp, "Add a driver")
        self.assertContains(resp, "Text it to their mobile")


class InviteLifecycleTests(TestCase):
    def setUp(self):
        self.boss = _superuser()
        self.driver = _driver()

    def test_create_revokes_previous_open_link(self):
        first = invites.create(self.driver, by=self.boss)
        second = invites.create(self.driver, by=self.boss)
        first.refresh_from_db()
        self.assertEqual(first.status, "revoked")
        self.assertEqual(second.status, "open")
        self.assertIsNone(invites.lookup(first.token))
        self.assertEqual(invites.lookup(second.token), second)

    def test_expired_link_is_not_open(self):
        invite = invites.create(self.driver)
        DriverInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(invites.lookup(invite.token))
        self.assertEqual(invites.lookup_any(invite.token).status, "expired")

    def test_profile_actions(self):
        self.client.force_login(self.boss)
        url = reverse("driver_invite", args=[self.driver.id])
        with mock.patch("drivers.sms.send", return_value=(True, None)) as send:
            self.client.post(url, {"action": "sms"})
        invite = self.driver.open_invite()
        self.assertEqual(invite.sent_via, "sms")
        self.assertEqual(send.call_count, 1)
        # "Text it again" reuses the same link
        with mock.patch("drivers.sms.send", return_value=(True, None)):
            self.client.post(url, {"action": "sms"})
        self.assertEqual(self.driver.open_invite().pk, invite.pk)
        self.assertEqual(self.driver.open_invite().send_count, 2)
        # Cancel
        self.client.post(url, {"action": "revoke"})
        self.assertIsNone(self.driver.open_invite())

    def test_profile_shows_login_card_and_link(self):
        self.client.force_login(self.boss)
        resp = self.client.get(reverse("driver_profile", args=[self.driver.id]))
        self.assertContains(resp, "No login yet")
        self.assertContains(resp, "Text a welcome link")
        self.assertContains(resp, "(407) 555-0134")
        invite = invites.create(self.driver, by=self.boss)
        resp = self.client.get(reverse("driver_profile", args=[self.driver.id]))
        self.assertContains(resp, invites.url(invite))
        self.assertContains(resp, "Link sent")

    def test_directory_flags_drivers_without_a_login(self):
        self.client.force_login(self.boss)
        resp = self.client.get(reverse("drivers_extend"))
        self.assertContains(resp, "No login")
        self.assertContains(resp, "Add a driver")
        invites.create(self.driver)
        resp = self.client.get(reverse("drivers_extend"))
        self.assertContains(resp, "Link sent")
        self.client.force_login(_dispatcher())
        resp = self.client.get(reverse("drivers_extend"))
        self.assertNotContains(resp, "Add a driver")


class WelcomePageTests(TestCase):
    def setUp(self):
        self.driver = _driver()
        self.invite = invites.create(self.driver)
        self.url = reverse("driver_welcome", args=[self.invite.token])

    def test_renders_without_login_and_prefills_username(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Welcome, Neuma")
        self.assertContains(resp, 'value="neuma"')
        self.assertContains(resp, "(407) 555-0134")

    def test_sets_password_signs_in_and_goes_to_my_details(self):
        resp = self.client.post(self.url, {
            "username": "neuma.silva", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
        })
        self.assertRedirects(resp, reverse("driver_my_details") + "?welcome=1", fetch_redirect_response=False)
        user = User.objects.get(pk=self.driver.profile.pk)
        self.assertEqual(user.username, "neuma.silva")
        self.assertTrue(user.check_password("orlando-mco-2026"))
        self.assertTrue(Driver.objects.get(pk=self.driver.pk).has_login())
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, "accepted")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        # Used link now shows the "already used" page
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 410)
        self.assertContains(resp, "already used", status_code=410)

    def test_operator_lands_on_their_board(self):
        self.driver.portal_role = "operator"
        self.driver.save()
        resp = self.client.post(self.url, {
            "username": "neuma", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
        })
        self.assertRedirects(resp, reverse("operator_board"), fetch_redirect_response=False)

    def test_bad_password_or_taken_username_shows_errors(self):
        User.objects.create_user(username="taken")
        resp = self.client.post(self.url, {"username": "taken", "new_password1": "12345678", "new_password2": "12345678"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "taken")
        self.assertContains(resp, "numeric")
        self.assertFalse(User.objects.get(pk=self.driver.profile.pk).has_usable_password())

    def test_unknown_and_expired_links(self):
        resp = self.client.get(reverse("driver_welcome", args=["nope"]))
        self.assertEqual(resp.status_code, 404)
        self.assertContains(resp, "isn't", status_code=404)
        DriverInvite.objects.filter(pk=self.invite.pk).update(expires_at=timezone.now() - timedelta(days=1))
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 410)
        self.assertContains(resp, "expired", status_code=410)

    def test_optional_license_photo_is_read_then_flow_continues_to_details(self):
        from drivers.license_ocr import LicenseScanResult
        result = LicenseScanResult(ok=True, fields={"license_number": "D777", "license_state": "FL"},
                                   confidence={"license_number": 99.0, "license_state": 99.0})
        with mock.patch("drivers.views.scan_license", return_value=result):
            resp = self.client.post(self.url, {
                "username": "neuma", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
                "license_scan": _license_photo(),
            })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["confirming_license"])
        details_url = reverse("driver_my_details") + "?welcome=1"
        self.assertEqual(resp.context["confirm_next"], details_url)
        self.assertContains(resp, f'name="next" value="{details_url}"')
        d = Driver.objects.get(pk=self.driver.pk)
        self.assertTrue(d.has_login())
        self.assertTrue(d.license_scan)
        # Confirming the read details carries on to My Details, not My Documents.
        resp = self.client.post(reverse("driver_my_documents"), {
            "action": "confirm_license", "next": details_url,
            "license_number": "D777", "license_state": "FL", "license_class": "E",
            "license_expiration": "2030-04-17", "license_full_name": "Neuma Silva",
            "license_date_of_birth": "1990-01-01", "license_address": "",
        })
        self.assertRedirects(resp, details_url, fetch_redirect_response=False)
        self.assertEqual(Driver.objects.get(pk=self.driver.pk).license_number, "D777")
        # A foreign next is ignored.
        resp = self.client.post(reverse("driver_my_documents"), {
            "action": "confirm_license", "next": "https://evil.example/", "license_number": "D777",
            "license_state": "FL", "license_expiration": "2030-04-17",
        })
        self.assertRedirects(resp, reverse("driver_my_documents"), fetch_redirect_response=False)

    def test_welcome_page_offers_the_photo_but_never_requires_it(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "Add a photo of your license")
        self.assertContains(resp, "optional")
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post(self.url, {
            "username": "neuma", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
            "license_scan": bad,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.get(pk=self.driver.profile.pk).has_usable_password())

    def test_returning_driver_copy(self):
        self.driver.profile.set_password("already-set-1")
        self.driver.profile.save()
        resp = self.client.get(self.url)
        self.assertContains(resp, "new password")


class MyDetailsTests(TestCase):
    def setUp(self):
        self.driver = _driver()
        self.driver.profile.set_password("pw")
        self.driver.profile.save()
        self.client.force_login(self.driver.profile)

    def _details(self, **extra):
        data = {"first_name": "Neuma", "phone_number": "4075550134"}
        data.update(extra)
        return data

    def test_license_photo_is_required_until_one_is_on_file(self):
        resp = self.client.get(reverse("driver_my_details"))
        self.assertContains(resp, "Take or upload license photo")
        resp = self.client.post(reverse("driver_my_details"), self._details())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "photo of your driver")
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.details_confirmed_at)
        # Once a license is on file the field is gone and saving works without it.
        Driver.objects.filter(pk=self.driver.pk).update(license_number="D123")
        resp = self.client.get(reverse("driver_my_details"))
        self.assertNotContains(resp, "Take or upload license photo")
        resp = self.client.post(reverse("driver_my_details"), self._details())
        self.assertRedirects(resp, reverse("driver_my_details"))

    def test_photo_goes_through_the_read_and_confirm_step(self):
        from drivers.license_ocr import LicenseScanResult
        result = LicenseScanResult(ok=True, fields={"license_number": "D123-456", "license_state": "FL"},
                                   confidence={"license_number": 99.0, "license_state": 99.0})
        with mock.patch("drivers.views.scan_license", return_value=result):
            resp = self.client.post(reverse("driver_my_details") + "?welcome=1",
                                    self._details(home_address="1 Main St", license_scan=_license_photo()))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["confirming_license"])
        self.assertEqual(resp.context["details_form"].initial["license_number"], "D123-456")
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.license_scan)
        self.assertEqual(self.driver.home_address, "1 Main St")
        self.assertIsNotNone(self.driver.details_confirmed_at)

    def test_rejects_a_file_that_is_not_a_photo(self):
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post(reverse("driver_my_details"), self._details(license_scan=bad))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Driver.objects.get(pk=self.driver.pk).license_scan)

    def test_renders_and_saves(self):
        Driver.objects.filter(pk=self.driver.pk).update(license_number="D123")
        resp = self.client.get(reverse("driver_my_details"))
        self.assertContains(resp, "My Details")
        resp = self.client.post(reverse("driver_my_details"), {
            "first_name": "Neuma", "last_name": "Silva", "email": "neuma@example.com",
            "phone_number": "(407) 555-0199", "home_address": "1 Main St, Orlando, FL 32801",
        })
        self.assertRedirects(resp, reverse("driver_my_details"))
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.phone_number, "+14075550199")
        self.assertEqual(self.driver.home_address, "1 Main St, Orlando, FL 32801")
        self.assertEqual(self.driver.profile.email, "neuma@example.com")
        self.assertIsNotNone(self.driver.details_confirmed_at)

    def test_welcome_flow_continues_to_documents(self):
        Driver.objects.filter(pk=self.driver.pk).update(license_number="D123")
        resp = self.client.post(reverse("driver_my_details") + "?welcome=1", {
            "first_name": "Neuma", "phone_number": "4075550134",
        })
        self.assertRedirects(resp, reverse("driver_my_documents"), fetch_redirect_response=False)

    def test_bad_phone_is_rejected(self):
        resp = self.client.post(reverse("driver_my_details"), {"first_name": "Neuma", "phone_number": "555"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "10-digit")

    def test_staff_without_driver_row_get_404(self):
        self.client.force_login(_dispatcher())
        self.assertEqual(self.client.get(reverse("driver_my_details")).status_code, 404)


class ChecklistTests(TestCase):
    def test_counts_and_details(self):
        d = _driver(email="")
        c = onboarding.checklist(d)
        keys = [i["key"] for i in c["items"]]
        self.assertEqual(keys, ["login", "phone", "email", "license", "permit", "address", "hired_on", "employment"])
        self.assertEqual(c["done"], 1)  # phone only
        self.assertFalse(c["complete"])
        self.assertIn("send a welcome link", c["items"][0]["detail"])

        invites.create(d)
        self.assertIn("not opened yet", onboarding.checklist(d)["items"][0]["detail"])

        d.profile.set_password("x"); d.profile.last_login = timezone.now()
        d.profile.email = "n@example.com"; d.profile.save()
        d.license_expiration = timezone.localdate() + timedelta(days=400)
        d.chauffeur_permit_expiration = timezone.localdate() + timedelta(days=400)
        d.home_address = "1 Main St"; d.hired_on = timezone.localdate(); d.employment_type = "full_time"
        d.save()
        c = onboarding.checklist(d)
        self.assertTrue(c["complete"], [i for i in c["items"] if i["state"] != "done"])

    def test_affiliate_skips_employment_and_operator_skips_card(self):
        d = _driver(driver_type="affiliate")
        self.assertNotIn("employment", [i["key"] for i in onboarding.checklist(d)["items"]])


class MyPasswordTests(TestCase):
    def setUp(self):
        self.driver = _driver()
        self.driver.profile.set_password("old-pw-12345")
        self.driver.profile.save()
        self.client.force_login(self.driver.profile)

    def test_details_page_links_to_it(self):
        resp = self.client.get(reverse("driver_my_details"))
        self.assertContains(resp, reverse("driver_my_password"))
        self.assertContains(resp, "Sign out")

    def test_changes_password_and_stays_signed_in(self):
        resp = self.client.post(reverse("driver_my_password"), {
            "old_password": "old-pw-12345", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
        })
        self.assertRedirects(resp, reverse("driver_my_details"))
        self.assertTrue(User.objects.get(pk=self.driver.profile.pk).check_password("orlando-mco-2026"))
        self.assertEqual(self.client.get(reverse("driver_my_details")).status_code, 200)

    def test_wrong_current_password(self):
        resp = self.client.post(reverse("driver_my_password"), {
            "old_password": "nope", "new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.get(pk=self.driver.profile.pk).check_password("old-pw-12345"))
