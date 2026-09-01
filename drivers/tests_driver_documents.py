"""
Driver self-service licensing documents + the AnalyzeID license scan and the
Textract-Queries permit scan.

Invariants under test:
  * the photo is saved even when OCR is unavailable or misreads — that image
    is the point of collecting it, and a failed scan must never lose it
  * a scan NEVER auto-commits: extracted fields come back as a confirm form,
    and only an explicit confirm POST writes to the driver
  * a driver reaches only their OWN row, and can only write scan files plus
    their own license and permit details
  * no test ever calls AWS

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test drivers.tests_driver_documents
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from drivers.license_ocr import (
    LicenseScanResult, is_date_of_birth_plausible, is_expiration_plausible,
    scan_license, sniff_content_type,
)
from drivers.models import Driver
from drivers.permit_ocr import PermitScanResult, scan_permit

SCAN_PATH = "drivers.views.scan_license"
PERMIT_SCAN_PATH = "drivers.views.scan_permit"


def _photo(name="license.jpg", content_type="image/jpeg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0 fake jpeg bytes", content_type=content_type)


def _ok_scan(**overrides):
    fields = {
        "license_number": "D123-456-78-901-0",
        "license_state": "FL",
        "license_expiration": date(2030, 4, 17),
    }
    fields.update(overrides)
    return LicenseScanResult(ok=True, fields=fields, confidence={k: 99.0 for k in fields})


def _ok_permit_scan(**overrides):
    fields = {
        "chauffeur_permit_number": "12345",
        "chauffeur_permit_fdl_number": "V123-456-78-900-0",
        "chauffeur_permit_expiration": date(2027, 11, 13),
    }
    fields.update(overrides)
    return PermitScanResult(ok=True, fields=fields, confidence={k: 99.0 for k in fields})


@override_settings(GOOGLE_MAPS_API_KEY="")
class MyDocumentsPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("doc_driver", first_name="Doc", last_name="Driver")
        self.driver = Driver.objects.create(profile=self.user, driver_type="inhouse")
        self.client.force_login(self.user)
        self.url = reverse("driver_my_documents")

    def test_page_lists_all_three_documents(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Driver's License", html)
        self.assertIn("Chauffeur Permit", html)
        self.assertIn("DOT Medical Card", html)

    def test_no_view_photo_link_when_nothing_is_on_file(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("View photo on file", html)

    def test_view_photo_link_appears_once_a_scan_is_on_file(self):
        self.driver.license_scan.save("license.jpg", _photo(), save=True)
        self.driver.chauffeur_permit_scan.save("permit.jpg", _photo(), save=True)
        self.driver.dot_medical_card_scan.save("dot.jpg", _photo(), save=True)
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count("View photo on file"), 3)
        self.assertIn(self.driver.license_scan.url, html)
        self.assertIn(self.driver.chauffeur_permit_scan.url, html)
        self.assertIn(self.driver.dot_medical_card_scan.url, html)

    def test_inputs_offer_both_camera_and_existing_files(self):
        """capture= would FORCE the camera and hide the photo library and file
        browser. Leaving it off is what lets the phone offer all three, which
        is the whole point of "take or upload"."""
        html = self.client.get(self.url).content.decode()
        self.assertEqual(html.count('type="file"'), 3)
        self.assertNotIn("capture=", html)
        self.assertEqual(html.count("Take or upload photo"), 3)

    def test_a_driver_without_a_driver_row_gets_404(self):
        stranger = User.objects.create_user("not_a_driver", password="x")
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_anonymous_is_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_each_file_input_has_a_distinct_accessible_name(self):
        """All three inputs shared the visible label 'Take or upload photo' —
        a screen-reader user couldn't tell them apart without a distinct
        aria-label on each."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("aria-label=\"Take or upload a photo of your driver's license\"", html)
        self.assertIn('aria-label="Take or upload a photo of your chauffeur permit"', html)
        self.assertIn('aria-label="Take or upload a photo of your DOT medical card"', html)

    def test_decorative_camera_icons_are_hidden_from_assistive_tech(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('class="bi bi-camera-fill me-1" aria-hidden="true"', html)
        self.assertEqual(html.count('class="bi bi-camera me-1" aria-hidden="true"'), 2)

    def test_concurrent_submit_guard_disables_every_scan_button(self):
        """Regression guard for the original bug: scoping the disable to
        form.querySelectorAll left the other two document cards tappable
        during an in-flight upload."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("document.querySelectorAll('label.scan-btn')", html)
        self.assertNotIn("form.querySelectorAll('label.scan-btn')", html)

    def test_freeze_never_rewrites_the_submitting_forms_own_label(self):
        """Regression guard for a real bug the fix above introduced: the file
        <input> lives INSIDE its label, so replacing every label's innerHTML
        (including the one being submitted) deletes the very input whose
        change event fired, and the upload silently arrives with no file —
        exactly the "Please choose or take a photo first" report. The
        submitting label must only be dimmed, never have innerHTML touched,
        so Django's Test Client (which can't execute this JS) at least can't
        regress the source back to the naive form."""
        html = self.client.get(self.url).content.decode()
        self.assertIn("currentLabel", html)
        self.assertIn("if (btn === currentLabel)", html)

    def test_bfcache_restore_resets_the_page(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("pageshow", html)
        self.assertIn("resetScanButtons", html)


@override_settings(GOOGLE_MAPS_API_KEY="")
class LicenseScanFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("scan_driver", first_name="Scan")
        self.driver = Driver.objects.create(profile=self.user, driver_type="inhouse")
        self.client.force_login(self.user)
        self.url = reverse("driver_my_documents")

    def test_successful_scan_saves_photo_and_offers_a_confirm_form(self):
        with mock.patch(SCAN_PATH, return_value=_ok_scan()) as scanner:
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        self.assertTrue(scanner.called)
        self.assertEqual(resp.status_code, 200)

        self.driver.refresh_from_db()
        self.assertTrue(self.driver.license_scan)
        # Content, not just a filename: the view stores the file and THEN reads
        # the same handle for OCR. If those two ever fight over the stream
        # position, this is what catches it — a 0-byte scan in the office's
        # hands is worse than no scan at all.
        self.driver.license_scan.open("rb")
        try:
            self.assertEqual(self.driver.license_scan.read(), _photo().read())
        finally:
            self.driver.license_scan.close()

        html = resp.content.decode()
        self.assertIn("Check your license details", html)
        self.assertIn("D123-456-78-901-0", html)
        self.assertIn("2030-04-17", html)

    def test_confirm_step_labels_are_wired_to_their_inputs(self):
        """<label for=...> not a bare caption <div> — otherwise a screen
        reader announces four unlabeled edit boxes on the one screen where a
        driver types a license number that becomes a compliance record."""
        with mock.patch(SCAN_PATH, return_value=_ok_scan()):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        html = resp.content.decode()
        for field in ("license_number", "license_state", "license_class", "license_expiration"):
            self.assertIn(f'for="id_{field}"', html)

    def test_name_dob_address_are_pre_filled_and_badged_on_the_confirm_step(self):
        scan = _ok_scan(
            license_full_name="Jane Carter",
            license_date_of_birth=date(1990, 4, 17),
            license_address="123 Main St, Orlando, FL 32801",
        )
        with mock.patch(SCAN_PATH, return_value=scan):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        html = resp.content.decode()
        self.assertIn('value="Jane Carter"', html)
        self.assertIn('value="1990-04-17"', html)
        self.assertIn('value="123 Main St, Orlando, FL 32801"', html)
        self.assertEqual(html.count("read from photo"), 5)  # number, expiration, name, dob, address

    def test_confirming_saves_name_dob_and_address(self):
        resp = self.client.post(self.url, {
            "action": "confirm_license",
            "license_number": "D123-456-78-901-0",
            "license_state": "FL",
            "license_class": "E",
            "license_expiration": "2030-04-17",
            "license_full_name": "Jane Carter",
            "license_date_of_birth": "1990-04-17",
            "license_address": "123 Main St, Orlando, FL 32801",
        })
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.license_full_name, "Jane Carter")
        self.assertEqual(self.driver.license_date_of_birth, date(1990, 4, 17))
        self.assertEqual(self.driver.license_address, "123 Main St, Orlando, FL 32801")

    def test_implausible_date_of_birth_is_flagged(self):
        scan = _ok_scan(license_date_of_birth=date(1910, 1, 1))
        with mock.patch(SCAN_PATH, return_value=scan):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        html = resp.content.decode()
        self.assertIn("date of birth", html.lower())
        self.assertIn("double-check", html.lower())

    def test_help_text_ids_referenced_by_aria_describedby_actually_exist(self):
        """Django auto-adds aria-describedby="id_x_helptext" for any field
        with model help_text — a dangling reference if that id is never
        rendered anywhere on the page."""
        with mock.patch(SCAN_PATH, return_value=_ok_scan()):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        html = resp.content.decode()
        self.assertIn('aria-describedby="id_license_state_helptext"', html)
        self.assertIn('id="id_license_state_helptext"', html)
        self.assertIn('aria-describedby="id_license_class_helptext"', html)
        self.assertIn('id="id_license_class_helptext"', html)

    def test_class_field_shows_the_scanned_badge_like_the_others(self):
        with mock.patch(SCAN_PATH, return_value=_ok_scan(license_class="E")):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        html = resp.content.decode()
        self.assertEqual(html.count("read from photo"), 2)
        self.assertEqual(html.count(">read<"), 2)

    def test_a_scan_alone_never_writes_the_license_details(self):
        """The whole safety property: OCR pre-fills a form, it does not commit.
        A misread digit on a license number is a compliance problem."""
        with mock.patch(SCAN_PATH, return_value=_ok_scan()):
            self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.license_number, "")
        self.assertIsNone(self.driver.license_expiration)

    def test_confirming_writes_the_details(self):
        resp = self.client.post(self.url, {
            "action": "confirm_license",
            "license_number": "D123-456-78-901-0",
            "license_state": "FL",
            "license_class": "E",
            "license_expiration": "2030-04-17",
        })
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.license_number, "D123-456-78-901-0")
        self.assertEqual(self.driver.license_expiration, date(2030, 4, 17))

    def test_driver_can_correct_what_the_scan_read(self):
        with mock.patch(SCAN_PATH, return_value=_ok_scan()):
            self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        self.client.post(self.url, {
            "action": "confirm_license",
            "license_number": "CORRECTED-999",
            "license_state": "GA",
            "license_class": "C",
            "license_expiration": "2031-01-02",
        })
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.license_number, "CORRECTED-999")
        self.assertEqual(self.driver.license_state, "GA")

    def test_failed_scan_still_keeps_the_photo_and_falls_back_to_typing(self):
        """OCR being down must not cost the driver their upload — staff can
        still read the image by hand."""
        failure = LicenseScanResult(ok=False, error="Couldn't read the license automatically.")
        with mock.patch(SCAN_PATH, return_value=failure):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        self.assertEqual(resp.status_code, 200)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.license_scan)
        self.assertIn("Check your license details", resp.content.decode())

    def test_implausible_expiration_is_flagged_but_still_offered(self):
        scan = _ok_scan(license_expiration=date(1974, 4, 17))
        with mock.patch(SCAN_PATH, return_value=scan):
            resp = self.client.post(self.url, {"action": "upload_license", "scan": _photo()})
        self.assertIn("double-check", resp.content.decode().lower())

    def test_an_uploaded_pdf_is_kept_and_falls_back_to_typing(self):
        """Allowing "upload" means real-world files arrive: a DMV PDF, an
        iPhone HEIC. Textract can't read those, but they are still the
        document the office needs, so the file must survive."""
        pdf = SimpleUploadedFile("license.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        resp = self.client.post(self.url, {"action": "upload_license", "scan": pdf})
        self.assertEqual(resp.status_code, 200)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.license_scan)
        self.assertIn("Check your license details", resp.content.decode())

    def test_an_uploaded_heic_is_kept_and_falls_back_to_typing(self):
        heic = SimpleUploadedFile(
            "license.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", content_type="image/heic",
        )
        self.client.post(self.url, {"action": "upload_license", "scan": heic})
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.license_scan)

    def test_posting_without_a_photo_is_rejected(self):
        resp = self.client.post(self.url, {"action": "upload_license"})
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.license_scan)

    def test_a_spoofed_content_type_is_rejected_before_saving_or_scanning(self):
        evil = SimpleUploadedFile(
            "shot.jpg", b"<html><script>alert(1)</script></html>", content_type="image/jpeg",
        )
        with mock.patch(SCAN_PATH) as scanner:
            resp = self.client.post(self.url, {"action": "upload_license", "scan": evil})
        self.assertFalse(scanner.called)
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.license_scan)

    def test_invalid_confirm_does_not_write(self):
        self.client.post(self.url, {
            "action": "confirm_license",
            "license_number": "D1",
            "license_expiration": "not-a-date",
        })
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.license_expiration)


@override_settings(GOOGLE_MAPS_API_KEY="")
class PermitAndDotCardUploadTests(TestCase):
    """The permit scans (drivers.permit_ocr) and confirms like the license;
    the DOT card stays photo-only — no reliable extractor exists for it, so
    nothing is parsed and the office fills in the details."""

    def setUp(self):
        self.user = User.objects.create_user("permit_driver", first_name="Permit")
        self.driver = Driver.objects.create(profile=self.user, driver_type="inhouse")
        self.client.force_login(self.user)
        self.url = reverse("driver_my_documents")

    def test_permit_photo_saves_and_uses_the_permit_scanner_not_the_license_one(self):
        with mock.patch(SCAN_PATH) as license_scanner, \
                mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()) as permit_scanner:
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo("permit.jpg")})
        self.assertFalse(license_scanner.called)
        self.assertTrue(permit_scanner.called)
        self.assertEqual(resp.status_code, 200)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.chauffeur_permit_scan)

    def test_dot_card_photo_saves_without_calling_any_ocr(self):
        with mock.patch(SCAN_PATH) as license_scanner, \
                mock.patch(PERMIT_SCAN_PATH) as permit_scanner:
            self.client.post(self.url, {"action": "upload_dot_card", "scan": _photo("dot.jpg")})
        self.assertFalse(license_scanner.called)
        self.assertFalse(permit_scanner.called)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.dot_medical_card_scan)

    def test_uploads_never_touch_expirations(self):
        """An upload (even one whose scan succeeds) pre-fills a form at most;
        only an explicit confirm writes a date."""
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        self.client.post(self.url, {"action": "upload_dot_card", "scan": _photo()})
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.chauffeur_permit_expiration)
        self.assertIsNone(self.driver.dot_medical_card_expiration)

    def test_unknown_action_changes_nothing(self):
        resp = self.client.post(self.url, {"action": "upload_everything", "scan": _photo()})
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.license_scan)
        self.assertFalse(self.driver.chauffeur_permit_scan)

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=["+14075551234"])
    def test_permit_upload_notifies_staff_even_when_the_scan_succeeds(self):
        """The backstop for a driver who uploads and then abandons the
        confirm step — the photo is already saved, and nothing else ever
        flags "uploaded, not yet transcribed".

        The notify call runs on a background thread (reservations.utils.
        _run_in_background) so the request doesn't block on a Twilio
        round-trip; patched here to run inline so the assertion below isn't
        racing that thread."""
        with mock.patch(
            "reservations.utils._run_in_background",
            side_effect=lambda fn, *a, **k: fn(*a, **k),
        ), mock.patch("drivers.views.notify_staff_of_document_upload") as notify, \
                mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo("permit.jpg")})
        self.assertTrue(notify.called)
        self.assertEqual(notify.call_args.args[1], "chauffeur_permit_scan")

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=["+14075551234"])
    def test_permit_upload_notifies_staff_when_the_scan_fails_too(self):
        failure = PermitScanResult(ok=False, error="Couldn't read the permit automatically.")
        with mock.patch(
            "reservations.utils._run_in_background",
            side_effect=lambda fn, *a, **k: fn(*a, **k),
        ), mock.patch("drivers.views.notify_staff_of_document_upload") as notify, \
                mock.patch(PERMIT_SCAN_PATH, return_value=failure):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo("permit.jpg")})
        self.assertTrue(notify.called)
        self.assertEqual(notify.call_args.args[1], "chauffeur_permit_scan")

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=["+14075551234"])
    def test_dot_card_upload_notifies_staff(self):
        with mock.patch(
            "reservations.utils._run_in_background",
            side_effect=lambda fn, *a, **k: fn(*a, **k),
        ), mock.patch("drivers.views.notify_staff_of_document_upload") as notify:
            self.client.post(self.url, {"action": "upload_dot_card", "scan": _photo("dot.jpg")})
        self.assertTrue(notify.called)
        self.assertEqual(notify.call_args.args[1], "dot_medical_card_scan")

    def test_a_spoofed_content_type_is_rejected_before_saving_or_scanning(self):
        """The actual attack this closes: declare image/jpeg, ship HTML. The
        media bucket is a public custom domain that re-serves whatever
        Content-Type gets stored, so this must never reach storage — and
        never reach Textract either."""
        evil = SimpleUploadedFile(
            "shot.jpg", b"<html><script>alert(1)</script></html>", content_type="image/jpeg",
        )
        with mock.patch(PERMIT_SCAN_PATH) as scanner:
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": evil})
        self.assertFalse(scanner.called)
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertFalse(self.driver.chauffeur_permit_scan)

    def test_uploaded_filename_is_not_the_original_client_supplied_name(self):
        """AWS_S3_FILE_OVERWRITE defaults True project-wide — a predictable
        name means one driver's upload can clobber another's."""
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo("permit.jpg")})
        self.driver.refresh_from_db()
        self.assertNotIn("permit.jpg", self.driver.chauffeur_permit_scan.name)


@override_settings(GOOGLE_MAPS_API_KEY="")
class PermitScanFlowTests(TestCase):
    """The permit's scan → pre-filled confirm → save flow, mirroring the
    license's. Same safety property throughout: a scan alone never writes."""

    def setUp(self):
        self.user = User.objects.create_user("permit_scan_driver", first_name="Permit")
        self.driver = Driver.objects.create(profile=self.user, driver_type="inhouse")
        self.client.force_login(self.user)
        self.url = reverse("driver_my_documents")

    def test_successful_scan_saves_photo_and_offers_a_confirm_form(self):
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()) as scanner:
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo("permit.jpg")})
        self.assertTrue(scanner.called)
        self.assertEqual(resp.status_code, 200)

        self.driver.refresh_from_db()
        self.assertTrue(self.driver.chauffeur_permit_scan)

        html = resp.content.decode()
        self.assertIn("Check your permit details", html)
        self.assertIn("12345", html)
        self.assertIn("V123-456-78-900-0", html)
        self.assertIn("2027-11-13", html)
        self.assertEqual(html.count("read from photo"), 3)

    def test_confirm_step_labels_are_wired_to_their_inputs(self):
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        html = resp.content.decode()
        for field in ("chauffeur_permit_number", "chauffeur_permit_fdl_number",
                      "chauffeur_permit_expiration"):
            self.assertIn(f'for="id_{field}"', html)

    def test_help_text_ids_referenced_by_aria_describedby_actually_exist(self):
        """Django auto-adds aria-describedby="id_x_helptext" for any field
        with help_text — a dangling reference if that id is never rendered."""
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        html = resp.content.decode()
        self.assertIn('aria-describedby="id_chauffeur_permit_number_helptext"', html)
        self.assertIn('id="id_chauffeur_permit_number_helptext"', html)
        self.assertIn('aria-describedby="id_chauffeur_permit_fdl_number_helptext"', html)
        self.assertIn('id="id_chauffeur_permit_fdl_number_helptext"', html)

    def test_a_scan_alone_never_writes_the_permit_details(self):
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.chauffeur_permit_number, "")
        self.assertEqual(self.driver.chauffeur_permit_fdl_number, "")
        self.assertIsNone(self.driver.chauffeur_permit_expiration)

    def test_confirming_writes_the_details(self):
        resp = self.client.post(self.url, {
            "action": "confirm_permit",
            "chauffeur_permit_number": "12345",
            "chauffeur_permit_fdl_number": "V123-456-78-900-0",
            "chauffeur_permit_expiration": "2027-11-13",
        })
        self.assertRedirects(resp, self.url)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.chauffeur_permit_number, "12345")
        self.assertEqual(self.driver.chauffeur_permit_fdl_number, "V123-456-78-900-0")
        self.assertEqual(self.driver.chauffeur_permit_expiration, date(2027, 11, 13))

    def test_driver_can_correct_what_the_scan_read(self):
        with mock.patch(PERMIT_SCAN_PATH, return_value=_ok_permit_scan()):
            self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        self.client.post(self.url, {
            "action": "confirm_permit",
            "chauffeur_permit_number": "99999",
            "chauffeur_permit_fdl_number": "V999-999-99-999-9",
            "chauffeur_permit_expiration": "2028-01-02",
        })
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.chauffeur_permit_number, "99999")
        self.assertEqual(self.driver.chauffeur_permit_expiration, date(2028, 1, 2))

    def test_failed_scan_still_keeps_the_photo_and_falls_back_to_typing(self):
        failure = PermitScanResult(ok=False, error="Couldn't read the permit automatically.")
        with mock.patch(PERMIT_SCAN_PATH, return_value=failure):
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        self.assertEqual(resp.status_code, 200)
        self.driver.refresh_from_db()
        self.assertTrue(self.driver.chauffeur_permit_scan)
        self.assertIn("Check your permit details", resp.content.decode())

    def test_implausible_expiration_is_flagged_but_still_offered(self):
        scan = _ok_permit_scan(chauffeur_permit_expiration=date(2010, 1, 1))
        with mock.patch(PERMIT_SCAN_PATH, return_value=scan):
            resp = self.client.post(self.url, {"action": "upload_permit", "scan": _photo()})
        self.assertIn("double-check", resp.content.decode().lower())

    def test_fdl_mismatch_on_confirm_warns_but_still_saves(self):
        """The cross-check the FDL# exists for: it should equal the license
        number on file. A mismatch is surfaced to the driver — and the office
        via the existing profile pill — but never blocks the save; staff sort
        out which of the two was mistyped."""
        self.driver.license_number = "V123-456-78-900-0"
        self.driver.save(update_fields=["license_number"])
        resp = self.client.post(self.url, {
            "action": "confirm_permit",
            "chauffeur_permit_number": "12345",
            "chauffeur_permit_fdl_number": "V888-888-88-888-8",
            "chauffeur_permit_expiration": "2027-11-13",
        }, follow=True)
        self.driver.refresh_from_db()
        self.assertEqual(self.driver.chauffeur_permit_fdl_number, "V888-888-88-888-8")
        html = resp.content.decode()
        # "doesn't" renders HTML-escaped, so assert on an apostrophe-free part.
        self.assertIn("match the license number we have on file", html)

    def test_invalid_confirm_does_not_write(self):
        self.client.post(self.url, {
            "action": "confirm_permit",
            "chauffeur_permit_number": "12345",
            "chauffeur_permit_expiration": "not-a-date",
        })
        self.driver.refresh_from_db()
        self.assertIsNone(self.driver.chauffeur_permit_expiration)


class PermitOcrUnitTests(TestCase):
    """scan_permit itself — the guards that run before any AWS call, and the
    parsing of the QUERY/QUERY_RESULT blocks. boto3 is mocked throughout."""

    @staticmethod
    def _response(answers):
        """{alias: [(text, confidence), ...]} -> an AnalyzeDocument response.
        An alias mapped to an empty list renders a QUERY with no ANSWER."""
        blocks = []
        for i, (alias, results) in enumerate(answers.items()):
            answer_ids = [f"a{i}_{j}" for j in range(len(results))]
            query = {"BlockType": "QUERY", "Id": f"q{i}",
                     "Query": {"Text": "?", "Alias": alias}}
            if answer_ids:
                query["Relationships"] = [{"Type": "ANSWER", "Ids": answer_ids}]
            blocks.append(query)
            for answer_id, (text, confidence) in zip(answer_ids, results):
                blocks.append({"BlockType": "QUERY_RESULT", "Id": answer_id,
                               "Text": text, "Confidence": confidence})
        return {"Blocks": blocks}

    def test_non_image_upload_is_refused_before_calling_aws(self):
        pdf = SimpleUploadedFile("permit.pdf", b"%PDF-1.4", content_type="application/pdf")
        with mock.patch("drivers.permit_ocr._client") as client:
            result = scan_permit(pdf)
        self.assertFalse(client.called)
        self.assertFalse(result.ok)
        self.assertIn("JPG or PNG", result.error)

    def test_oversized_upload_is_refused_before_calling_aws(self):
        big = _photo()
        big.size = 11 * 1024 * 1024
        with mock.patch("drivers.permit_ocr._client") as client:
            result = scan_permit(big)
        self.assertFalse(client.called)
        self.assertFalse(result.ok)

    def test_aws_failure_is_swallowed_into_a_readable_error(self):
        with mock.patch("drivers.permit_ocr._client", side_effect=RuntimeError("no creds")):
            result = scan_permit(_photo())
        self.assertFalse(result.ok)
        self.assertIn("type the details in", result.error)

    def test_fields_are_mapped_and_the_date_is_parsed(self):
        response = self._response({
            "PERMIT_NUMBER": [("12345", 99.1)],
            "FDL_NUMBER": [("V123-456-78-900-0", 98.4)],
            "EXPIRATION_DATE": [("11/13/2027", 97.2)],
        })
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(_photo())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["chauffeur_permit_number"], "12345")
        self.assertEqual(result.fields["chauffeur_permit_fdl_number"], "V123-456-78-900-0")
        self.assertEqual(result.fields["chauffeur_permit_expiration"], date(2027, 11, 13))

    def test_low_confidence_answers_are_dropped(self):
        response = self._response({"PERMIT_NUMBER": [("MAYBE-8", 41.0)]})
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(_photo())
        self.assertFalse(result.ok)

    def test_a_query_with_no_answer_is_skipped(self):
        response = self._response({
            "PERMIT_NUMBER": [("12345", 99.0)],
            "EXPIRATION_DATE": [],
        })
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(_photo())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields, {"chauffeur_permit_number": "12345"})

    def test_the_most_confident_of_several_answers_wins(self):
        response = self._response({
            "PERMIT_NUMBER": [("12345", 88.0), ("I2345", 96.0)],
        })
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(_photo())
        self.assertEqual(result.fields["chauffeur_permit_number"], "I2345")

    def test_an_unparseable_expiration_is_dropped_not_guessed(self):
        response = self._response({"EXPIRATION_DATE": [("SEE CITY CLERK", 95.0)]})
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(_photo())
        self.assertFalse(result.ok)
        self.assertNotIn("chauffeur_permit_expiration", result.fields)

    def test_a_blank_page_reports_cleanly(self):
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = {"Blocks": []}
            result = scan_permit(_photo())
        self.assertFalse(result.ok)
        self.assertIn("make out the details", result.error)

    def test_mislabeled_content_type_is_still_read(self):
        """Same rule as the license: Android file pickers hand out
        application/octet-stream for perfectly good JPEGs — sniff the bytes,
        never trust the declared type."""
        jpeg = SimpleUploadedFile(
            "photo", b"\xff\xd8\xff\xe0 fake jpeg bytes", content_type="application/octet-stream",
        )
        response = self._response({"PERMIT_NUMBER": [("12345", 99.0)]})
        with mock.patch("drivers.permit_ocr._client") as client:
            client.return_value.analyze_document.return_value = response
            result = scan_permit(jpeg)
        self.assertTrue(client.return_value.analyze_document.called)
        self.assertTrue(result.ok)


@override_settings(GOOGLE_MAPS_API_KEY="")
class DriverIsolationTests(TestCase):
    def test_a_driver_can_only_ever_write_their_own_row(self):
        mine = User.objects.create_user("iso_mine", first_name="Mine")
        my_driver = Driver.objects.create(profile=mine, driver_type="inhouse")
        theirs = User.objects.create_user("iso_theirs", first_name="Theirs")
        their_driver = Driver.objects.create(profile=theirs, driver_type="inhouse")

        self.client.force_login(mine)
        self.client.post(reverse("driver_my_documents"), {
            "action": "confirm_license",
            "license_number": "MINE-1",
            "license_state": "FL",
            "license_class": "E",
            "license_expiration": "2030-01-01",
        })
        my_driver.refresh_from_db()
        their_driver.refresh_from_db()
        self.assertEqual(my_driver.license_number, "MINE-1")
        self.assertEqual(their_driver.license_number, "")


class LicenseOcrUnitTests(TestCase):
    """scan_license itself — the guards that run before any AWS call, and the
    parsing of what comes back. boto3 is mocked throughout."""

    def test_non_image_upload_is_refused_before_calling_aws(self):
        pdf = SimpleUploadedFile("license.pdf", b"%PDF-1.4", content_type="application/pdf")
        with mock.patch("drivers.license_ocr._client") as client:
            result = scan_license(pdf)
        self.assertFalse(client.called)
        self.assertFalse(result.ok)
        self.assertIn("JPG or PNG", result.error)

    def test_oversized_upload_is_refused_before_calling_aws(self):
        big = _photo()
        big.size = 11 * 1024 * 1024
        with mock.patch("drivers.license_ocr._client") as client:
            result = scan_license(big)
        self.assertFalse(client.called)
        self.assertFalse(result.ok)

    def test_aws_failure_is_swallowed_into_a_readable_error(self):
        with mock.patch("drivers.license_ocr._client", side_effect=RuntimeError("no creds")):
            result = scan_license(_photo())
        self.assertFalse(result.ok)
        self.assertIn("type the details in", result.error)

    def test_fields_are_mapped_and_dates_normalized(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "DOCUMENT_NUMBER"},
             "ValueDetection": {"Text": "D123-456-78-901-0", "Confidence": 99.1}},
            {"Type": {"Text": "EXPIRATION_DATE"},
             "ValueDetection": {"Text": "04/17/2030", "Confidence": 98.0,
                                "NormalizedValue": {"Value": "2030-04-17T00:00:00"}}},
            {"Type": {"Text": "STATE_IN_ADDRESS"},
             "ValueDetection": {"Text": "FL", "Confidence": 97.5}},
            {"Type": {"Text": "FIRST_NAME"},
             "ValueDetection": {"Text": "Jane", "Confidence": 99.9}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertTrue(result.ok)
        self.assertEqual(result.fields["license_number"], "D123-456-78-901-0")
        self.assertEqual(result.fields["license_expiration"], date(2030, 4, 17))
        self.assertEqual(result.fields["license_state"], "FL")
        # Names are returned by AnalyzeID but deliberately not stored.
        self.assertNotIn("first_name", result.fields)

    def test_expiration_falls_back_to_printed_format(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "EXPIRATION_DATE"},
             "ValueDetection": {"Text": "04/17/2030", "Confidence": 98.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_expiration"], date(2030, 4, 17))

    def test_low_confidence_fields_are_dropped(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "DOCUMENT_NUMBER"},
             "ValueDetection": {"Text": "MAYBE-8", "Confidence": 41.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertFalse(result.ok)

    def test_a_non_license_photo_reports_cleanly(self):
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = {"IdentityDocuments": []}
            result = scan_license(_photo())
        self.assertFalse(result.ok)
        self.assertIn("didn't look like a driver's license", result.error)

    def test_mislabeled_content_type_is_still_read(self):
        """Android file pickers routinely hand out application/octet-stream
        for a perfectly good JPEG — the declared type must never be trusted
        over the file's own magic bytes."""
        jpeg = SimpleUploadedFile(
            "photo", b"\xff\xd8\xff\xe0 fake jpeg bytes", content_type="application/octet-stream",
        )
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = {"IdentityDocuments": [{
                "IdentityDocumentFields": [
                    {"Type": {"Text": "DOCUMENT_NUMBER"},
                     "ValueDetection": {"Text": "D1", "Confidence": 99.0}},
                ]
            }]}
            result = scan_license(jpeg)
        self.assertTrue(client.return_value.analyze_id.called)
        self.assertTrue(result.ok)

    def test_state_name_is_preferred_over_state_in_address(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "STATE_NAME"},
             "ValueDetection": {"Text": "FLORIDA", "Confidence": 99.0}},
            {"Type": {"Text": "STATE_IN_ADDRESS"},
             "ValueDetection": {"Text": "GA", "Confidence": 97.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        # The issuing state (STATE_NAME), not the address state, and
        # normalized from the full printed name to the 2-letter code.
        self.assertEqual(result.fields["license_state"], "FL")

    def test_state_in_address_is_only_a_fallback(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "STATE_IN_ADDRESS"},
             "ValueDetection": {"Text": "GA", "Confidence": 97.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_state"], "GA")

    def test_class_field_is_extracted(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "CLASS"},
             "ValueDetection": {"Text": "E", "Confidence": 98.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_class"], "E")

    def test_name_is_assembled_from_first_middle_last_and_shouty_case_is_fixed(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "FIRST_NAME"}, "ValueDetection": {"Text": "JANE", "Confidence": 99.0}},
            {"Type": {"Text": "MIDDLE_NAME"}, "ValueDetection": {"Text": "A", "Confidence": 95.0}},
            {"Type": {"Text": "LAST_NAME"}, "ValueDetection": {"Text": "O'BRIEN", "Confidence": 98.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_full_name"], "Jane A O'Brien")

    def test_name_with_missing_parts_uses_whatever_is_present(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "FIRST_NAME"}, "ValueDetection": {"Text": "JANE", "Confidence": 99.0}},
            {"Type": {"Text": "LAST_NAME"}, "ValueDetection": {"Text": "CARTER", "Confidence": 98.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_full_name"], "Jane Carter")

    def test_already_mixed_case_name_is_left_alone(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "FIRST_NAME"}, "ValueDetection": {"Text": "Jane", "Confidence": 99.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_full_name"], "Jane")

    def test_date_of_birth_is_extracted(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "DATE_OF_BIRTH"},
             "ValueDetection": {"Text": "04/17/1990", "Confidence": 98.0,
                                "NormalizedValue": {"Value": "1990-04-17T00:00:00"}}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_date_of_birth"], date(1990, 4, 17))

    def test_address_is_assembled_from_street_city_state_zip(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "ADDRESS"}, "ValueDetection": {"Text": "123 Main St", "Confidence": 97.0}},
            {"Type": {"Text": "CITY_IN_ADDRESS"}, "ValueDetection": {"Text": "Orlando", "Confidence": 96.0}},
            {"Type": {"Text": "STATE_IN_ADDRESS"}, "ValueDetection": {"Text": "FL", "Confidence": 97.0}},
            {"Type": {"Text": "ZIP_CODE_IN_ADDRESS"}, "ValueDetection": {"Text": "32801", "Confidence": 95.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_address"], "123 Main St, Orlando, FL 32801")

    def test_address_with_only_city_and_state_still_assembles(self):
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "CITY_IN_ADDRESS"}, "ValueDetection": {"Text": "Orlando", "Confidence": 96.0}},
            {"Type": {"Text": "STATE_IN_ADDRESS"}, "ValueDetection": {"Text": "FL", "Confidence": 97.0}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertEqual(result.fields["license_address"], "Orlando, FL")

    def test_a_passport_is_rejected_not_pre_filled_as_a_license(self):
        """AnalyzeID reads US/CA passports too. A driver photographing the
        wrong document must not get a passport number pre-filled into
        license_number with a confident green 'read from photo' badge."""
        response = {"IdentityDocuments": [{"IdentityDocumentFields": [
            {"Type": {"Text": "ID_TYPE"},
             "ValueDetection": {"Text": "PASSPORT", "Confidence": 99.0}},
            {"Type": {"Text": "DOCUMENT_NUMBER"},
             "ValueDetection": {"Text": "X98765432", "Confidence": 98.7}},
        ]}]}
        with mock.patch("drivers.license_ocr._client") as client:
            client.return_value.analyze_id.return_value = response
            result = scan_license(_photo())
        self.assertFalse(result.ok)
        self.assertIn("passport", result.error.lower())
        self.assertEqual(result.fields, {})

    def test_expiration_plausibility(self):
        today = timezone.localdate()
        self.assertTrue(is_expiration_plausible(today + timedelta(days=365)))
        self.assertTrue(is_expiration_plausible(today - timedelta(days=30)))
        self.assertFalse(is_expiration_plausible(today - timedelta(days=365 * 30)))
        self.assertFalse(is_expiration_plausible(today + timedelta(days=365 * 30)))
        self.assertFalse(is_expiration_plausible(None))

    def test_date_of_birth_plausibility(self):
        today = timezone.localdate()
        self.assertTrue(is_date_of_birth_plausible(today - timedelta(days=365 * 35)))
        self.assertFalse(is_date_of_birth_plausible(today - timedelta(days=365 * 5)))
        self.assertFalse(is_date_of_birth_plausible(today - timedelta(days=365 * 110)))
        self.assertFalse(is_date_of_birth_plausible(today + timedelta(days=30)))
        self.assertFalse(is_date_of_birth_plausible(None))


class SniffContentTypeTests(TestCase):
    """Magic-byte identification — the thing every upload is actually gated
    on, never the client-declared Content-Type."""

    def test_jpeg(self):
        self.assertEqual(sniff_content_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")

    def test_png(self):
        self.assertEqual(sniff_content_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")

    def test_pdf(self):
        self.assertEqual(sniff_content_type(b"%PDF-1.4 rest"), "application/pdf")

    def test_heic(self):
        payload = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
        self.assertEqual(sniff_content_type(payload), "image/heic")

    def test_unrecognized_bytes(self):
        self.assertIsNone(sniff_content_type(b"<html><body>not a document</body></html>"))

    def test_empty(self):
        self.assertIsNone(sniff_content_type(b""))
