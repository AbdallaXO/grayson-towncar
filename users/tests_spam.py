"""
Contact-form spam filter tests.

Every sample below is a real submission taken from the production contact-form
history, spam and genuine alike. The prime directive is the second class: a
customer asking for a ride must never be turned away. One piece of spam getting
through costs a dispatcher ten seconds; one blocked booking inquiry costs a
fare and a customer who thinks the website is broken.
"""

import time
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from users import turnstile
from users.forms import ContactUsFormSubmission
from users.models import ContactUsForm, PartnerForm
from users.spam import BLOCK_THRESHOLD, is_spam, score_submission
from users.views import CONTACT_ATTEMPTS_PER_HOUR


# ── Real spam, verbatim from the contact-form history ────────────────────────

SPAM_SAMPLES = [
    # The campaign that prompted this filter: shared "chakyGM" tag, +7 phone.
    dict(
        first_name="DanielchakyGM", last_name="KeithchakyGM",
        email="alexisolvera2018@gmail.com", phone_number="89367162866",
        about="Hi! Hope your day is going smoothly. Let's make your target achievable.",
    ),
    dict(
        first_name="MarkchakyGM", last_name="JacobchakyGM",
        email="alexbalog8@gmail.com", phone_number="83671438596",
        about="Hi! Hope your day is going smoothly. Let's enable your next step.",
    ),
    # Earlier waves of the same operation.
    dict(
        first_name="DanielexenePW", last_name="DanielexenePW",
        email="tlanemagee@yahoo.com", phone_number="88836549561",
        about="TURN A SMALL MOMENT INTO A MASSIVE WIN WITH THE $27,000,000 JACKPOT https://3sco.re/x",
    ),
    dict(
        first_name="SarahKnownPA", last_name="SarahKnownPA",
        email="nikitafofanov46@gmail.com", phone_number="89375537913",
        about="Hi! Hope your day is going smoothly.\r\n\r\nLet's make your target achievable "
              "with a plan built around what actually moves the needle for you today.",
    ),
    dict(
        first_name="Joriuckror", last_name="Joriuckror",
        email="f7e9s9z@sisii.fun", phone_number="86131751565",
        about="Вам перевод 100846 руб. забрать тут  https://tinyurl.com/k12N9jAa",
    ),
    # Cold-outreach marketing pitches — no ride content, always a link.
    dict(
        first_name="Hester", last_name="Mosher",
        email="mosher.hester29@yahoo.com", phone_number="483112072",
        about='Stop kidding yourself. To a busy business owner, your "SEO" or "Ads" '
              "pitch is noise. Click here https://example.com/offer to unsubscribe.",
    ),
    dict(
        first_name="Evelyn", last_name="Keldie",
        email="evelyn.keldie@msn.com", phone_number="3176149486",
        about="Only 3 EASY Clicks- Smartly Create & Sell High Quality Images, 4K HD Videos. "
              "https://example.com/ai — unsubscribe any time.",
    ),
]


# ── Real customers, verbatim. None of these may ever be blocked ──────────────

LEGIT_SAMPLES = [
    dict(
        first_name="Linda", last_name="LaForge",
        email="lindabug@cox.net", phone_number="8603023823",
        about="Two people from airport to Disney's Animal Kingdom Lodge - Kidani Village.",
    ),
    dict(
        first_name="Meghan", last_name="Olds",
        email="meghan7141@gmail.com", phone_number="18125986704",
        about="We are flying into MCO on 12/3/26 and are needing a ride to a hotel.",
    ),
    # 9-digit phone: a dropped digit, not a bot. Weak signal only.
    dict(
        first_name="Crista", last_name="Wood",
        email="fuddie1910@yahoo.com", phone_number="434645006",
        about="Hello- I'm looking for a quote on transportation from the airport.",
    ),
    # First and last name identical — real person, weak signal only.
    dict(
        first_name="Kaitlin", last_name="Kaitlin",
        email="kaitlin.kelch@gmail.com", phone_number="+1 309 660 1770",
        about="Hello, looking to book a roundtrip transportation October 3rd.",
    ),
    # Names sharing a 4-character tail. Must not read as a campaign tag.
    dict(
        first_name="Teagan", last_name="Flanagan",
        email="teaganflanagan@hotmail.com", phone_number="0435574002",
        about="Getting from MCO to Hilton Buena Vista Lake on the 14th.",
    ),
    # ALL-CAPS names must not trip the campaign-tag rule.
    dict(
        first_name="TATANISHA", last_name="Grady",
        email="tgrady40@gmail.com", phone_number="716-228-4405",
        about="6 passengers, 8 pieces of luggage, July 14 2026 transportation from DoubleTree.",
    ),
    dict(
        first_name="WADE", last_name="REWA",
        email="wade.rewa@example.com", phone_number="818-910-1005",
        about="I'd like to discuss my requirements.",
    ),
    # Credentials typed into the name field must not read as a campaign tag.
    dict(
        first_name="Robert", last_name="SmithMD",
        email="rsmith@example.com", phone_number="4072127190",
        about="Need a car from MCO to the Grand Floridian on the 12th.",
    ),
    dict(
        first_name="Angela", last_name="JonesPhD",
        email="ajones@example.com", phone_number="5613334444",
        about="Requesting a quote for airport pickup for two passengers.",
    ),
    # A travel agent talking about affiliates and commissions — business
    # vocabulary that overlaps with spam pitch language.
    dict(
        first_name="Stefanie", last_name="Byrne",
        email="karleekacztravel@gmail.com", phone_number="5044512317",
        about="Hi! I am actually a travel agent affiliate - but my client has asked "
              "about a commission on airport transfers to Disney.",
    ),
    # Long opener with no trip details yet.
    dict(
        first_name="Robert", last_name="Feus",
        email="Robert.Feus@cruiseplanners.com", phone_number="407-584-8058",
        about="Hello,\r\nI just want to check on something, as I couldn't find the "
              "information anywhere on the site and wanted to ask before I go any "
              "further with planning things out for the group I am handling.",
    ),
    # International customer.
    dict(
        first_name="Lindsay", last_name="Delph",
        email="lindsaydelph@gmail.com", phone_number="+447736038334",
        about="Hello, I'm travelling in August and wondered if you had availability.",
    ),
]


class SpamScoringTests(TestCase):
    def test_known_spam_is_blocked(self):
        for sample in SPAM_SAMPLES:
            with self.subTest(name=sample["first_name"]):
                score, reasons = score_submission(**sample)
                self.assertGreaterEqual(
                    score, BLOCK_THRESHOLD,
                    f"spam scored only {score} ({reasons})",
                )

    def test_real_customers_are_never_blocked(self):
        """The prime directive. A failure here is a lost booking."""
        for sample in LEGIT_SAMPLES:
            with self.subTest(name=sample["first_name"]):
                score, reasons = score_submission(**sample)
                self.assertLess(
                    score, BLOCK_THRESHOLD,
                    f"customer {sample['first_name']} {sample['last_name']} "
                    f"scored {score} ({reasons})",
                )

    def test_campaign_tag_survives_a_changed_phone_number(self):
        """The name pattern alone must kill it if the bot switches numbers."""
        self.assertTrue(is_spam(
            first_name="DanielchakyGM", last_name="KeithchakyGM",
            email="x@gmail.com", phone_number="407-212-7190",
            about="Hi! Hope your day is going smoothly.",
        ))

    def test_foreign_trunk_phone_survives_a_changed_name(self):
        """And the phone rule alone must kill it if the bot switches names."""
        self.assertTrue(is_spam(
            first_name="Daniel", last_name="Keith",
            email="x@gmail.com", phone_number="89367162866",
            about="Hi! Hope your day is going smoothly.",
        ))

    def test_reasons_explain_the_block(self):
        _, reasons = score_submission(
            first_name="DanielchakyGM", last_name="KeithchakyGM",
            email="x@gmail.com", phone_number="89367162866", about="hello",
        )
        self.assertIn("campaign_tag_name", reasons)
        self.assertIn("foreign_trunk_phone", reasons)

    def test_empty_submission_does_not_crash(self):
        score, _ = score_submission()
        self.assertEqual(score, 0)

    def test_none_fields_do_not_crash(self):
        score, _ = score_submission(
            first_name=None, last_name=None, email=None,
            phone_number=None, about=None,
        )
        self.assertEqual(score, 0)


class ContactFormSubmissionTests(TestCase):
    """The filter must actually stop the row from being written."""

    def _post_data(self, sample):
        data = dict(sample)
        data["contact_method"] = "email"
        # Loaded a minute ago, so the bot-speed check is not what's firing.
        data["form_loaded_at"] = str(time.time() - 60)
        return data

    def test_spam_submission_is_rejected(self):
        for sample in SPAM_SAMPLES:
            with self.subTest(name=sample["first_name"]):
                form = ContactUsFormSubmission(data=self._post_data(sample))
                self.assertFalse(form.is_valid())

    def test_customer_submission_saves(self):
        for sample in LEGIT_SAMPLES:
            with self.subTest(name=sample["first_name"]):
                form = ContactUsFormSubmission(data=self._post_data(sample))
                self.assertTrue(form.is_valid(), form.errors.as_text())

    def test_no_row_is_created_for_spam(self):
        before = ContactUsForm.objects.count()
        form = ContactUsFormSubmission(data=self._post_data(SPAM_SAMPLES[0]))
        self.assertFalse(form.is_valid())
        self.assertEqual(ContactUsForm.objects.count(), before)

    def test_honeypot_still_blocks(self):
        data = self._post_data(LEGIT_SAMPLES[0])
        data["website"] = "http://bot.example.com"
        form = ContactUsFormSubmission(data=data)
        self.assertFalse(form.is_valid())

    def test_instant_submission_still_blocks(self):
        data = self._post_data(LEGIT_SAMPLES[0])
        data["form_loaded_at"] = str(time.time())
        form = ContactUsFormSubmission(data=data)
        self.assertFalse(form.is_valid())


class ContactFormRateLimitTests(TestCase):
    """Volume throttle — the backstop for the next campaign, whatever it says."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _post(self, sample, ip="203.0.113.7"):
        data = dict(sample)
        data["contact_method"] = "email"
        data["form_loaded_at"] = str(time.time() - 60)
        return self.client.post(reverse("contact"), data, REMOTE_ADDR=ip)

    def test_rejected_attempts_still_burn_the_allowance(self):
        """A bot being content-blocked must not get unlimited retries."""
        for _ in range(CONTACT_ATTEMPTS_PER_HOUR):
            self._post(SPAM_SAMPLES[0])

        self.assertEqual(
            cache.get("contact_form_attempts_203.0.113.7"),
            CONTACT_ATTEMPTS_PER_HOUR,
        )
        # Next attempt is refused before the form is even built.
        response = self._post(LEGIT_SAMPLES[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    def test_one_customer_message_goes_through(self):
        response = self._post(LEGIT_SAMPLES[0])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactUsForm.objects.count(), 1)

    def test_limit_is_per_ip(self):
        for _ in range(CONTACT_ATTEMPTS_PER_HOUR + 2):
            self._post(SPAM_SAMPLES[0], ip="198.51.100.4")

        # A different visitor is unaffected by the flood next door.
        response = self._post(LEGIT_SAMPLES[0], ip="203.0.113.7")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactUsForm.objects.count(), 1)

    def test_proxy_header_identifies_the_client(self):
        """Railway forwards the real IP; without this every visitor shares one bucket."""
        for _ in range(CONTACT_ATTEMPTS_PER_HOUR + 1):
            self.client.post(
                reverse("contact"),
                dict(SPAM_SAMPLES[0], contact_method="email",
                     form_loaded_at=str(time.time() - 60)),
                HTTP_X_FORWARDED_FOR="192.0.2.55, 10.0.0.1",
            )
        self.assertIsNotNone(cache.get("contact_form_attempts_192.0.2.55"))


class TurnstileTests(TestCase):
    """
    The challenge layer. Unconfigured it must be completely invisible; once
    configured it must turn away anything without a valid token.

    Note the failure policy these tests pin: FAIL CLOSED, per Cloudflare's
    canonical integration. If siteverify is unreachable, submissions are
    rejected — including genuine ones. See users/turnstile.py.
    """

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _post(self, sample, token=None, ip="203.0.113.9"):
        data = dict(sample)
        data["contact_method"] = "email"
        data["form_loaded_at"] = str(time.time() - 60)
        if token is not None:
            data[turnstile.TOKEN_FIELD] = token
        return self.client.post(reverse("contact"), data, REMOTE_ADDR=ip)

    # ── Unconfigured: nothing changes ──

    @override_settings(TURNSTILE_SITE_KEY="", TURNSTILE_SECRET="")
    def test_no_keys_means_no_verification(self):
        self.assertFalse(turnstile.is_configured())
        passed, reason = turnstile.verify("")
        self.assertTrue(passed)
        self.assertEqual(reason, "not_configured")

    @override_settings(TURNSTILE_SITE_KEY="", TURNSTILE_SECRET="")
    def test_customer_unaffected_when_unconfigured(self):
        response = self._post(LEGIT_SAMPLES[0])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactUsForm.objects.count(), 1)

    @override_settings(TURNSTILE_SITE_KEY="", TURNSTILE_SECRET="")
    def test_widget_absent_when_unconfigured(self):
        response = self.client.get(reverse("contact"))
        self.assertNotContains(response, "cf-turnstile")

    # ── Configured ──

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_widget_rendered_when_configured(self):
        response = self.client.get(reverse("contact"))
        self.assertContains(response, "cf-turnstile")
        self.assertContains(response, "challenges.cloudflare.com")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_submission_without_a_token_is_refused(self):
        """A script that never loaded the page has no token to send."""
        response = self._post(LEGIT_SAMPLES[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_forged_token_is_refused(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {
            "success": False, "error-codes": ["invalid-input-response"],
        }
        response = self._post(LEGIT_SAMPLES[0], token="forged")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_valid_token_lets_the_customer_through(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"success": True}
        response = self._post(LEGIT_SAMPLES[0], token="valid")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactUsForm.objects.count(), 1)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post", side_effect=requests.Timeout("boom"))
    def test_unreachable_siteverify_fails_closed(self, mock_post):
        """
        Canonical Cloudflare behaviour. The cost is real and worth restating:
        during a siteverify outage this turns away genuine customers too. The
        error message routes them to the phone number.
        """
        response = self._post(LEGIT_SAMPLES[0], token="whatever")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_non_2xx_from_siteverify_fails_closed(self, mock_post):
        mock_post.return_value.ok = False
        mock_post.return_value.status_code = 500
        response = self._post(LEGIT_SAMPLES[0], token="whatever")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_non_json_body_fails_closed(self, mock_post):
        mock_post.return_value.ok = True
        mock_post.return_value.json.side_effect = ValueError("not json")
        response = self._post(LEGIT_SAMPLES[0], token="whatever")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_success_must_be_exactly_true(self, mock_post):
        """A truthy-but-not-True value must not be accepted as a pass."""
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"success": "yes"}
        response = self._post(LEGIT_SAMPLES[0], token="whatever")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactUsForm.objects.count(), 0)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_canonical_siteverify_payload(self, mock_post):
        """secret / response / remoteip, form-encoded, to the canonical URL."""
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"success": True}
        self._post(LEGIT_SAMPLES[0], token="tok-123", ip="198.51.100.30")

        self.assertEqual(mock_post.call_args.args[0], turnstile.VERIFY_URL)
        sent = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent["secret"], "secret")
        self.assertEqual(sent["response"], "tok-123")
        self.assertEqual(sent["remoteip"], "198.51.100.30")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_widget_carries_the_spin_analytics_action(self):
        response = self.client.get(reverse("contact"))
        self.assertContains(response, 'data-action="turnstile-spin-v2"')

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_widget_is_reset_after_submit(self):
        """Tokens are single-use; an inline error must not strand the customer."""
        response = self.client.get(reverse("contact"))
        self.assertContains(response, "turnstile?.reset()")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="")
    def test_secret_alone_gates_the_widget(self):
        """Site key ships as a default; the secret is what turns this on."""
        self.assertFalse(turnstile.is_configured())
        self.assertEqual(turnstile.site_key(), "")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_client_ip_is_passed_to_cloudflare(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = {"success": True}
        self._post(LEGIT_SAMPLES[0], token="valid", ip="198.51.100.22")
        self.assertEqual(
            mock_post.call_args.kwargs["data"]["remoteip"], "198.51.100.22"
        )


class PartnerFormTurnstileTests(TestCase):
    """
    Second insertion point. Same contract as the contact form: the challenge
    gates the existing handler, it does not replace it.
    """

    APPLICATION = {
        "name": "Dana Whitfield",
        "email": "dana@whitfieldtravel.com",
        "phone_number": "4075551234",
        "agency_name": "Whitfield Travel",
        "preferred_contact": "email",
        "referral_source": "google",
    }

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="")
    def test_widget_absent_when_unconfigured(self):
        response = self.client.get(reverse("partner"))
        self.assertNotContains(response, "cf-turnstile")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_widget_rendered_with_analytics_action(self):
        response = self.client.get(reverse("partner"))
        self.assertContains(response, 'data-action="turnstile-spin-v2"')
        self.assertContains(response, "challenges.cloudflare.com")

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    def test_application_without_a_token_is_refused(self):
        before = PartnerForm.objects.count()
        response = self.client.post(reverse("partner"), self.APPLICATION)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PartnerForm.objects.count(), before)

    @override_settings(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET="secret")
    @patch("users.turnstile.requests.post")
    def test_failed_challenge_is_visible_to_the_applicant(self, mock_post):
        """This page suppresses the messages block, so it must surface inline."""
        mock_post.return_value.ok = True
        mock_post.return_value.json.return_value = {"success": False}
        response = self.client.post(
            reverse("partner"),
            dict(self.APPLICATION, **{turnstile.TOKEN_FIELD: "forged"}),
        )
        self.assertContains(response, "couldn&#x27;t verify")

    @override_settings(TURNSTILE_SITE_KEY="", TURNSTILE_SECRET="")
    def test_applicant_unaffected_when_unconfigured(self):
        before = PartnerForm.objects.count()
        response = self.client.post(reverse("partner"), self.APPLICATION)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PartnerForm.objects.count(), before + 1)


class ContactFormTaskTests(TestCase):
    """Spam already sitting in the database must not page a dispatcher."""

    def test_spam_rows_create_no_ops_task(self):
        from ops.models import OperationalTask
        from ops.tasks import _scan_uncontacted_forms

        spam = ContactUsForm.objects.create(status="pending", **SPAM_SAMPLES[0])
        real = ContactUsForm.objects.create(status="pending", **LEGIT_SAMPLES[0])

        _scan_uncontacted_forms()

        self.assertFalse(
            OperationalTask.objects.filter(contact_form=spam).exists(),
            "dispatcher was paged about a spam submission",
        )
        self.assertTrue(
            OperationalTask.objects.filter(contact_form=real).exists(),
            "real customer did not generate a follow-up task",
        )
