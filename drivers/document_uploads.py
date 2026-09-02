"""
Shared safety gate for every driver-uploaded document photo (license, permit,
DOT medical card) before it touches storage.

Four things every upload path needs, that a plain Django FileField gives you
none of:

  * the file actually IS one of the types this app claims to accept — sniffed
    from its own bytes (drivers.license_ocr.sniff_content_type), never the
    client-declared Content-Type, which is attacker-controlled on any
    multipart POST and routinely wrong even for honest clients

  * a size ceiling, checked before anything is read into memory

  * a Content-Type that gets STORED and RE-SERVED as the sniffed type, not
    whatever the browser said. The media bucket is a public custom domain
    (media.graysontowncar.com per business/settings.py) and django-storages
    persists + re-serves the upload's declared Content-Type verbatim — so
    trusting it would let any logged-in driver stash a text/html (or worse)
    payload behind a company-owned origin, served back with that content type

  * a unique storage name, so two drivers uploading a same-named file (very
    common — "license.jpg", "IMG_1234.jpg") don't silently overwrite each
    other. AWS_S3_FILE_OVERWRITE defaults True project-wide.

A JPEG/PNG/HEIC photo is additionally recompressed to WebP
(drivers.image_compression) before it's handed back — see that module's
docstring for why, and for why this must run AFTER anything that needs the
original bytes (namely license OCR).

    from drivers.document_uploads import prepare_document_upload
    prepared, error = prepare_document_upload(upload)
    if error:
        ...tell the user, don't save...
    driver.license_scan = prepared   # now safe: sniffed+compressed type, unique name
    driver.save(update_fields=["license_scan"])

A caller that ALSO needs the original bytes for something else — namely
license OCR, which must never run on a spoofed or unrecognized upload — should
call sniff_and_validate() first and only proceed to whatever needs the
original once that passes; see drivers/views.py's my_documents.
"""

import uuid

from django.core.files.base import ContentFile

from drivers.image_compression import compress_to_webp
from drivers.license_ocr import sniff_content_type

#: Generous relative to a phone photo, but not so large that an unauthenticated
#: (well, any-logged-in-driver) upload endpoint becomes a free multi-hundred-MB
#: write channel into the bucket. Checked against the ORIGINAL upload, before
#: compression — compression only ever shrinks a photo, so this stays the
#: real ceiling on what a driver can push through the endpoint.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

_EXTENSION_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "application/pdf": "pdf",
    "image/heic": "heic",
    "image/webp": "webp",
}


def sniff_and_validate(upload):
    """Read `upload`, sniff its real content type, and enforce the size cap.
    Returns (content_type, "") on success, or (None, user-facing error) on
    failure.

    Read-only — never mutates or compresses `upload`. Exposed separately from
    prepare_document_upload() so a caller that also needs the ORIGINAL bytes
    for something else (namely license OCR, which must never run on an
    unrecognized or oversized upload) can validate first and use the result
    before compression touches anything.
    """
    size = getattr(upload, "size", 0) or 0
    if size > MAX_UPLOAD_BYTES:
        return None, "That file is too large — please use one under 15 MB."

    try:
        upload.seek(0)
        payload = upload.read()
        upload.seek(0)
    except Exception:
        return None, "Couldn't read that file. Please try again."

    detected = sniff_content_type(payload)
    if detected is None:
        return None, "That doesn't look like a photo or PDF — please try a different file."
    return detected, ""


def prepare_document_upload(upload):
    """Validate `upload` (via sniff_and_validate) and return (file, error).

    On success, `error` is "" and `file` is a NEW Django File — safe to
    assign straight to a model FileField — with a sniffed, true content type,
    a random collision-proof name, and (for a JPEG/PNG/HEIC photo) resized
    and re-encoded to WebP; a PDF passes through with its bytes unchanged.

    On failure, `error` is a user-facing message and `file` is None.
    """
    detected, error = sniff_and_validate(upload)
    if error:
        return None, error

    upload.seek(0)
    payload = upload.read()

    compressed = compress_to_webp(payload, detected)
    if compressed:
        payload, detected = compressed

    name = f"{uuid.uuid4().hex}.{_EXTENSION_BY_TYPE[detected]}"
    prepared = ContentFile(payload, name=name)
    prepared.content_type = detected
    return prepared, ""
