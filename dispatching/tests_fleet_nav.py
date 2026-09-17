"""What the fleet role's top bar offers, and what it must never offer.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_fleet_nav

The fleet manager is the one member of staff with no dispatch board, no
reservations and no part in anyone's pay. Two places used to forget that:

  * the dark bar's wordmark read "Dispatching" and linked to the dispatch
    dashboard — the only control on his bar that left the fleet pages, sitting
    where a logo goes;
  * the public site's staff menu tested `is_staff` and nothing else, so landing
    on the marketing site (a 404, the "Main Website" link in his own bar) handed
    him Commissions, Driver Pay and Pay Rates.

Neither is a permission — every fleet page stays open to any staff member by
URL, and a dispatcher marking a car down at 9 PM must never need a role flag.
These are about what the bar OFFERS, which is what tells a new person what their
job is.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from users.models import UserProfile


class _NavFixture(TestCase):
    def fleet_manager(self):
        user = User.objects.create_user("nav_fm", password="x", is_staff=True)
        UserProfile.objects.create(user=user, phone_number="407-555-0100",
                                   is_fleet_manager=True)
        return user

    def dispatcher(self):
        user = User.objects.create_user("nav_disp", password="x", is_staff=True)
        UserProfile.objects.create(user=user, phone_number="407-555-0101",
                                   is_fleet_manager=False)
        return user

    def body(self, user, url):
        self.client.force_login(user)
        return self.client.get(url).content.decode()


class TheFleetBar(_NavFixture):
    def test_the_wordmark_names_his_own_room(self):
        body = self.body(self.fleet_manager(), reverse("fleet_desk"))
        self.assertIn(">Fleet</span>", body)
        self.assertNotIn(">Dispatching</span>", body)

    def test_a_dispatcher_still_gets_the_dispatching_wordmark(self):
        body = self.body(self.dispatcher(), reverse("fleet_desk"))
        self.assertIn(">Dispatching</span>", body)

    def test_the_six_pages_are_not_offered_twice_on_one_screen(self):
        """He carries them in the dark bar; the pill row underneath is the same
        six links again, and two identical tab rows stacked is not navigation."""
        body = self.body(self.fleet_manager(), reverse("fleet_day"))
        self.assertNotIn('class="fl-pill', body)

    def test_a_dispatcher_keeps_the_pills(self):
        """Their top bar has one Fleet entry and no way to reach the other five
        pages, so the pills are the only fleet navigation they have."""
        body = self.body(self.dispatcher(), reverse("fleet_day"))
        self.assertIn('class="fl-pill', body)


class TheStaffMenuOnThePublicSite(_NavFixture):
    """The marketing site's own navbar, which he reaches by any stray link."""

    def test_he_is_not_shown_other_people_s_commissions(self):
        body = self.body(self.fleet_manager(), reverse("home"))
        self.assertNotIn(reverse("admin_commission_report"), body)
        self.assertNotIn(reverse("driver_payment_management"), body)
        self.assertNotIn(reverse("driver_pay_rates"), body)

    def test_he_gets_his_own_pages_instead(self):
        body = self.body(self.fleet_manager(), reverse("home"))
        self.assertIn(reverse("fleet_desk"), body)
        self.assertIn(reverse("fleet_inspections"), body)

    def test_a_dispatcher_s_menu_is_untouched(self):
        body = self.body(self.dispatcher(), reverse("home"))
        self.assertIn(reverse("admin_commission_report"), body)
