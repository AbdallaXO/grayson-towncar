"""
drivers.document_uploads.prepare_document_upload — the security gate every
driver document upload (license, permit, DOT card) passes through before
anything is written to the model/S3.

Invariants under test:
  * only a real, magic-byte-sniffed JPEG/PNG/PDF/HEIC is accepted — the
    client-declared Content-Type is never trusted
  * an oversized file is rejected WITHOUT reading it into memory first
  * on success, the returned file's content_type and name are the sniffed
    type and a random filename, so the stored object's Content-Type can
    never be attacker-controlled and two same-named uploads can't collide
  * a real photo comes back recompressed to WebP (drivers.image_compression);
    a PDF, or a photo Pillow can't decode, is stored as-is

drivers.document_notifications.notify_staff_of_document_upload:
  * texts every configured number, with a label naming which document
  * silently no-ops when no numbers are configured
"""

import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from drivers.document_notifications import notify_staff_of_document_upload
from drivers.document_uploads import MAX_UPLOAD_BYTES, prepare_document_upload
from drivers.models import Driver

#: Magic-byte-only — sniffs as a real JPEG but isn't a decodable image, so
#: compression silently no-ops and the original bytes are kept. Exercises
#: that fallback path without needing image_compression's own test coverage
#: here (see drivers.tests_image_compression for that).
JPEG_BYTES = b"\xff\xd8\xff\xe0 real looking jpeg bytes"
HTML_PAYLOAD = b"<html><body><script>alert(1)</script></body></html>"


def _real_jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (120, 60, 200)).save(buf, format="JPEG")
    return buf.getvalue()


class PrepareDocumentUploadTests(TestCase):
    def test_a_real_jpeg_is_accepted(self):
        upload = SimpleUploadedFile("whatever.jpg", JPEG_BYTES, content_type="image/jpeg")
        prepared, error = prepare_document_upload(upload)
        self.assertEqual(error, "")
        self.assertIsNotNone(prepared)

    def test_content_type_is_overwritten_to_the_sniffed_type_not_the_declared_one(self):
        """The whole point: an attacker can declare anything they like."""
        upload = SimpleUploadedFile("evil.jpg", JPEG_BYTES, content_type="text/html")
        prepared, _ = prepare_document_upload(upload)
        self.assertEqual(prepared.content_type, "image/jpeg")

    def test_html_declared_as_an_image_is_rejected(self):
        """The actual attack this closes: spoof the Content-Type, keep a
        .jpg extension, ship real HTML/script bytes."""
        upload = SimpleUploadedFile("shot.jpg", HTML_PAYLOAD, content_type="image/jpeg")
        prepared, error = prepare_document_upload(upload)
        self.assertTrue(error)
        self.assertIsNone(prepared)

    def test_filename_is_randomized(self):
        """Two drivers uploading "license.jpg" must not collide — S3 file
        overwrite defaults on for this project."""
        one = SimpleUploadedFile("license.jpg", JPEG_BYTES, content_type="image/jpeg")
        two = SimpleUploadedFile("license.jpg", JPEG_BYTES, content_type="image/jpeg")
        prepared_one, _ = prepare_document_upload(one)
        prepared_two, _ = prepare_document_upload(two)
        self.assertNotEqual(prepared_one.name, prepared_two.name)
        self.assertNotEqual(prepared_one.name, "license.jpg")
        self.assertTrue(prepared_one.name.endswith(".jpg"))

    def test_oversized_file_is_rejected_without_reading_it(self):
        upload = SimpleUploadedFile("big.jpg", JPEG_BYTES, content_type="image/jpeg")
        upload.size = MAX_UPLOAD_BYTES + 1
        with mock.patch("drivers.document_uploads.sniff_content_type") as sniff:
            prepared, error = prepare_document_upload(upload)
        self.assertTrue(error)
        self.assertIsNone(prepared)
        self.assertFalse(sniff.called)

    def test_pdf_is_accepted_and_not_recompressed(self):
        pdf_bytes = b"%PDF-1.4 rest"
        upload = SimpleUploadedFile("permit.pdf", pdf_bytes, content_type="application/pdf")
        prepared, error = prepare_document_upload(upload)
        self.assertEqual(error, "")
        self.assertEqual(prepared.content_type, "application/pdf")
        self.assertEqual(prepared.read(), pdf_bytes)

    def test_garbage_is_rejected(self):
        upload = SimpleUploadedFile("mystery", b"not a real document at all", content_type="image/jpeg")
        prepared, error = prepare_document_upload(upload)
        self.assertTrue(error)
        self.assertIsNone(prepared)

    def test_a_decodable_photo_is_recompressed_to_webp(self):
        """The actual storage-savings feature: a real phone-photo-shaped
        JPEG comes back smaller, re-encoded as WebP, not stored at its
        original size."""
        original = _real_jpeg_bytes()
        upload = SimpleUploadedFile("license.jpg", original, content_type="image/jpeg")
        prepared, error = prepare_document_upload(upload)
        self.assertEqual(error, "")
        self.assertEqual(prepared.content_type, "image/webp")
        self.assertTrue(prepared.name.endswith(".webp"))
        stored = prepared.read()
        self.assertLess(len(stored), len(original))
        self.assertEqual(Image.open(io.BytesIO(stored)).format, "WEBP")

    def test_an_undecodable_but_correctly_sniffed_photo_falls_back_to_the_original(self):
        """JPEG_BYTES sniffs as real (magic bytes only) but Pillow can't open
        it — must not block the upload, must store what was actually sent."""
        upload = SimpleUploadedFile("whatever.jpg", JPEG_BYTES, content_type="image/jpeg")
        prepared, error = prepare_document_upload(upload)
        self.assertEqual(error, "")
        self.assertEqual(prepared.content_type, "image/jpeg")
        self.assertEqual(prepared.read(), JPEG_BYTES)


@override_settings(GOOGLE_MAPS_API_KEY="")
class DocumentUploadNotificationTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("notify_driver", first_name="Notify")
        self.driver = Driver.objects.create(profile=user, driver_type="inhouse")

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=["+14075551234", "+14075555678"])
    def test_texts_every_configured_number_with_the_document_label(self):
        with mock.patch("drivers.document_notifications.sms.send") as send:
            notify_staff_of_document_upload(self.driver, "chauffeur_permit_scan")
        self.assertEqual(send.call_count, 2)
        body = send.call_args_list[0].args[1]
        self.assertIn("chauffeur permit", body)
        self.assertIn(str(self.driver), body)

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=["+14075551234"])
    def test_dot_card_label_is_distinct_from_permit(self):
        with mock.patch("drivers.document_notifications.sms.send") as send:
            notify_staff_of_document_upload(self.driver, "dot_medical_card_scan")
        body = send.call_args.args[1]
        self.assertIn("DOT medical card", body)

    @override_settings(DOCUMENT_UPLOAD_NOTIFY_PHONES=[])
    def test_no_numbers_configured_is_a_silent_no_op(self):
        with mock.patch("drivers.document_notifications.sms.send") as send:
            notify_staff_of_document_upload(self.driver, "chauffeur_permit_scan")
        self.assertFalse(send.called)
