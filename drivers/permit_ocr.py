"""
Read a chauffeur-permit photo — the yellow City of Orlando "DRIVER PERMIT"
card — into structured fields, via AWS Textract's AnalyzeDocument Queries.

Why not AnalyzeID, like the license: AnalyzeID only understands standardized
identity documents (US/CA driver's licenses and passports). A county-issued
for-hire permit isn't in its vocabulary — run one through it and nothing
comes back, which is why the permit was photo-only for so long. But the
Orlando card prints stable labels next to every value (PERMIT#, FDL#,
Exp Date), and Textract Queries answers plain-English questions against the
document itself, so there is still no home-grown layout parser here to
drift when the city tweaks the card.

    from drivers.permit_ocr import scan_permit
    result = scan_permit(uploaded_file)
    result.ok            -> False when the service is unavailable or misreads
    result.fields        -> {"chauffeur_permit_number": "...", ...}
    result.error         -> operator-readable reason when not ok

Same contract as drivers.license_ocr.scan_license, deliberately: NOTHING here
writes to the database. The caller shows what came back as a PRE-FILLED FORM
the human confirms — the FDL# on the permit is cross-checked against the
license number on file (Driver.chauffeur_permit_fdl_mismatch), so a misread
digit that auto-committed would manufacture a false compliance alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field

# One gate, one client, one date parser — shared with the license scanner so
# the two can never drift apart on size caps, sniffing, or region/credentials.
from drivers.license_ocr import (
    MAX_BYTES, MIN_CONFIDENCE, _client, _parse_date, sniff_content_type,
)

logger = logging.getLogger(__name__)

#: Query alias -> (the question Textract answers, the Driver model field the
#: answer lands in). Aliases are what come back on the QUERY blocks, so they
#: are the stable key here — the question wording can be tuned freely.
_QUERIES = {
    "PERMIT_NUMBER": ("What is the permit number?", "chauffeur_permit_number"),
    "FDL_NUMBER": ("What is the FDL number?", "chauffeur_permit_fdl_number"),
    "EXPIRATION_DATE": ("What is the expiration date?", "chauffeur_permit_expiration"),
}

#: Fields whose answer is a printed date rather than free text.
_DATE_FIELDS = {"chauffeur_permit_expiration"}


@dataclass
class PermitScanResult:
    ok: bool
    fields: dict = dc_field(default_factory=dict)
    error: str = ""
    #: Field name -> Textract confidence, for surfacing "double-check this one".
    confidence: dict = dc_field(default_factory=dict)


def scan_permit(upload) -> PermitScanResult:
    """Extract chauffeur-permit fields from an uploaded image. Never raises."""
    # Cheap size check BEFORE reading the whole thing into memory.
    size = getattr(upload, "size", 0) or 0
    if size > MAX_BYTES:
        return PermitScanResult(
            ok=False,
            error="That photo was saved, but it's too large to read automatically — "
                  "please type the details in below.",
        )

    try:
        upload.seek(0)
        payload = upload.read()
        upload.seek(0)
    except Exception:
        logger.exception("permit scan: could not read the upload")
        return PermitScanResult(ok=False, error="Couldn't read that photo. Try again.")

    # Sniffed, not the browser-declared content_type — same reasoning as the
    # license scanner and views.py's upload guard. AnalyzeDocument also reads
    # single-page PDFs, but the JPEG/PNG gate is kept identical to the
    # license's so the two flows promise drivers the same thing.
    detected = sniff_content_type(payload)
    if detected not in ("image/jpeg", "image/png"):
        return PermitScanResult(
            ok=False,
            error="That file was saved, but only a JPG or PNG photo can be read "
                  "automatically — please type the details in below.",
        )

    try:
        response = _client().analyze_document(
            Document={"Bytes": payload},
            FeatureTypes=["QUERIES"],
            QueriesConfig={"Queries": [
                {"Text": text, "Alias": alias}
                for alias, (text, _) in _QUERIES.items()
            ]},
        )
    except Exception as exc:
        # Credentials missing, throttling, network — all the same to the
        # driver: type it in instead.
        logger.warning("permit scan: AnalyzeDocument call failed: %s", exc)
        return PermitScanResult(
            ok=False,
            error="Couldn't read the permit automatically. Your photo was still saved — "
                  "please type the details in below.",
        )

    blocks = response.get("Blocks") or []
    by_id = {block.get("Id"): block for block in blocks}

    extracted, confidence = {}, {}
    for block in blocks:
        if block.get("BlockType") != "QUERY":
            continue
        alias = ((block.get("Query") or {}).get("Alias") or "").upper()
        if alias not in _QUERIES:
            continue
        _, model_field = _QUERIES[alias]

        # A query can come back with several candidate answers; keep the one
        # Textract is most confident in. No ANSWER relationship at all means
        # the card (or the photo) simply doesn't show it.
        answers = []
        for rel in block.get("Relationships") or []:
            if rel.get("Type") != "ANSWER":
                continue
            for answer_id in rel.get("Ids") or []:
                answer = by_id.get(answer_id) or {}
                text = (answer.get("Text") or "").strip()
                score = answer.get("Confidence") or 0.0
                if text:
                    answers.append((score, text))

        # Logged BEFORE any filtering — the only record of what Textract
        # actually returned, same rationale as the license scanner's log line.
        logger.info("permit scan: query %s answers %r", alias, answers)

        if not answers:
            continue
        score, raw = max(answers)
        if score < MIN_CONFIDENCE:
            continue

        if model_field in _DATE_FIELDS:
            parsed = _parse_date(raw, None)
            if not parsed:
                continue
            extracted[model_field] = parsed
        else:
            extracted[model_field] = raw
        confidence[model_field] = score

    if not extracted:
        return PermitScanResult(
            ok=False,
            error="Couldn't make out the details on that photo. Your photo was saved — "
                  "try a flatter, brighter shot, or type the details in below.",
        )

    return PermitScanResult(ok=True, fields=extracted, confidence=confidence)
