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


def find_booking_source_drift():
    """
    Return (forward_qs, reverse_qs) — reservations whose stored booking_source
    disagrees with their travel_agent FK:
      forward: travel agent linked but booking_source != 'travel_agent'
      reverse: booking_source == 'travel_agent' but no agent FK (orphan label)

    Used by both the recompute_booking_source command and the in-app
    "Fix attribution drift" button so they stay identical.
    """
    from reservations.models import Reservation
    forward = Reservation.objects.filter(travel_agent__isnull=False).exclude(
        booking_source="travel_agent"
    )
    reverse = Reservation.objects.filter(
        booking_source="travel_agent", travel_agent__isnull=True
    )
    return forward, reverse


def repair_booking_source_drift(apply=False) -> dict:
    """
    Repair travel-agent drift in Reservation.booking_source. Pure-counts when
    apply=False (preview); writes inside one transaction when apply=True.

    forward: agent-linked bookings mislabeled google/meta/direct -> 'travel_agent'
    reverse: orphan 'travel_agent' labels (no agent) -> re-derived real source

    Leaves every other row untouched (e.g. 'phone'). Idempotent. Returns
    {"forward": n, "reverse": n, "total": n, "applied": bool}.
    """
    from django.db import transaction
    from reservations.models import Reservation

    forward, reverse = find_booking_source_drift()
    forward_count = forward.count()
    reverse_rows = list(
        reverse.only(
            "id", "booking_source", "gclid", "fbclid",
            "utm_source", "utm_medium", "travel_agent",
        )
    )
    reverse_count = len(reverse_rows)

    if apply and (forward_count or reverse_count):
        with transaction.atomic():
            forward.update(booking_source="travel_agent")
            for r in reverse_rows:
                Reservation.objects.filter(pk=r.id).update(
                    booking_source=derive_booking_source(r, request=None)
                )

    return {
        "forward": forward_count,
        "reverse": reverse_count,
        "total": forward_count + reverse_count,
        "applied": bool(apply),
    }


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
