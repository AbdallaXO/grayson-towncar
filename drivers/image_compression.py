"""Recompress an accepted document photo (license/permit/DOT-card image) to
WebP before it's stored.

A phone photo commonly runs several MB — more for a modern HEIC image — and
this app expects up to three of them per driver. At 30-50+ chauffeurs that's
real storage and egress for pixels nobody needs at full camera resolution: a
document only has to be legible when a staff member opens it, not printable
at poster size. Resized to a legible max dimension and re-encoded as WebP, a
typical phone photo this size lands well under 500 KB — usually 80-90%
smaller than the original.

Runs AFTER anything that needs the ORIGINAL bytes — most importantly license
OCR (drivers.license_ocr.scan_license), which calls AWS Textract's AnalyzeID
and only reads JPEG/PNG. Compressing first would silently break every
license scan. See drivers/document_uploads.py and drivers/views.py's
my_documents for the call order this depends on.

Never raises, and never blocks an upload: if Pillow can't open or re-encode
the photo for any reason (an unusual HEIC subtype, a corrupt file), the
caller gets None back and stores the original bytes untouched — the same
"always keep what was uploaded" principle license_ocr.py already follows
when OCR itself fails.
"""

import io
import logging

logger = logging.getLogger(__name__)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    # Falls back to storing the original HEIC untouched — see compress_to_webp.
    pass

#: Long-edge cap, in pixels. A document only needs to be legible when opened
#: by staff, not printable — comfortably larger than any screen this is
#: viewed on.
MAX_DIMENSION = 2000

#: Visually lossless for a document photo at the resize above; the main
#: lever on final file size.
WEBP_QUALITY = 82

#: Content types this module knows how to recompress. PDFs pass through
#: untouched elsewhere — WebP is a raster image format, and re-rastering a
#: document PDF would need a separate rendering step (poppler/pdf2image)
#: this app doesn't have.
COMPRESSIBLE_TYPES = {"image/jpeg", "image/png", "image/heic"}


def compress_to_webp(payload: bytes, content_type: str):
    """(new_bytes, "image/webp") on success, or None to mean "leave it
    alone" — either `content_type` isn't a photo type this recompresses, or
    recompression failed for any reason."""
    if content_type not in COMPRESSIBLE_TYPES:
        return None
    try:
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(payload))
        # Phone cameras store rotation as an EXIF tag rather than rotating
        # the pixels; re-encoding drops EXIF (fine — GPS/device metadata has
        # no business sitting in the media bucket) but would leave the photo
        # sideways unless the rotation is baked in first.
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format="WEBP", quality=WEBP_QUALITY)
        return buf.getvalue(), "image/webp"
    except Exception:
        logger.warning("document photo compression failed; storing original", exc_info=True)
        return None
