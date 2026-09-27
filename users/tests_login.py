"""Sign-in page: username or email, role-based landing, reset redirect.

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test users.tests_login
"""
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from drivers.models import Driver
from users.models import UserProfile


class LoginPageTests(TestCase):
    def test_renders_new_design_with_working_forgot_link(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'href="' + reverse("password_reset") + '"')
        self.assertContains(resp, "Username or email")
        self.assertContains(resp, "Reset password")
        self.assertNotContains(resp, "Use Google Instead")
        self.assertNotContains(resp, reverse("register"))

    def test_username_is_case_insensitive(self):
        User.objects.create_superuser(username="Abdi", email="a@x.com", password="pw-abdi-1")
        resp = self.client.post(reverse("login"), {"username": "abdi", "password": "pw-abdi-1"})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_email_works_as_identifier(self):
        u = User.objects.create_user(username="neuma", email="Neuma@example.com", password="pw-neuma-1")
        Driver.objects.create(profile=u)
        resp = self.client.post(reverse("login"), {"username": "neuma@example.com", "password": "pw-neuma-1"})
        self.assertRedirects(resp, reverse("schedule"), fetch_redirect_response=False)

    def test_wrong_password_message(self):
        User.objects.create_user(username="neuma", password="pw-neuma-1")
        resp = self.client.post(reverse("login"), {"username": "neuma", "password": "nope"}, follow=True)
        self.assertContains(resp, "Check both and try again")

    def test_inactive_account_message(self):
        User.objects.create_user(username="gone", password="pw-gone-1", is_active=False)
        resp = self.client.post(reverse("login"), {"username": "gone", "password": "pw-gone-1"}, follow=True)
        self.assertContains(resp, "switched off")

    def test_fleet_manager_lands_on_fleet_desk(self):
        u = User.objects.create_user(username="fleet", password="pw-fleet-1", is_staff=True)
        UserProfile.objects.create(user=u, phone_number="", is_fleet_manager=True)
        resp = self.client.post(reverse("login"), {"username": "fleet", "password": "pw-fleet-1"})
        self.assertRedirects(resp, reverse("fleet_desk"), fetch_redirect_response=False)

    def test_staff_without_driver_row_lands_on_dashboard(self):
        User.objects.create_user(username="desk", password="pw-desk-1", is_staff=True)
        resp = self.client.post(reverse("login"), {"username": "desk", "password": "pw-desk-1"})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_next_is_honoured_when_local(self):
        User.objects.create_superuser(username="abdi", email="a@x.com", password="pw-abdi-1")
        resp = self.client.post(reverse("login") + "?next=/drivers/", {"username": "abdi", "password": "pw-abdi-1"})
        self.assertRedirects(resp, "/drivers/", fetch_redirect_response=False)
        resp = self.client.post(reverse("login") + "?next=https://evil.example/x", {"username": "abdi", "password": "pw-abdi-1"})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_signed_in_visitor_is_sent_home(self):
        u = User.objects.create_user(username="neuma", password="pw-neuma-1")
        Driver.objects.create(profile=u)
        self.client.force_login(u)
        self.assertRedirects(self.client.get(reverse("login")), reverse("schedule"), fetch_redirect_response=False)

    def test_missing_fields_do_not_crash(self):
        resp = self.client.post(reverse("login"), {})
        self.assertEqual(resp.status_code, 200)

    def test_register_page_still_renders(self):
        self.assertContains(self.client.get(reverse("register")), "Create account")


class PasswordResetTests(TestCase):
    def test_reset_pages_render(self):
        for name in ("password_reset", "password_reset_done", "password_reset_complete"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertContains(resp, "auth-card")

    def test_reset_email_is_branded_and_confirm_lands_by_role(self):
        u = User.objects.create_user(username="neuma", email="neuma@example.com", password="pw-old-1")
        Driver.objects.create(profile=u)
        self.client.post(reverse("password_reset"), {"email": "neuma@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "Reset your Grayson Towncar password")
        self.assertIn("/users/password-reset-confirm/", msg.body)
        self.assertTrue(msg.alternatives)
        # Follow the link out of the email.
        import re
        path = re.search(r"(/users/password-reset-confirm/[^\s]+)", msg.body).group(1)
        resp = self.client.get(path, follow=True)
        set_url = resp.redirect_chain[-1][0]
        resp = self.client.post(set_url, {"new_password1": "orlando-mco-2026", "new_password2": "orlando-mco-2026"})
        self.assertRedirects(resp, reverse("after_login"), fetch_redirect_response=False)
        resp = self.client.get(reverse("after_login"))
        self.assertRedirects(resp, reverse("schedule"), fetch_redirect_response=False)

    def test_agent_login_page_renders(self):
        resp = self.client.get(reverse("agent_login"))
        self.assertContains(resp, "agent portal")
        self.assertContains(resp, reverse("password_reset"))
