"""Job details as text, for an operator to paste into their own system.

An operator (drivers.Driver.portal_role == 'operator') doesn't drive our jobs —
they re-key them into LimoAnywhere (or similar) and dispatch their own drivers.
Every extra minute of re-typing is a chance to transpose a flight number or a
phone digit, so the portal hands them the job as text they can paste in one go,
plus per-field values for the systems that want one box at a time.

Deliberately NO money. Not the customer's price, not the operator's rate. The
operator prices the job against their rate card with us; the copy block exists
to move TRIP FACTS accurately, and a stray dollar figure pasted into their
system is worse than useless.

``build_job_fields`` is the single source of truth: ``build_job_text`` is just
that list rendered. Add a field once and both the per-field copy chips and the
copy-everything block pick it up.
"""

import re

from django.utils import timezone

# Typographic characters -> their plain-ASCII equivalent. The copy block leaves
# our site and lands in somebody else's dispatch system, and those are routinely
# not UTF-8 clean: a pasted en dash or curly apostrophe comes back as %E2%80%93
# style percent codes or mojibake, which is how an operator ends up reading a
# job full of punctuation garbage. Everything we generate ourselves (the flight
# line's separator, "Yes - count not confirmed") and everything a dispatcher
# pastes into the notes from an email goes through this on the way out.
# Letters keep their accents — mangling a guest's name is worse than a stray
# character, and a name is the one field the operator reads rather than parses.
_ASCII_PUNCT = {
    "·": "-",   # ·  middle dot
    "•": "-",   # •  bullet
    "–": "-",   # –  en dash
    "—": "-",   # —  em dash
    "‘": "'",   # '  left single quote
    "’": "'",   # '  right single quote / apostrophe
    "“": '"',   # "  left double quote
    "”": '"',   # "  right double quote
    "…": "...",  # …  ellipsis
    " ": " ",   # non-breaking space
}
_ASCII_TABLE = str.maketrans(_ASCII_PUNCT)


def _ascii_safe(value):
    """Swap typographic punctuation for ASCII so a paste survives the trip."""
    return str(value).translate(_ASCII_TABLE)


def _fmt_time(t):
    if not t:
        return ""
    return t.strftime("%I:%M %p").lstrip("0")


def _fmt_date(d):
    if not d:
        return ""
    return d.strftime("%a %b %d, %Y").replace(" 0", " ")


def _fmt_phone(raw):
    """(407) 555-0134 for a US 10-digit; otherwise whatever we were given."""
    if not raw:
        return ""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return str(raw).strip()


_AIRPORT_CODE = re.compile(r"\(([A-Z]{3})\)")


def short_place(address):
    """A full Google address -> the bit a human says out loud.

    'Disney's Coronado Springs Resort, West Buena Vista Drive, Lake Buena Vista,
    FL, USA'  ->  "Disney's Coronado Springs Resort"
    'Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL, USA'
                                          ->  'MCO'

    Display only. The copy block keeps the FULL address, because that is what
    has to land in the operator's system — shortening what they paste would
    turn a cosmetic win into a driver at the wrong entrance.
    """
    if not address:
        return ""
    text = str(address).strip()

    code = _AIRPORT_CODE.search(text)
    if code:
        return code.group(1)

    parts = [p.strip() for p in text.split(",")]
    head = parts[0]
    # A bare street number means nothing on its own — keep the city with it.
    if re.match(r"^\d", head) and len(parts) > 1:
        return f"{head}, {parts[1]}"
    return head or text


def _flight_line(leg):
    """'DL 1234 · lands 3:45 PM' — airline, number, and the time that matters.

    For an arrival that's the landing time (when their driver has to be there);
    for a departure it's the take-off the guest must not miss. Uses the tracked
    estimate when AeroAPI has one, since that's the number an operator should be
    planning against, and says so.
    """
    flight = leg.flight_information
    if not flight:
        return ""
    ident = " ".join(p for p in [(flight.airline or "").strip(),
                                 (flight.flight_number or "").strip()] if p)
    ident = ident or "Flight"
    if leg.get_trip_type() != "arrival":
        return ident
    est = getattr(flight, "estimated_arrival_local", None)
    sched = getattr(flight, "scheduled_arrival_local", None)
    when = est or sched
    if not when:
        return ident
    label = "est. lands" if est and est != sched else "lands"
    return f"{ident} - {label} {_fmt_time(timezone.localtime(when))}"


def _seats_line(leg):
    """Car seats spelled out — the single most-missed detail on a farm-out.

    Delegates to Leg.display_carseats so the operator copies the exact string
    the chauffeur portal and the board show. One formatter, no drift.
    """
    return leg.display_carseats or ""


def _store_stop_line(leg):
    """The Publix grocery stop, on the one leg it actually rides on.

    An operator's driver has to make this stop and budget the 20 minutes for it,
    so it has to survive the re-key like any other trip fact. Gated on
    Leg.shows_store_stop (not the reservation flag) so it lands on the leg into
    town and not on the return.
    """
    return "Publix @9930 Universal Blvd (20 min)" if leg.shows_store_stop else ""


def build_job_fields(leg):
    """[(label, value), ...] for one leg — the facts an operator re-keys.

    Only non-empty fields come back, so the portal renders no blank rows and the
    copy block has no dangling labels. Order matches the way a reservation form
    is normally filled top to bottom.
    """
    reservation = leg.reservation
    customer = reservation.customer if reservation else None

    passenger = ""
    if customer:
        passenger = f"{customer.first_name} {customer.last_name}".strip()

    # effective_* everywhere below: leg columns are NULL-means-inherit overrides.
    vehicle = ""
    if leg.effective_vehicle:
        vehicle = str(leg.effective_vehicle)

    luggage = ""
    if leg.effective_luggage_count:
        luggage = str(leg.effective_luggage_count)
        if leg.effective_luggage_type:
            luggage += f" {leg.effective_luggage_type}"

    cruise = ""
    if leg.cruise_information:
        cruise = " ".join(
            p for p in [(leg.cruise_information.cruise_line or "").strip(),
                        (leg.cruise_information.ship_name or "").strip()] if p
        )

    notes = (reservation.special_requests or "").strip() if reservation else ""

    candidates = [
        ("Confirmation", reservation.display_number if reservation else ""),
        ("Passenger", passenger),
        ("Phone", _fmt_phone(customer.phone_number) if customer else ""),
        ("Date", _fmt_date(leg.pickup_date)),
        ("Pickup time", _fmt_time(leg.pickup_time)),
        ("Pickup", leg.pickup_location),
        ("Dropoff", leg.dropoff_location),
        ("Flight", _flight_line(leg)),
        ("Cruise", cruise),
        ("Vehicle", vehicle),
        ("Passengers", str(leg.effective_passenger_count or "")),
        ("Luggage", luggage),
        ("Car seats", _seats_line(leg)),
        ("Store stop", _store_stop_line(leg)),
        ("Notes", notes),
    ]
    return [
        (label, _ascii_safe(value).strip())
        for label, value in candidates
        if str(value or "").strip()
    ]


def build_job_text(leg):
    """The whole job as one pasteable block: 'Label: value' per line."""
    return "\n".join(f"{label}: {value}" for label, value in build_job_fields(leg))


def build_day_text(legs):
    """Every job for a day, separated by a rule — for loading tomorrow in one go."""
    blocks = [build_job_text(leg) for leg in legs]
    return "\n\n----------\n\n".join(b for b in blocks if b)
