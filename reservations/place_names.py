"""
Tidying for the addresses a dispatcher types into the booking wizard.

WHAT THIS IS FOR. ``DispatcherLegForm`` runs both address fields through
``tidy_address`` on the way in, so the string that reaches the database is the
one a dispatcher meant rather than the one their hands produced while the guest
is still on the phone: a doubled space, a space before the comma, a trailing
comma from a half-deleted line, an address typed with caps lock on.

WHAT THIS MUST NOT DO — the important half. It never changes WHICH PLACE the
text names. A stored address is not just words on a screen; four systems read
it as data:

  * ``quote_engine.match_location`` matches it against the ``Location`` rows to
    find the rate card, longest keyword wins — rewrite "MCO Airport" into
    "Orlando International Airport (MCO)" and the leg stops matching the
    "MCO Airport" location, so the trip silently reprices.
  * ``analytics.categorize_location`` / ``is_airport_location`` bucket it for
    route timing and trip type.
  * ``farmout_optimizer.is_port_or_sanford`` gates the airport permit rules.
  * ``booking_filters.short_place``, the wizard's own ``shortPlace()`` and the
    repeat-route summary all shorten by an EXACT replace of the string
    "Orlando International Airport (MCO)".

All four match case-insensitively on substrings, so changing case is safe and
changing words is not. Expanding a code into the "real" airport name is exactly
the kind of helpfulness that moves a price without anyone touching a price
field. So: hygiene, not correction. An address we do not recognise passes
through as written.

CASE IS ONLY TOUCHED WHEN IT CARRIES NO INFORMATION. If the letters are all one
case, the dispatcher told us nothing about how the name is written and we can
tidy it. If the text is already mixed case they typed it — or, far more often,
picked it off the wizard's datalist, which supplies properly written names — and
it is left byte for byte alone. This matters because step 5 prints the address
in bold inside a sentence read out loud to the guest, and a name in block
capitals reads as shouting.
"""
import re

# The Leg address columns are CharField(max_length=255). Putting a space after a
# comma is the one thing here that LENGTHENS a value, so a string that only just
# fit could be pushed over the column width. When that happens the spacing is
# given up and the whitespace-squeezed value is used, which only ever shortens —
# so a tidied address is never longer than the one that was typed.
MAX_ADDRESS_LENGTH = 255

# Codes that stay in capitals when a single-case address is recased. The airport
# codes are the ones dispatchers actually type, and the list matches
# advisor_display._AIRPORT_PLAIN, which is the board's vocabulary for the same
# places. It is duplicated rather than imported: reservations must not depend on
# dispatching, and dispatching already imports from here.
_KEEP_UPPER = {
    "MCO", "SFB", "MLB", "LAL", "TPA", "MIA", "FLL", "PBI", "RSW", "JAX",
    "SRQ", "DAB", "PIE",
    # Written the way they appear on a Central Florida address line.
    "FL", "USA", "US", "PO", "RV", "VIP",
    "N", "S", "E", "W", "NE", "NW", "SE", "SW",
}

# Two-letter postal codes. Applied only inside an address tail — after a comma,
# or directly before a ZIP — because "in", "or" and "me" are ordinary words
# everywhere else in a name.
_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI",
}

# Joining words a person does not capitalise mid-name: "Disney's Art of
# Animation", "Bay Lake Tower at the Contemporary". Never applied to the first
# word of the address.
_LOWER_WORDS = {
    "a", "an", "and", "at", "by", "de", "del", "for", "in", "la", "las", "of",
    "on", "or", "the", "to", "van", "von",
}

_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_ORDINAL_RE = re.compile(r"^(\d+)(ST|ND|RD|TH)$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"^(?P<lead>[^0-9A-Za-z]*)(?P<core>.*?)(?P<trail>[^0-9A-Za-z]*)$")
_HAS_DIGIT_RE = re.compile(r"\d")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")


def _squeeze(text):
    """Runs of whitespace become one space. Only ever removes characters."""
    return re.sub(r"\s+", " ", text).strip()


def _collapse(text):
    """Whitespace and comma hygiene. Never changes a letter.

    The spacing around every comma is made uniform, repeated commas collapse,
    and a dangling comma at either end goes.
    """
    out = re.sub(r"\s*,\s*", ",", _squeeze(text))  # " ,FL" and ",FL" -> ",FL"
    out = re.sub(r",{2,}", ",", out)               # ",,"  -> ","
    out = re.sub(r",", ", ", out)                  # ",FL" -> ", FL"
    return out.strip().strip(",").strip()


def _is_single_case(text):
    """True when the letters are all upper or all lower — i.e. case says nothing.

    Mixed case means someone wrote the name, and a name someone wrote is left
    exactly as they wrote it.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return all(c.isupper() for c in letters) or all(c.islower() for c in letters)


def _cap_word(word):
    """Capitalise one word, keeping an apostrophe's own word intact.

    ``str.title`` turns "disney's" into "Disney'S" and "o'brien" into "O'brien"
    — wrong both times. What separates a possessive from a name is how many
    letters follow the apostrophe.
    """
    parts = word.split("'")
    out = [parts[0][:1].upper() + parts[0][1:].lower()]
    for part in parts[1:]:
        if len(part) > 1:
            out.append(part[:1].upper() + part[1:].lower())
        else:
            out.append(part.lower())
    return "'".join(out)


def _recase_token(token, first, after_comma, next_token):
    """Recase one whitespace-separated token, leaving its punctuation where it is."""
    match = _TOKEN_RE.match(token)
    lead, core, trail = match.group("lead"), match.group("core"), match.group("trail")
    if not core:
        return token

    upper = core.upper()

    if upper in _KEEP_UPPER:
        return lead + upper + trail

    # A postal state code, but only where an address actually carries one.
    if (len(core) == 2 and upper in _STATE_CODES
            and (after_comma
                 or (next_token and _ZIP_RE.match(next_token.strip(",."))))):
        return lead + upper + trail

    ordinal = _ORDINAL_RE.match(core)
    if ordinal:
        return lead + ordinal.group(1) + ordinal.group(2).lower() + trail

    # Anything carrying a digit is a number, a route or a unit — "32819", "i-4",
    # "us-192", "a1a", "bldg 3". Guessing at its shape is how "A1A" becomes
    # "A1a", so a hyphenated one is recased part by part and a bare number is
    # left exactly as it is.
    if _HAS_DIGIT_RE.search(core):
        if "-" in core:
            return lead + "-".join(
                part.upper()
                if _HAS_DIGIT_RE.search(part) or part.upper() in _KEEP_UPPER
                else _cap_word(part)
                for part in core.split("-")
            ) + trail
        return lead + (upper if _HAS_LETTER_RE.search(core) else core) + trail

    if not first and core.lower() in _LOWER_WORDS:
        return lead + core.lower() + trail

    if "-" in core:
        return lead + "-".join(_cap_word(p) for p in core.split("-")) + trail

    return lead + _cap_word(core) + trail


def _recase(text):
    tokens = text.split(" ")
    out = []
    for i, token in enumerate(tokens):
        after_comma = i > 0 and tokens[i - 1].endswith(",")
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        out.append(_recase_token(token, i == 0, after_comma, nxt))
    return " ".join(out)


def tidy_address(value):
    """Clean up a typed pickup or drop-off address without renaming the place.

    Always returns a string: the Leg address columns are non-null, and the form
    reads the field with ``.get()``, which is None when the field never cleaned.
    """
    if value is None:
        return ""
    collapsed = _collapse(str(value))
    if not collapsed:
        return ""

    tidied = _recase(collapsed) if _is_single_case(collapsed) else collapsed
    if len(tidied) <= MAX_ADDRESS_LENGTH:
        return tidied

    # Comma spacing is the only step that lengthens a value, and it is the one
    # worth giving up: dropping it cannot cost the column anything the typed
    # address did not already cost it.
    squeezed = _squeeze(str(value))
    return _recase(squeezed) if _is_single_case(squeezed) else squeezed
