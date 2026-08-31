"""
Admin view of guest-communication rates across every in-house chauffeur.

Deliberately NOT a dispatcher page. Dispatchers run the day; judging who is and
isn't texting their guests is an owner's conversation, so this sits behind
is_superuser like the chauffeur KPI page does (dispatching/views.py:20597-20601).

It is also barred from the chauffeur load/KPI pages themselves: SOP-003 keeps
those to volume only ("Volume is not quality" — SOPS/chauffeur-load-metrics.md:186).
This is a quality metric, so it lives on its own page and on the driver profile.

Read every rate as "opened from the driver app" — see drivers/comms_metrics.py.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from . import comms_metrics
from .client_messages import KINDS, KIND_CHOICES
from .models import Driver

#: Weakest first is the actionable order — the page exists to surface who needs
#: a word, not to congratulate the top of the list.
SORTS = {"weakest": "Needs a word", "best": "Strongest", "name": "Name", "trips": "Most trips"}
SORT_DEFAULT = "weakest"


@login_required(login_url="login")
def comms_kpis(request):
    if not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")

    window, days = comms_metrics.resolve_window(request.GET.get("window"))
    start, end = comms_metrics.window_bounds(days)

    sort = request.GET.get("sort") or SORT_DEFAULT
    if sort not in SORTS:
        sort = SORT_DEFAULT

    drivers = list(
        Driver.objects
        .select_related("profile")
        .filter(driver_type="inhouse")
        .order_by("profile__first_name", "profile__last_name")
    )
    stats = comms_metrics.comms_stats_bulk([d.id for d in drivers], start, end)

    rows = []
    for driver in drivers:
        row_stats = stats.get(driver.id) or {}
        trips = max(
            ((row_stats.get(k) or {}).get("eligible", 0) for k in KINDS), default=0
        )
        overall = comms_metrics.overall_pct(row_stats)
        rows.append(
            {
                "driver": driver,
                "trips": trips,
                "cells": comms_metrics.as_tiles(row_stats),
                "overall": overall,
                "accent": comms_metrics.accent_for(overall),
            }
        )

    # Chauffeurs with no completed trips in the window have no rate to compare,
    # so they sit at the bottom rather than polluting either end of the ranking.
    def sort_key(row):
        has_data = row["trips"] > 0
        overall = row["overall"] if row["overall"] is not None else 0
        name = (row["driver"].profile.first_name or "").lower() if row["driver"].profile else ""
        if sort == "name":
            return (0, name)
        if sort == "trips":
            return (not has_data, -row["trips"], name)
        if sort == "best":
            return (not has_data, -overall, name)
        return (not has_data, overall, name)

    rows.sort(key=sort_key)

    rated = [r for r in rows if r["trips"] > 0]
    fleet_sent = sum(c["sent"] for r in rated for c in r["cells"])
    fleet_eligible = sum(c["eligible"] for r in rated for c in r["cells"])
    fleet_pct = comms_metrics._pct(fleet_sent, fleet_eligible)

    return render(
        request,
        "drivers/comms_kpis.html",
        {
            "rows": rows,
            "rated_count": len(rated),
            "kind_labels": [label for _, label in KIND_CHOICES],
            "window": window,
            "windows": comms_metrics.WINDOW_LABELS,
            "sort": sort,
            "sorts": SORTS,
            "start": start,
            "end": end,
            "fleet_pct": fleet_pct,
            "fleet_accent": comms_metrics.accent_for(fleet_pct),
            "fleet_sent": fleet_sent,
            "fleet_eligible": fleet_eligible,
            "tracking_start": comms_metrics.tracking_start(),
            # Rates can be legitimately unknowable (launch day). Say so, and show
            # raw logged activity so a working system is distinguishable from a
            # broken one before the first rate window closes.
            "window_empty": comms_metrics.window_is_empty(start, end),
            "activity": comms_metrics.recent_activity(days=7),
        },
    )
