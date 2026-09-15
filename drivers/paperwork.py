"""Paperwork status for the driver directory.

Driver.credential_alerts() answers one question — is anything expired or
about to expire? — and deliberately says nothing when a date was never
entered, so a field being backfilled over time doesn't flag the whole roster
on day one (see models.py). The directory needs the other half of the
picture: who has NOT sent a license or permit yet, and whose photo is
sitting in storage with no details typed in (the case
document_notifications.py exists for). This module reads the same Driver
fields and gives each of the three documents one plain state, plus the
roster-level counts behind the tiles at the top of the directory.

Nothing here decides which documents a driver *must* carry. The DOT medical
card is optional on the model ("if applicable"), so it is shown but never
counted as missing.
"""
from django.utils import timezone
from django.utils.formats import date_format

# One entry per document, in the order the directory lists them.
DOCUMENTS = (
    {
        "key": "license", "label": "License", "long": "driver's license",
        "scan": "license_scan", "expiration": "license_expiration", "required": True,
    },
    {
        "key": "permit", "label": "Permit", "long": "chauffeur permit",
        "scan": "chauffeur_permit_scan", "expiration": "chauffeur_permit_expiration", "required": True,
    },
    {
        "key": "dot", "label": "DOT card", "long": "DOT medical card",
        "scan": "dot_medical_card_scan", "expiration": "dot_medical_card_expiration", "required": False,
    },
)

EXPIRED = "expired"        # the expiration date is in the past
EXPIRING = "expiring"      # within Driver.CREDENTIAL_WARNING_DAYS
ON_FILE = "on_file"        # details entered; photo may or may not be attached
PHOTO_ONLY = "photo_only"  # a photo was uploaded but nobody entered the details
MISSING = "missing"        # nothing at all

# Directory filter keys → what the tile says. Order = order of the tiles.
FILTERS = (
    ("missing_license", "Missing a license"),
    ("missing_permit", "Missing a permit"),
    ("expiring", "Expired or expiring"),
    ("attention", "Needs a look"),
    ("complete", "License and permit on file"),
)
FILTER_KEYS = tuple(key for key, _ in FILTERS)
FILTER_LABELS = dict(FILTERS)

# The one-line explanation under each tile, and what to say when a filter
# comes back empty — plain words, no field names.
FILTER_SUBTITLES = {
    "missing_license": "nothing on file at all",
    "missing_permit": "nothing on file at all",
    "expiring": "expired, or expires within 30 days",
    "attention": "photo sent but details not typed in, or permit number doesn't match",
    "complete": "both on file and not expired",
}
FILTER_EMPTY_MESSAGES = {
    "missing_license": "Every active chauffeur has a license on file.",
    "missing_permit": "Every active chauffeur has a permit on file.",
    "expiring": "Nothing is expired or expiring in the next 30 days.",
    "attention": "Nothing needs a look.",
    "complete": "Nobody has both a license and a permit on file yet.",
}


def _scan_url(scan):
    """Storage URL for a saved scan, or "" — signing can fail when storage
    credentials aren't configured, and the directory must still render."""
    try:
        return scan.url
    except Exception:  # noqa: BLE001 — any storage backend error
        return ""


def _detail(state, expiration, days, has_scan, mismatch):
    """The words shown next to the document name in the directory."""
    if state == MISSING:
        text = "not on file"
    elif state == PHOTO_ONLY:
        text = "photo sent, details not entered"
    elif state == EXPIRED:
        text = f"expired {date_format(expiration, 'M j, Y')}"
    elif state == EXPIRING:
        if days <= 0:
            text = "expires today"
        elif days == 1:
            text = "expires tomorrow"
        else:
            text = f"expires in {days} days"
    else:
        text = f"expires {date_format(expiration, 'M j, Y')}"
        if not has_scan:
            text += " · no photo"
    if mismatch:
        text += " · number doesn't match the license"
    return text


def document_state(driver, doc, today=None, with_urls=True):
    """One dict describing where `doc` (an entry of DOCUMENTS) stands for `driver`."""
    today = today or timezone.localdate()
    scan = getattr(driver, doc["scan"])
    expiration = getattr(driver, doc["expiration"])
    has_scan = bool(scan)
    days = None
    if expiration is None:
        state = PHOTO_ONLY if has_scan else MISSING
    else:
        days = (expiration - today).days
        if days < 0:
            state = EXPIRED
        elif days <= driver.CREDENTIAL_WARNING_DAYS:
            state = EXPIRING
        else:
            state = ON_FILE
    mismatch = doc["key"] == "permit" and driver.chauffeur_permit_fdl_mismatch
    return {
        "key": doc["key"],
        "label": doc["label"],
        "long": doc["long"],
        "required": doc["required"],
        "state": state,
        "expiration": expiration,
        "days": days,
        "has_scan": has_scan,
        "scan_url": _scan_url(scan) if (has_scan and with_urls) else "",
        "mismatch": mismatch,
        "detail": _detail(state, expiration, days, has_scan, mismatch),
    }


def summarize(driver, today=None, with_urls=True):
    """All three documents plus the flags the directory filters and counts on.

    `flags` is a set drawn from FILTER_KEYS. A driver can carry several at
    once (missing a permit AND a license about to expire). "Missing" means
    nothing at all was sent — a photo without details is the office's job,
    not the driver's, so it lands under "attention" instead.
    """
    docs = [document_state(driver, doc, today, with_urls) for doc in DOCUMENTS]
    by_key = {d["key"]: d for d in docs}
    license_, permit = by_key["license"], by_key["permit"]

    flags = set()
    if license_["state"] == MISSING:
        flags.add("missing_license")
    if permit["state"] == MISSING:
        flags.add("missing_permit")
    if any(d["state"] in (EXPIRED, EXPIRING) for d in docs):
        flags.add("expiring")
    if any(d["state"] == PHOTO_ONLY for d in docs) or permit["mismatch"]:
        flags.add("attention")
    if (license_["state"] in (ON_FILE, EXPIRING)
            and permit["state"] in (ON_FILE, EXPIRING)):
        flags.add("complete")

    # Row accent: red for something wrong today, amber for something to do soon.
    if any(d["state"] == EXPIRED for d in docs) or permit["mismatch"]:
        accent = "red"
    elif any(d["state"] in (EXPIRING, PHOTO_ONLY) for d in docs):
        accent = "amber"
    else:
        accent = ""

    # Which required documents the driver still has to send — drives the
    # "Text … to ask" link. Photo-only isn't on this list on purpose.
    owed = [d["long"] for d in docs if d["required"] and d["state"] == MISSING]

    return {"docs": docs, "flags": flags, "accent": accent, "owed": owed}


def first_name_for(driver):
    """How the office addresses this driver. Many accounts have no first name
    and a lowercase username ("neuma"); capitalise just the first letter so
    "Hi Neuma" reads right without mangling a name like "AldoH"."""
    name = (driver.profile.first_name or driver.profile.username or "").strip()
    return name[:1].upper() + name[1:]


def ask_sms_href(driver, summary):
    """An `sms:` link that opens the dispatcher's Messages app with the request
    already written, or "" when there is nothing to ask for or no number to
    send it to. Nothing is sent by this app — a person hits send."""
    owed = summary["owed"]
    phone = (driver.phone_number or "").strip()
    if not owed or not phone:
        return ""
    from drivers.client_messages import sms_href  # shares the app's phone normalization

    first = first_name_for(driver)
    what = " and ".join(owed)
    body = (
        f"Hi {first}, could you send us a photo of your {what}? "
        f"Open the driver app, tap Documents, and take the photo there. Thanks!"
    )
    return sms_href(phone, body)


def roster_counts(summaries):
    """Tile numbers for a roster of summaries (the active chauffeurs)."""
    filters = {key: 0 for key in FILTER_KEYS}
    on_file = {doc["key"]: 0 for doc in DOCUMENTS}
    for summary in summaries:
        for key in summary["flags"]:
            filters[key] += 1
        for doc in summary["docs"]:
            if doc["state"] in (ON_FILE, EXPIRING, EXPIRED):
                on_file[doc["key"]] += 1
    return {"filters": filters, "on_file": on_file, "total": len(summaries)}
