"""
drivers.image_compression.compress_to_webp

Invariants under test:
  * a real JPEG/PNG is resized + re-encoded to WebP, and shrinks
  * a PDF (or anything else not in COMPRESSIBLE_TYPES) is left alone
  * a payload Pillow can't open never raises — the caller gets None back and
    is expected to fall back to the original bytes
  * EXIF rotation is baked into the pixels before the tag is dropped
"""

import io

from django.test import SimpleTestCase
from PIL import Image

from drivers.image_compression import MAX_DIMENSION, compress_to_webp


def _jpeg_bytes(size=(3000, 2000), color=(200, 60, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class CompressToWebpTests(SimpleTestCase):
    def test_a_real_jpeg_is_recompressed_to_webp_and_shrinks(self):
        original = _jpeg_bytes()
        result = compress_to_webp(original, "image/jpeg")
        self.assertIsNotNone(result)
        new_bytes, new_type = result
        self.assertEqual(new_type, "image/webp")
        self.assertLess(len(new_bytes), len(original))
        image = Image.open(io.BytesIO(new_bytes))
        self.assertEqual(image.format, "WEBP")

    def test_oversized_dimensions_are_capped(self):
        new_bytes, _ = compress_to_webp(_jpeg_bytes(size=(6000, 4000)), "image/jpeg")
        image = Image.open(io.BytesIO(new_bytes))
        self.assertLessEqual(max(image.size), MAX_DIMENSION)

    def test_a_pdf_is_left_alone(self):
        self.assertIsNone(compress_to_webp(b"%PDF-1.4 not an image", "application/pdf"))

    def test_unreadable_bytes_return_none_instead_of_raising(self):
        """sniff_content_type only checks magic bytes — a truncated or
        corrupt file can still pass that check and reach here. Must not
        blow up the upload; the caller stores the original instead."""
        result = compress_to_webp(b"\xff\xd8\xff\xe0 not actually a decodable jpeg", "image/jpeg")
        self.assertIsNone(result)

    def test_exif_rotation_is_applied_before_the_tag_is_dropped(self):
        buf = io.BytesIO()
        image = Image.new("RGB", (400, 200), (10, 200, 10))
        exif = image.getexif()
        exif[0x0112] = 6  # "Rotate 90 CW" — landscape source, portrait display
        image.save(buf, format="JPEG", exif=exif)

        new_bytes, _ = compress_to_webp(buf.getvalue(), "image/jpeg")
        result = Image.open(io.BytesIO(new_bytes))
        # exif_transpose on tag 6 swaps width/height — confirms the rotation
        # was baked into the pixels, not silently discarded with the tag.
        self.assertEqual(result.size, (200, 400))
