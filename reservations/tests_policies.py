"""
The public Reservation Policies page.

Every number on this page is something a guest can be charged against, so the
tests pin the figures to the dispatcher field guide. If a number here changes,
the field guide, the confirmation email, and this page must all change together.
"""
from django.test import TestCase
from django.urls import reverse


class PoliciesPageTests(TestCase):
    def setUp(self):
        self.response = self.client.get(reverse("policies"))
        self.html = self.response.content.decode()

    def test_page_renders(self):
        self.assertEqual(self.response.status_code, 200)
        self.assertEqual(reverse("policies"), "/policies/")
        self.assertIn("Our policies, in plain English", self.html)

    def test_airport_arrival_numbers(self):
        self.assertIn("60-minute grace period", self.html)
        self.assertIn("30 to 40 minutes", self.html)
        self.assertIn("$100 for the next hour", self.html)

    def test_departure_grace_numbers(self):
        self.assertIn("10-minute grace period", self.html)
        self.assertIn("in the vehicle at 4:00 PM", self.html)
        self.assertIn("15 more minutes for $30", self.html)
        self.assertIn("At 25 minutes the car has to go", self.html)

    def test_cancellation_tiers_match_refund_engine(self):
        # The refund engine is 48h+ full, 24-48h half, <24h nothing.
        self.assertIn("48 or more hours before pickup", self.html)
        self.assertIn("50% refund", self.html)
        self.assertIn("Less than 24 hours before pickup", self.html)
        self.assertIn("No refund", self.html)

    def test_publix_stop_rules(self):
        self.assertIn("9930 Universal Blvd", self.html)
        self.assertIn("Add it ahead of time", self.html)
        self.assertIn("Past 20 minutes there is a $30 fee", self.html)
        self.assertIn("$1 for every extra minute", self.html)
        self.assertIn("publix.com/locations/1191-lake-cay-commons", self.html)

    def test_extra_stops_are_a_policy_not_a_price_list(self):
        # Stops are an additional charge, quoted by dispatch. The S1/S2/S3 tiers
        # are pricing and stay off this page.
        self.assertIn("is an additional charge", self.html)
        self.assertIn("can't add a stop himself", self.html)
        self.assertNotIn("$40", self.html)
        self.assertNotIn("$60", self.html)

    def test_other_fees(self):
        self.assertIn("$20 after-hours fee", self.html)
        self.assertIn("$250", self.html)
        self.assertIn("$200 cleaning fee", self.html)

    def test_never_promises_curbside_airport_pickup(self):
        # Airport pickups are always a baggage-claim meet, never curbside.
        arrivals = self.html.split('id="arrivals"', 1)[1].split("</section>", 1)[0]
        self.assertIn("baggage claim", arrivals)
        self.assertNotIn("curb", arrivals.lower())

    def test_uses_the_normal_site_navbar_and_footer(self):
        self.assertNotIn("ed-page", self.html)
        self.assertIn("footer-bottom-link", self.html)

    def test_site_navbar_and_footer_link_to_policies(self):
        home = self.client.get("/").content.decode()
        # Once in the About dropdown, once in the footer.
        self.assertGreaterEqual(home.count('href="/policies/"'), 2)
