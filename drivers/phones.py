"""One way to store a phone number, one way to show it.

Driver phone numbers used to be free text — "407 555 0134", "(407) 555-0134",
"4075550134", "+14075550134" all lived side by side, so the directory looked
untidy, `tel:` links sometimes dialled a partial number, and the Twilio path
had to re-normalise on every send. Everything that writes a driver phone
number now goes through :func:`normalize` (stored as E.164, e.g.
``+14075550134``), and everything that shows one goes through :func:`pretty`
(``(407) 555-0134``) via the ``phone`` template filter.

US-centric on purpose: this is an Orlando company and its chauffeurs carry US
mobiles. A number typed with a leading ``+`` and a plausible length is kept as
typed so an international affiliate is never rejected.
"""
import re

_DIGITS = re.compile(r"\D+")


def normalize(raw):
    """E.164 form of ``raw``, or ``""`` when it is blank or not a phone number.

    * 10 digits ............................ ``+1`` + digits
    * 11 digits starting with 1 ............ ``+`` + digits
    * ``+`` followed by 8–15 digits ........ kept (international)
    * anything else ........................ ``""`` (caller decides: reject or keep raw)
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    plus = text.startswith("+")
    digits = _DIGITS.sub("", text)
    if plus:
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def is_valid(raw):
    return bool(normalize(raw))


def pretty(raw):
    """Human form: ``(407) 555-0134`` for a US number, ``+44 20 7946 0958``-style
    spacing is NOT attempted for others — they are shown as stored. Unparseable
    legacy values come back unchanged so nothing on file ever disappears."""
    if not raw:
        return ""
    e164 = normalize(raw)
    if not e164:
        return str(raw).strip()
    if e164.startswith("+1") and len(e164) == 12:
        d = e164[2:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return e164


def digits_only(raw):
    """Just the digits, for a ``tel:`` href or a search match."""
    return _DIGITS.sub("", str(raw or ""))


INVALID_MESSAGE = (
    "Enter a 10-digit US mobile number, like 407-555-0134, or a full "
    "international number starting with +."
)
