"""
Read a driver's-license photo into structured fields, via AWS Textract's
AnalyzeID.

AnalyzeID is purpose-built for identity documents and returns NAMED fields
(DOCUMENT_NUMBER, EXPIRATION_DATE, ...) rather than the loose word soup plain
OCR gives back, so there is no parsing heuristic here to drift. It is US/CA
licenses AND PASSPORTS — which is why ID_TYPE is checked below rather than
just discarded: a passport reads just as cleanly as a license and would
otherwise pre-fill a license-number field with a passport number. The
chauffeur permit and DOT medical card are NOT run through this at all: those
are issued per-county with no standard layout AnalyzeID knows, so it returns
nothing for them. The permit has its own scanner (drivers.permit_ocr, built
on Textract Queries against the labeled Orlando card); the DOT medical card
remains photo-only.

    from drivers.license_ocr import scan_license
    result = scan_license(uploaded_file)
    result.ok            -> False when the service is unavailable or misreads
    result.fields        -> {"license_number": "...", "license_expiration": date(...)}
    result.error         -> operator-readable reason when not ok

NOTHING here writes to the database. The caller shows what came back as a
PRE-FILLED FORM the human confirms — a misread digit on a license number is a
compliance problem, not a cosmetic one, so the extraction never auto-commits.
The name/DOB/address fields are exactly as sensitive as the rest of this — a
misread birthdate is no less a problem than a misread license number, so the
same confirm-before-save rule applies uniformly, not just to the "official"
fields.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime

logger = logging.getLogger(__name__)

#: Textract AnalyzeID caps a synchronous request at 10 MB and reads JPEG/PNG
#: only (no PDF, no HEIC). Phone cameras happily produce all three, so the
#: gate lives here rather than surfacing as a 400 from AWS.
MAX_BYTES = 10 * 1024 * 1024

#: Below this, AnalyzeID is guessing — a blurry or angled phone photo. Better
#: to send the driver back for a second shot than to pre-fill a wrong number
#: they might tap past.
MIN_CONFIDENCE = 80.0

#: Textract field name -> Driver model field, for straightforward one-to-one
#: fields. Name (FIRST_NAME/MIDDLE_NAME/LAST_NAME/SUFFIX) and address
#: (ADDRESS/CITY_IN_ADDRESS/ZIP_CODE_IN_ADDRESS) are handled separately below
#: — several Textract fields combine into one model field there. STATE_NAME
#: (the issuing authority) and STATE_IN_ADDRESS (the state in the holder's
#: mailing address) are DIFFERENT fields on the same document; STATE_NAME is
#: preferred for license_state (see _STATE_FIELD_PRIORITY), and
#: STATE_IN_ADDRESS is collected here ONLY so _assemble_address can also use
#: it — it is never a license_state fallback candidate on its own merit, just
#: whichever the priority order below picks. ID_TYPE is collected but
#: deliberately absent from this map — see _NON_LICENSE_ID_TYPES below.
_FIELD_MAP = {
    "DOCUMENT_NUMBER": "license_number",
    "EXPIRATION_DATE": "license_expiration",
    "CLASS": "license_class",
    "STATE_NAME": "license_state",
    "STATE_IN_ADDRESS": "license_state",
    "DATE_OF_BIRTH": "license_date_of_birth",
    "FIRST_NAME": None,
    "MIDDLE_NAME": None,
    "LAST_NAME": None,
    "SUFFIX": None,
    "ADDRESS": None,
    "CITY_IN_ADDRESS": None,
    "ZIP_CODE_IN_ADDRESS": None,
}

#: Preference order when more than one Textract field maps to the same model
#: field (today: license_state). Earlier wins.
_STATE_FIELD_PRIORITY = ("STATE_NAME", "STATE_IN_ADDRESS")

#: Order matters — this is printed name order, not alphabetical.
_NAME_PART_FIELDS = ("FIRST_NAME", "MIDDLE_NAME", "LAST_NAME", "SUFFIX")

#: Full state/territory name (as AnalyzeID prints it, e.g. "FLORIDA") -> the
#: 2-letter code the rest of this app assumes (Driver.license_state's
#: help_text says "e.g. FL"). Anything not in here — a Canadian province, a
#: name Textract mangled — passes through unchanged; the driver corrects it
#: on the confirm screen same as any other misread.
_STATE_ABBREVIATIONS = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "PUERTO RICO": "PR", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

#: Substrings of ID_TYPE that mean "not a driver's license" — AnalyzeID reads
#: US/CA passports too, and one scans just as cleanly as a license.
_NON_LICENSE_ID_TYPES = ("PASSPORT",)


@dataclass
class LicenseScanResult:
    ok: bool
    fields: dict = dc_field(default_factory=dict)
    error: str = ""
    #: Field name -> Textract confidence, for surfacing "double-check this one".
    confidence: dict = dc_field(default_factory=dict)


def sniff_content_type(payload: bytes) -> str | None:
    """Identify a file by its own magic bytes — never trust a browser- or
    OS-supplied Content-Type, which is attacker-controlled on any upload
    endpoint and is routinely wrong even for honest clients (Android file
    pickers hand out application/octet-stream for a perfectly good JPEG).
    Recognizes exactly what this app's document uploads accept."""
    if not payload:
        return None
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"%PDF-"):
        return "application/pdf"
    if len(payload) >= 12 and payload[4:8] == b"ftyp" and payload[8:12] in (
        b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
        b"mif1", b"msf1",
    ):
        return "image/heic"
    return None


def _parse_date(raw, normalized):
    """AnalyzeID normalizes dates to ISO 8601 when it is confident enough.
    Fall back to the common US printed forms when it does not. Used for both
    EXPIRATION_DATE and DATE_OF_BIRTH — same field shape, same fallback."""
    if normalized:
        try:
            return datetime.fromisoformat(normalized.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _normalize_state(raw: str) -> str:
    upper = raw.strip().upper()
    if len(upper) == 2:
        return upper
    return _STATE_ABBREVIATIONS.get(upper, raw.strip())


def _titlecase_name_part(raw: str) -> str:
    """Licenses print names ALL CAPS almost universally — "O'BRIEN" reads
    better as "O'Brien" than shouted. Only touches text that's ALL CAPS to
    begin with, so a document that's already mixed-case is left alone."""
    raw = raw.strip()
    return raw.title() if raw.isupper() else raw


def _assemble_name(by_name):
    """FIRST_NAME/MIDDLE_NAME/LAST_NAME/SUFFIX -> one printed-order string,
    using whichever parts AnalyzeID actually read. Confidence is the weakest
    of the parts used — one bad part makes the whole name suspect."""
    parts, scores = [], []
    for key in _NAME_PART_FIELDS:
        if key in by_name:
            raw, score, _ = by_name[key]
            parts.append(_titlecase_name_part(raw))
            scores.append(score)
    if not parts:
        return None, None
    return " ".join(parts), min(scores)


def _assemble_address(by_name):
    """ADDRESS (street) + CITY_IN_ADDRESS + STATE_IN_ADDRESS + ZIP_CODE_IN_ADDRESS
    -> one "123 Main St, Orlando, FL 32801"-shaped line, using whichever parts
    AnalyzeID actually read. Reference only — see Driver.license_address's
    help_text; a license address goes stale the moment someone moves."""
    def _text(key):
        entry = by_name.get(key)
        return entry[0] if entry else None

    street = _text("ADDRESS")
    city = _text("CITY_IN_ADDRESS")
    state = _text("STATE_IN_ADDRESS")
    zip_code = _text("ZIP_CODE_IN_ADDRESS")

    locality = " ".join(p for p in (state, zip_code) if p)
    line2 = ", ".join(p for p in (city, locality) if p)
    full = ", ".join(p for p in (street, line2) if p)
    if not full:
        return None, None

    scores = [by_name[k][1] for k in ("ADDRESS", "CITY_IN_ADDRESS", "STATE_IN_ADDRESS", "ZIP_CODE_IN_ADDRESS")
              if k in by_name]
    return full, (min(scores) if scores else None)


def _client():
    """Credentials come from boto3's default chain — AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY are its own env var names, the same ones settings.py
    hands to S3, so there is nothing to wire through explicitly."""
    import boto3

    return boto3.client(
        "textract",
        region_name=os.getenv("AWS_S3_REGION_NAME", "us-east-1"),
    )


def scan_license(upload) -> LicenseScanResult:
    """Extract license fields from an uploaded image. Never raises."""
    # Cheap size check BEFORE reading the whole thing into memory — no point
    # buffering a 300 MB file just to reject it a moment later.
    size = getattr(upload, "size", 0) or 0
    if size > MAX_BYTES:
        return LicenseScanResult(
            ok=False,
            error="That photo was saved, but it's too large to read automatically — "
                  "please type the details in below.",
        )

    try:
        upload.seek(0)
        payload = upload.read()
        upload.seek(0)
    except Exception:
        logger.exception("license scan: could not read the upload")
        return LicenseScanResult(ok=False, error="Couldn't read that photo. Try again.")

    # Sniffed, not the browser-declared content_type: a JPEG a file manager
    # labelled application/octet-stream is still perfectly readable, and
    # trusting the declared type is also how an attacker gets an arbitrary
    # payload past this same gate elsewhere (see views.py's upload guard).
    detected = sniff_content_type(payload)
    if detected not in ("image/jpeg", "image/png"):
        return LicenseScanResult(
            ok=False,
            error="That file was saved, but only a JPG or PNG photo can be read "
                  "automatically — please type the details in below.",
        )

    try:
        response = _client().analyze_id(DocumentPages=[{"Bytes": payload}])
    except Exception as exc:
        # Credentials missing, region without AnalyzeID, throttling, network.
        # All of them mean the same thing to the driver: type it in instead.
        logger.warning("license scan: AnalyzeID call failed: %s", exc)
        return LicenseScanResult(
            ok=False,
            error="Couldn't read the license automatically. Your photo was still saved — "
                  "please type the details in below.",
        )

    documents = response.get("IdentityDocuments") or []
    if not documents:
        return LicenseScanResult(
            ok=False,
            error="That didn't look like a driver's license. Your photo was saved — "
                  "please type the details in below.",
        )

    # Collect every field of interest by ITS OWN Textract name first — two
    # different names (STATE_NAME / STATE_IN_ADDRESS) can map to the same
    # model field, and AnalyzeID's field order isn't something to build
    # "last one wins" precedence on.
    by_name = {}
    id_type_text = ""
    for entry in documents[0].get("IdentityDocumentFields") or []:
        name = ((entry.get("Type") or {}).get("Text") or "").upper()
        value_block = entry.get("ValueDetection") or {}
        raw = (value_block.get("Text") or "").strip()
        score = value_block.get("Confidence") or 0.0

        # Logged BEFORE any filtering — this is the only record of what
        # Textract actually returned. Without it, "why didn't field X show
        # up" is unanswerable after the fact: the raw response isn't
        # persisted anywhere, and the next scan can return something
        # different (a passport-page-2 field set, a different confidence).
        logger.info("license scan: Textract field %s = %r (confidence %.1f)", name, raw, score)

        if name == "ID_TYPE" and raw:
            id_type_text = raw.upper()
            continue
        if name not in _FIELD_MAP or not raw or score < MIN_CONFIDENCE:
            continue
        by_name[name] = (raw, score, value_block.get("NormalizedValue", {}).get("Value"))

    if id_type_text and any(bad in id_type_text for bad in _NON_LICENSE_ID_TYPES):
        return LicenseScanResult(
            ok=False,
            error="That looks like a passport, not a driver's license. Your photo was "
                  "saved — please upload a license photo, or type the details in below.",
        )

    extracted, confidence = {}, {}

    if "DOCUMENT_NUMBER" in by_name:
        extracted["license_number"], confidence["license_number"], _ = by_name["DOCUMENT_NUMBER"]

    if "CLASS" in by_name:
        extracted["license_class"], confidence["license_class"], _ = by_name["CLASS"]

    for state_field in _STATE_FIELD_PRIORITY:
        if state_field in by_name:
            raw, score, _ = by_name[state_field]
            extracted["license_state"] = _normalize_state(raw)
            confidence["license_state"] = score
            break

    if "EXPIRATION_DATE" in by_name:
        raw, score, normalized = by_name["EXPIRATION_DATE"]
        parsed = _parse_date(raw, normalized)
        if parsed:
            extracted["license_expiration"] = parsed
            confidence["license_expiration"] = score

    if "DATE_OF_BIRTH" in by_name:
        raw, score, normalized = by_name["DATE_OF_BIRTH"]
        parsed = _parse_date(raw, normalized)
        if parsed:
            extracted["license_date_of_birth"] = parsed
            confidence["license_date_of_birth"] = score

    full_name, name_score = _assemble_name(by_name)
    if full_name:
        extracted["license_full_name"] = full_name
        confidence["license_full_name"] = name_score

    address, address_score = _assemble_address(by_name)
    if address:
        extracted["license_address"] = address
        confidence["license_address"] = address_score

    if not extracted:
        return LicenseScanResult(
            ok=False,
            error="Couldn't make out the details on that photo. Your photo was saved — "
                  "try a flatter, brighter shot, or type the details in below.",
        )

    return LicenseScanResult(ok=True, fields=extracted, confidence=confidence)


def is_expiration_plausible(value) -> bool:
    """A license that expired years ago, or expires a lifetime from now, is a
    misread far more often than a real date. Used to warn, never to reject —
    a genuinely lapsed license is exactly what staff need to see."""
    if not isinstance(value, date):
        return False
    today = date.today()
    return (today - value).days < 3650 and (value - today).days < 7300


def is_date_of_birth_plausible(value) -> bool:
    """Same idea for DATE_OF_BIRTH: nobody driving for this company is under
    ~16 or over 100. Outside that range is a misread, not a real driver."""
    if not isinstance(value, date):
        return False
    age_days = (date.today() - value).days
    return 16 * 365 <= age_days <= 100 * 365
