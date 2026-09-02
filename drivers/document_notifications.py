"""SMS notification when a driver uploads a permit or DOT-medical-card photo.

The license path self-completes: OCR reads it and the driver confirms the
details right there. The permit now scans too (drivers.permit_ocr), but its
scan can fail — or the driver can abandon the confirm step with the photo
already saved — and the DOT card has no OCR at all. In every one of those
cases Driver.credential_alerts() stays silent until an expiration date
exists (a blank date deliberately isn't an alert, see models.py), so a photo
can sit in S3 untranscribed indefinitely with nothing on the staff side ever
flagging it. This is the only thing that does — which is why the permit
upload sends it whether or not its scan succeeded.
"""

import logging

from django.conf import settings

from drivers import sms

logger = logging.getLogger(__name__)

DOCUMENT_LABELS = {
    "chauffeur_permit_scan": "chauffeur permit",
    "dot_medical_card_scan": "DOT medical card",
}


def notify_staff_of_document_upload(driver, field_name):
    """Text every configured number that `driver` uploaded a photo for
    `field_name` (one of DOCUMENT_LABELS) and needs it transcribed."""
    phones = getattr(settings, "DOCUMENT_UPLOAD_NOTIFY_PHONES", []) or []
    if not phones:
        logger.info(
            "DOCUMENT_UPLOAD_NOTIFY_PHONES unset; skipping upload SMS for driver %s", driver.id,
        )
        return
    label = DOCUMENT_LABELS.get(field_name, field_name)
    # "entered or checked": the permit scan may have pre-filled the details
    # and the driver confirmed them — staff verify rather than transcribe.
    body = (
        f"{driver} uploaded a {label} photo — needs the number/expiration "
        f"entered or checked on their driver profile."
    )
    for phone in phones:
        sms.send(phone, body)
