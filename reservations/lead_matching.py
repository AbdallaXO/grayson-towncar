"""
Bulk Lead → Reservation matching for the auto-conversion admin action / command.

The booking side (Customer/Reservation) shares only email and phone with a lead —
and phone is stored as last-10-digits, not E.164 — so matching is by email then
phone, exactly as the original per-lead admin action did. The point of THIS module
is scale, not smarter matching: preload reservations once and ``bulk_update``,
instead of 2 queries + a signal-driven GHL thread PER lead (which spawned ~3000
threads and timed out the 60s worker at 3k leads).

``bulk_update`` intentionally skips the per-Lead post_save signals (that is exactly
what avoids the thread storm); callers push the GHL status separately, batched.
This module is pure (no GHL / network / threads) so it is fast and unit-testable.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.utils import timezone

# Lead statuses still "open" (eligible to be auto-converted).
ACTIVE_STATUSES = ["new", "contacted", "interested", "future_contact"]


def norm_phone(raw) -> str:
    """Last 10 digits — same rule as Lead.normalize_phone, usable on the raw
    Customer.phone_number (which has no normalized column)."""
    if not raw:
        return ""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def norm_email(raw) -> str:
    return raw.strip().lower() if raw else ""


@dataclass(frozen=True)
class ResRef:
    """The minimal reservation projection the matcher needs."""
    id: int
    email: str        # normalized (lowercased)
    phone: str        # normalized (last 10)
    created_at: object  # for the deterministic "most recent" tiebreak


def _recency_key(ref):
    """Sort key for 'newest booking wins'. The leading bool keeps rows with a real
    created_at ahead of any with None, so None never gets compared to a datetime
    (which would TypeError)."""
    return (ref.created_at is not None, ref.created_at, ref.id)


class ReservationIndex:
    """In-memory email/phone indexes over reservations for O(1) lookup."""

    def __init__(self):
        self.by_email: dict[str, list[ResRef]] = defaultdict(list)
        self.by_phone: dict[str, list[ResRef]] = defaultdict(list)

    @classmethod
    def build(cls, rows) -> "ReservationIndex":
        """
        rows: iterable of dicts with keys id, customer__email,
        customer__phone_number, created_at (i.e. Reservation.objects.values(...)).
        """
        idx = cls()
        for r in rows:
            ref = ResRef(
                id=r["id"],
                email=norm_email(r.get("customer__email")),
                phone=norm_phone(r.get("customer__phone_number")),
                created_at=r.get("created_at"),
            )
            if ref.email:
                idx.by_email[ref.email].append(ref)
            if ref.phone:
                idx.by_phone[ref.phone].append(ref)
        return idx


def match_lead(lead, index: ReservationIndex):
    """
    Best reservation id for a lead — email first, then phone (newest booking wins),
    mirroring the original matching. Returns None if no reservation matches.
    `lead` may be a Lead instance or any object exposing .email and .normalized_phone.
    """
    email = norm_email(getattr(lead, "email", ""))
    if email and index.by_email.get(email):
        return max(index.by_email[email], key=_recency_key).id
    phone = getattr(lead, "normalized_phone", "") or ""
    if phone and index.by_phone.get(phone):
        return max(index.by_phone[phone], key=_recency_key).id
    return None


@dataclass
class ConversionReport:
    converted: int = 0
    no_match: int = 0
    already_converted: int = 0
    converted_lead_ids: list = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        prefix = "[DRY RUN] " if self.dry_run else ""
        return (
            f"{prefix}converted {self.converted}, no match {self.no_match}, "
            f"already converted {self.already_converted}."
        )


def recheck_lead_conversions(lead_qs, *, dry_run=False, now=None, batch_size=500) -> ConversionReport:
    """
    Scale-safe bulk auto-conversion. For each lead in `lead_qs`, find its best
    matching reservation (email → phone) and convert+link it. One reservation scan
    + batched ``bulk_update`` — no per-lead query, no per-lead signal/thread storm.

    bulk_update skips Lead post_save signals; push GHL status separately (batched)
    on the returned converted_lead_ids if desired.
    """
    from .models import Lead, Reservation

    now = now or timezone.now()
    report = ConversionReport(dry_run=dry_run)

    index = ReservationIndex.build(
        Reservation.objects.values(
            "id", "created_at", "customer__email", "customer__phone_number",
        ).iterator()
    )

    to_convert = []  # (lead, reservation_id)
    for lead in lead_qs.iterator():
        if lead.converted or lead.status == Lead.StatusChoices.CONVERTED:
            report.already_converted += 1
            continue
        res_id = match_lead(lead, index)
        if res_id:
            to_convert.append((lead, res_id))
        else:
            report.no_match += 1

    report.converted = len(to_convert)
    report.converted_lead_ids = [l.id for l, _ in to_convert]

    if dry_run:
        return report

    stamp = now.strftime("%Y-%m-%d %H:%M")
    for lead, res_id in to_convert:
        lead.status = Lead.StatusChoices.CONVERTED
        lead.converted = True
        lead.converted_at = now
        if res_id and not lead.converted_reservation_id:
            lead.converted_reservation_id = res_id
        note = f"Auto-converted on {stamp} - matched Reservation #{res_id}"
        lead.notes = f"{lead.notes}\n\n{note}" if lead.notes else note
    if to_convert:
        Lead.objects.bulk_update(
            [l for l, _ in to_convert],
            ["status", "converted", "converted_at", "converted_reservation", "notes"],
            batch_size=batch_size,
        )

    return report
