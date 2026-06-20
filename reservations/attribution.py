"""
Source attribution helpers for Reservation booking_source.

Single source of truth for translating raw UTM/click-ID/referrer/agent state
into the canonical Reservation.booking_source channel used by KPI dashboards.
Called from:
  - reservations.views.reservation_form (public booking)
  - reservations.admin.ReservationAdmin.save_model (phone / dispatcher entry)
  - The data migration that backfills booking_source on existing rows

This module is the ONE place that knows the channel taxonomy:
  - CHANNEL_LABELS  : slug -> human label (drives every dashboard label)
  - CHANNEL_GROUPS  : top-level groupings (Search / AI / Social / ...) for the
                      Reservation Sources page cards + drill-down
  - BOOKING_SOURCE_CHOICES : the model field choices (admin dropdown / filters)

We deliberately classify GRANULARLY — Bing, ChatGPT, Gemini, Perplexity,
Copilot, Claude, etc. each get their own channel instead of collapsing into
"direct"/"other" — so the founder can see exactly where every booking came
from. New AI assistants and search engines are added by editing the maps below.

Repeat-customer status is tracked SEPARATELY via Reservation.is_repeat_booking
so a returning customer who comes back via Google Ads still attributes the
revenue to Google Ads (not "repeat").
"""
import re

# Source values that indicate Meta / Facebook / Instagram traffic.
META_SRC_TOKENS = {"facebook", "fb", "ig", "instagram", "meta"}

# utm_medium values that indicate paid traffic.
PAID_MEDIUM_TOKENS = {"cpc", "paid", "ads", "ppc", "paidsocial", "paid_social"}

# ── Channel taxonomy ──────────────────────────────────────────────────────
# slug -> display label. Anything NOT listed here that still reaches a
# dashboard is shown title-cased (so a brand-new utm_source is never hidden).
CHANNEL_LABELS = {
    "google_ads": "Google Ads",
    "google_organic": "Google Organic",
    "bing_ads": "Bing Ads",
    "bing_organic": "Bing Organic",
    "yahoo": "Yahoo Search",
    "duckduckgo": "DuckDuckGo",
    "ecosia": "Ecosia",
    "chatgpt": "ChatGPT",
    "gemini": "Gemini (Google AI)",
    "perplexity": "Perplexity",
    "copilot": "Microsoft Copilot",
    "claude": "Claude",
    "ai_other": "Other AI Assistant",
    "meta_ads": "Meta Ads",
    "meta_organic": "Meta Organic",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "twitter": "X / Twitter",
    "reddit": "Reddit",
    "linkedin": "LinkedIn",
    "pinterest": "Pinterest",
    "travel_agent": "Travel Agent",
    "referral": "Referral",
    "yelp": "Yelp",
    "trustpilot": "Trustpilot",
    "tripadvisor": "Tripadvisor",
    "direct": "Direct",
    "phone": "Phone / Dispatcher",
    "other": "Other",
}

# Top-level groups for the Reservation Sources cards.
# (group key, label, accent color, [member channel slugs])
CHANNEL_GROUPS = [
    ("search", "Search Engines", "#C9A227",
     ["google_ads", "google_organic", "bing_ads", "bing_organic",
      "yahoo", "duckduckgo", "ecosia"]),
    ("ai", "AI Assistants", "#7C4DFF",
     ["chatgpt", "gemini", "perplexity", "copilot", "claude", "ai_other"]),
    ("social", "Social", "#3A6EA5",
     ["meta_ads", "meta_organic", "tiktok", "youtube", "twitter",
      "reddit", "linkedin", "pinterest"]),
    ("travel_agents", "Travel Agents", "#2E7D52",
     ["travel_agent"]),
    ("referral", "Referral / Reviews", "#5C6BC0",
     ["referral", "yelp", "trustpilot", "tripadvisor"]),
    ("direct", "Direct / Other", "#8B8470",
     ["direct", "phone", "other"]),
]

# Model field choices — derived from the label map so they never drift.
BOOKING_SOURCE_CHOICES = list(CHANNEL_LABELS.items())


def channel_label(slug) -> str:
    """Display label for a channel slug, title-casing any unknown slug so a
    brand-new tagged source still reads cleanly (never a raw 'foo_bar')."""
    if not slug:
        return "—"
    return CHANNEL_LABELS.get(slug) or slug.replace("_", " ").title()


# Ordered (substring, channel) — first match wins. Applied to a normalized
# utm_source token and, failing that, to the first-touch referrer host (so
# `chatgpt.com`, `gemini.google.com`, `bing.com`, etc. all resolve). Markers
# beginning with "@" are search/social engines that split paid vs organic.
_SOURCE_PATTERNS = [
    ("chatgpt", "chatgpt"), ("openai", "chatgpt"),
    ("perplexity", "perplexity"),
    ("copilot", "copilot"),
    ("gemini", "gemini"), ("bard", "gemini"),
    ("claude", "claude"), ("anthropic", "claude"),
    ("duckduckgo", "duckduckgo"),
    ("ecosia", "ecosia"),
    ("yahoo", "yahoo"),
    ("tiktok", "tiktok"),
    ("youtube", "youtube"), ("youtu.be", "youtube"),
    ("linkedin", "linkedin"),
    ("pinterest", "pinterest"),
    ("reddit", "reddit"),
    ("trustpilot", "trustpilot"),
    ("tripadvisor", "tripadvisor"),
    ("yelp", "yelp"),
    ("twitter", "twitter"), ("x.com", "twitter"), ("t.co", "twitter"),
    ("instagram", "@meta"), ("facebook", "@meta"),
    ("bing", "@bing"),
    ("google", "@google"),
]


def _normalize(value):
    return (value or "").strip().lower()


def _split_engine(marker, paid):
    """Resolve an '@engine' marker into its paid/organic channel slug."""
    if marker == "@google":
        return "google_ads" if paid else "google_organic"
    if marker == "@bing":
        return "bing_ads" if paid else "bing_organic"
    if marker == "@meta":
        return "meta_ads" if paid else "meta_organic"
    return marker


def _match_patterns(text, paid):
    """First-match substring lookup of a utm_source token or referrer host."""
    t = _normalize(text)
    if not t:
        return None
    for needle, slug in _SOURCE_PATTERNS:
        if needle in t:
            return _split_engine(slug, paid) if slug.startswith("@") else slug
    return None


def _slugify_source(value):
    """Sanitize an unrecognized but explicit utm_source into a stable channel
    slug (≤32 chars) so it surfaces as its own channel rather than vanishing
    into 'direct'. e.g. 'Some Partner' -> 'some_partner'."""
    s = _normalize(value)
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "other")[:32]


def classify_channel(src=None, medium=None, gclid=None, fbclid=None,
                     referrer_host=None) -> str:
    """
    Pure channel classification from ad/UTM/referrer signals (no agent/staff
    context — that's handled by derive_booking_source). Returns a channel slug.

    Precedence: click IDs > explicit utm_source > first-touch referrer host >
    referral medium > direct. Search engines (Google/Bing) and Meta split into
    _ads/_organic by paid medium; referrer-derived hits are always organic.
    """
    medium = _normalize(medium)
    src = _normalize(src)
    paid = medium in PAID_MEDIUM_TOKENS

    # 1. Click IDs are the strongest paid signals.
    if gclid:
        return "google_ads"
    if fbclid:
        return "meta_ads" if paid else "meta_organic"

    # 2. Explicit utm_source tag.
    if src:
        if src in META_SRC_TOKENS:
            return "meta_ads" if paid else "meta_organic"
        hit = _match_patterns(src, paid)
        if hit:
            return hit
        if medium == "referral" or src == "referral":
            return "referral"
        # Tagged but unrecognized — preserve it as its own channel so nothing
        # is silently bucketed into "direct".
        return _slugify_source(src)

    # 3. No utm_source — fall back to the first-touch external referrer host.
    #    These are organic (a paid click would carry a click ID / utm tag).
    if referrer_host:
        hit = _match_patterns(referrer_host, paid=False)
        if hit:
            return hit
        return "referral"  # external site we don't specifically recognize

    # 4. Explicit referral medium with no source/referrer.
    if medium == "referral":
        return "referral"

    # 5. Default — no UTM, no referrer, no agent, no staff context.
    return "direct"


def derive_booking_source(reservation, request=None) -> str:
    """
    Return the canonical booking_source channel for a Reservation.

    Order of precedence (most specific wins):
      1. Travel-agent attribution             -> "travel_agent"
      2. Staff/dispatcher creating from admin  -> "phone"
      3. Otherwise classify_channel() on the click IDs / utm_source /
         referrer_host (Google/Bing/AI assistants/Meta/social/referral/direct).

    The function is intentionally pure: it does not save the reservation.
    Callers are responsible for assigning the result to
    reservation.booking_source and saving.
    """
    # 1. Travel agent FK is the strongest signal and always wins -- including
    #    when the row is entered by staff in admin. This keeps booking_source
    #    consistent with find_booking_source_drift(), which treats any
    #    agent-linked row whose source != 'travel_agent' as drift to repair.
    if getattr(reservation, "travel_agent_id", None):
        return "travel_agent"

    # 2. Phone / dispatcher: any reservation created by a logged-in staff user
    if request is not None:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False):
            return "phone"

    # 3. Acquisition channel from the captured signals.
    return classify_channel(
        src=getattr(reservation, "utm_source", None),
        medium=getattr(reservation, "utm_medium", None),
        gclid=getattr(reservation, "gclid", None),
        fbclid=getattr(reservation, "fbclid", None),
        referrer_host=getattr(reservation, "referrer_host", None),
    )


def resolve_agent_by_customer_email(reservation):
    """
    Return the registered travel agent whose account email matches the
    reservation's customer (booking-contact) email, or None.

    Travel agents routinely book for their clients using their OWN email as the
    booking contact, so a customer email equal to an active agent's login email
    means the trip belongs in that agent's portal. This is the lookup behind the
    auto-attach in Reservation.save(): set the FK once, at creation, when unset.

    Pure lookup -- never mutates or saves the reservation. Inactive agents are
    skipped (a deactivated agent must be attached manually). Matches
    case-insensitively; .first() is deterministic if two accounts share an email.
    """
    if not getattr(reservation, "customer_id", None):
        return None
    email = (getattr(reservation.customer, "email", "") or "").strip()
    if not email:
        return None
    from users.models import TravelAgent
    return (
        TravelAgent.objects.filter(is_active=True, user__email__iexact=email)
        .select_related("user")
        .first()
    )


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


def rederive_all_booking_sources(apply=False, batch_size=1000) -> dict:
    """
    Re-derive booking_source for EVERY existing reservation from the current
    attribution logic, so older bookings pick up channels added after they were
    created (Bing, ChatGPT, Gemini, ...). Powers both the
    ``rederive_booking_source`` management command and the in-app "Reclassify
    sources" button, so they behave identically.

    Rows tagged 'phone' are LEFT ALONE — a dispatcher booking carries no UTM, so
    re-deriving it (request=None) would wrongly collapse it to 'direct'. Phone is
    only ever set with staff request context, which we don't have here.

    Pure-count when apply=False (preview); writes in one transaction when
    apply=True. Idempotent. Returns
    {"changed": int, "transitions": [(old, new, count), ...], "applied": bool}.
    """
    from collections import Counter
    from django.db import transaction
    from reservations.models import Reservation

    rows = (
        Reservation.objects.exclude(booking_source="phone")
        .only(
            "id", "booking_source", "gclid", "fbclid",
            "utm_source", "utm_medium", "referrer_host", "travel_agent",
        )
        .iterator(chunk_size=2000)
    )
    changed = []
    transitions = Counter()
    for r in rows:
        new_source = derive_booking_source(r, request=None)
        if new_source != r.booking_source:
            transitions[(r.booking_source or "(blank)", new_source)] += 1
            r.booking_source = new_source
            changed.append(r)

    if apply and changed:
        with transaction.atomic():
            Reservation.objects.bulk_update(
                changed, ["booking_source"], batch_size=batch_size
            )

    return {
        "changed": len(changed),
        "transitions": [(old, new, n) for (old, new), n in transitions.most_common()],
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
