"""
Spam scoring for public contact-form submissions.

One source of truth, used in three places:

  * ``users.forms.ContactUsFormSubmission.clean()`` — reject at the door, so the
    row is never written, no thank-you email goes to the spammer, and no
    dispatcher task is ever created.
  * ``ops.tasks._scan_uncontacted_forms()`` — never page a dispatcher about a
    submission that scores as spam, for rows that predate this module.
  * ``users/management/commands/clean_spam_contacts.py`` — clear the backlog.

Why a score instead of a boolean: every rule below was measured against the
full history of real submissions before being given a weight. Rules that hit
only spam and never a customer are worth BLOCK_THRESHOLD on their own. Rules
that also graze the occasional real customer are worth 1 and need a second
signal to agree — a 9-digit phone number is usually a bot, but it is sometimes
a customer who dropped a digit, and losing a booking inquiry costs far more
than reading one piece of spam.

The campaign the site is being hit with (names like "DanielchakyGM
KeithchakyGM" with 11-digit +7 phone numbers) trips two certain rules
independently, so it dies even if the bot changes one of them.
"""

import re

# A submission is spam once its signals add up to this.
BLOCK_THRESHOLD = 2

# Weight for a rule that has never matched a real customer.
CERTAIN = 2

CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

# Bot campaigns sign their names: a real first name, a nonsense word, then a
# two-letter campaign tag in caps — "DanielexenePW", "SarahKnownPA",
# "DanielchakyGM". Requiring a lowercase letter before the caps keeps genuine
# ALL-CAPS entries ("WADE REWA", "JANET FREY") and initials out of it.
CAMPAIGN_TAG_RE = re.compile(r"[a-z][A-Z]{2,}$")

# ...and the minimum length keeps a credential written into the name field
# ("SmithMD", "JonesPhD") out of it too. The shortest name this campaign has
# used is 11 characters, so nothing real is competing for this range.
CAMPAIGN_TAG_MIN_LENGTH = 10

SPAM_KEYWORDS = [
    "tinyurl.com",
    "bit.ly",
    "руб",
    "перевод",
    "сюрприз",
    "подарок",
    "новости",
    "ссылк",
    "joriuckror",
]

# Pitch language from cold-outreach spam. Deliberately excludes words a real
# travel agent uses about their own business — "affiliate", "commission",
# "leads" on their own all appear in genuine agency inquiries.
SOLICITATION_PHRASES = [
    "seo",
    "backlink",
    "guest post",
    "crypto",
    "bitcoin",
    "btc",
    "jackpot",
    "casino",
    "click here",
    "limited time",
    "unsubscribe",
    "digital marketing",
    "web design",
    "high-value leads",
    "ai tools",
    "make money",
    "passive income",
    "9-5",
    "funnel",
    "cold email",
    "rank higher",
    "traffic to your",
]

# Vocabulary of an actual ride inquiry. A long message containing none of it is
# not talking about a car, but this is weak on its own — some real customers
# open with a question before mentioning the trip.
RIDE_CONTEXT_WORDS = [
    "mco", "airport", "disney", "port", "pickup", "pick up", "transport",
    "quote", "flight", "hotel", "resort", "cruise", "ride", "car", "van",
    "suv", "passenger", "luggage", "shuttle", "universal", "orlando",
    "canaveral", "travel", "trip", "book", "arriv", "depart", "terminal",
    "driver", "seat", "reservation", "price", "rate",
]


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _shared_suffix_length(first, last):
    """How many trailing characters two names have in common, case-insensitive."""
    first, last = (first or "").lower(), (last or "").lower()
    n = 0
    while n < min(len(first), len(last)) and first[-1 - n] == last[-1 - n]:
        n += 1
    return n


def score_submission(first_name="", last_name="", email="", phone_number="", about=""):
    """
    Score one contact submission.

    Returns ``(score, reasons)`` — reasons are short slugs, safe to log and to
    show staff, so a blocked submission can always be explained after the fact.

    ``email`` is accepted but deliberately not scored: this campaign sends from
    ordinary gmail and yahoo addresses, so any rule based on it would have to
    reach for the domain reputation of real customers' mailboxes.
    """
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    about = about or ""

    names = f"{first_name} {last_name}"
    body = about.lower()
    combined = f"{names} {about}".lower()
    digits = _digits(phone_number)

    score = 0
    reasons = []

    def flag(reason, weight):
        nonlocal score
        score += weight
        reasons.append(reason)

    # ── Certain: measured against every real submission, zero customers hit ──

    if CYRILLIC_RE.search(combined):
        flag("cyrillic_text", CERTAIN)

    if URL_RE.search(names):
        flag("url_in_name", CERTAIN)

    if any(keyword in combined for keyword in SPAM_KEYWORDS):
        flag("spam_keyword", CERTAIN)

    # 11 digits starting with 8 is the Russian/Kazakh domestic trunk format.
    # A US number of that length starts with 1, so this cannot catch a local
    # customer — and it matches every entry of the current campaign.
    if len(digits) == 11 and digits.startswith("8"):
        flag("foreign_trunk_phone", CERTAIN)

    if any(
        len(name) >= CAMPAIGN_TAG_MIN_LENGTH and CAMPAIGN_TAG_RE.search(name)
        for name in (first_name, last_name)
    ):
        flag("campaign_tag_name", CERTAIN)

    # ── Weak: needs a second signal to agree ──

    if URL_RE.search(about):
        flag("url_in_message", 1)

    if any(phrase in body for phrase in SOLICITATION_PHRASES):
        flag("solicitation_pitch", 1)

    if first_name and first_name.lower() == last_name.lower():
        flag("identical_names", 1)
    elif _shared_suffix_length(first_name, last_name) >= 5:
        # "DanielchakyGM / KeithchakyGM" share a 7-character tail. Real pairs
        # like "Teagan Flanagan" only ever reach 4, hence the threshold.
        flag("shared_name_suffix", 1)

    if digits and len(digits) < 10:
        flag("short_phone", 1)

    if len(about) > 150 and not any(word in body for word in RIDE_CONTEXT_WORDS):
        flag("no_ride_context", 1)

    return score, reasons


def is_spam(first_name="", last_name="", email="", phone_number="", about=""):
    """True when a submission scores at or above the block threshold."""
    score, _ = score_submission(first_name, last_name, email, phone_number, about)
    return score >= BLOCK_THRESHOLD


def score_instance(instance):
    """Score a saved ContactUsForm row. Returns ``(score, reasons)``."""
    return score_submission(
        first_name=instance.first_name,
        last_name=instance.last_name,
        email=instance.email,
        phone_number=instance.phone_number,
        about=instance.about,
    )


def instance_is_spam(instance):
    """True when a saved ContactUsForm row scores as spam."""
    score, _ = score_instance(instance)
    return score >= BLOCK_THRESHOLD
