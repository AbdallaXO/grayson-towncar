"""
Source attribution helpers for Reservation booking_source.

Single source of truth for translating raw UTM/click-ID/agent state into
the canonical Reservation.booking_source channel value used by KPI
dashboards. Called from:
  - reservations.views.reservation_form (public booking)
  - reservations.admin.ReservationAdmin.save_model (phone / dispatcher entry)
  - The data migration that backfills booking_source on existing rows

Repeat-customer status is tracked SEPARATELY via Reservation.is_repeat_booking
so a returning customer who comes back via Google Ads still attributes the
revenue to Google Ads (not "repeat").
"""

# Source values that indicate Meta / Facebook / Instagram traffic.
META_SRC_TOKENS = {"facebook", "fb", "ig", "instagram", "meta"}

# utm_medium values that indicate paid traffic.
PAID_MEDIUM_TOKENS = {"cpc", "paid", "ads", "ppc", "paidsocial", "paid_social"}


def _normalize(value):
    return (value or "").strip().lower()


def derive_booking_source(reservation, request=None) -> str:
    """
    Return the canonical booking_source channel for a Reservation.

    Order of precedence (most specific wins):
      1. Staff/dispatcher creating from admin   -> "phone"
      2. Travel-agent attribution               -> "travel_agent"
      3. Google click ID present                -> "google_ads"
      4. Facebook click ID or meta utm_source   -> "meta_ads" / "meta_organic"
      5. Google in utm_source                   -> "google_ads" / "google_organic"
      6. Explicit referral medium/source         -> "referral"
      7. Otherwise                              -> "direct"

    The function is intentionally pure: it does not save the reservation.
    Callers are responsible for assigning the result to
    reservation.booking_source and saving.
    """
    # 1. Phone / dispatcher: any reservation created by a logged-in staff user
    if request is not None:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False):
            return "phone"

    # 2. Travel agent FK takes priority over UTM tracking
    if getattr(reservation, "travel_agent_id", None):
        return "travel_agent"

    medium = _normalize(getattr(reservation, "utm_medium", None))
    src = _normalize(getattr(reservation, "utm_source", None))

    # 3. Google Ads click ID is the strongest paid-google signal
    if getattr(reservation, "gclid", None):
        return "google_ads"

    # 4. Meta / Facebook / Instagram
    if getattr(reservation, "fbclid", None) or src in META_SRC_TOKENS:
        return "meta_ads" if medium in PAID_MEDIUM_TOKENS else "meta_organic"

    # 5. Generic Google source (organic SEO or non-gclid paid)
    if "google" in src:
        return "google_ads" if medium in PAID_MEDIUM_TOKENS else "google_organic"

    # 6. Explicit referral
    if medium == "referral" or src == "referral":
        return "referral"

    # 7. Default — no UTM, no agent, no staff context. Could be organic
    # direct traffic, repeat customer typing the URL, etc. We do NOT guess
    # "repeat" here; that lives on is_repeat_booking.
    return "direct"


def derive_is_repeat(reservation) -> bool:
    """
    True iff this reservation's customer has any earlier reservation.
    Match is by email (case-insensitive) since the same person can be
    represented by multiple Customer rows over time.
    """
    if not reservation.customer_id:
        return False
    email = (reservation.customer.email or "").strip()
    if not email:
        return False
    # Local import to avoid circular import at module load
    from reservations.models import Reservation
    qs = Reservation.objects.filter(customer__email__iexact=email)
    if reservation.pk:
        qs = qs.exclude(pk=reservation.pk)
    return qs.exists()
