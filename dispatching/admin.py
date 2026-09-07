"""Admin for the two day-of ledgers — read-only, on purpose.

These are the ONLY dispatching models registered here, and the app was empty
before them. That is not an oversight being corrected wholesale: scheduler
settings have their own tuned UI (Planner -> Tuning), and DayPlan is a job
ledger nobody reads by hand. These two are different — they exist to be looked
at, they are the evidence behind a published accuracy number, and until now the
only way to see them was to ask someone with a database client.

EVERYTHING HERE IS READ-ONLY, including for a superuser. A ledger you can edit
is not evidence, and the whole argument for these tables is that the accuracy
number stays honest without anyone curating it. Deleting a row would delete the
denominator behind a percentage this project publishes.

For the numbers rather than the rows, use `manage.py advisor_scorecard`, which
knows not to quote a percentage it cannot stand behind.
"""
import logging

from django.contrib import admin

from dispatching.models import AdvisorEvent, DispatchEtaSample

logger = logging.getLogger(__name__)


class _ReadOnly(admin.ModelAdmin):
    """Look, filter, search. Never add, change or delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdvisorEvent)
class AdvisorEventAdmin(_ReadOnly):
    list_display = ("service_date", "card_id", "episode", "kind", "severity",
                    "impact_leg_id", "sightings", "outcome_quality",
                    "outcome_late_min", "was_right")
    list_filter = ("service_date", "kind", "severity", "basis",
                   "outcome_quality", "source", "had_plans")
    search_fields = ("card_id", "headline")
    date_hierarchy = "service_date"
    ordering = ("-service_date", "-first_seen_at")

    def changelist_view(self, request, extra_context=None):
        """Put the verdict above the rows.

        The question is almost always "is this any good"; the rows are what you
        drill into afterwards. Numbers come from advisor_events.scorecard(), the
        same call `manage.py advisor_scorecard` makes, so a browser and a
        terminal cannot quietly disagree. Never breaks the page: a failure here
        costs the summary, not the ledger."""
        from dispatching import advisor_events

        ctx = dict(extra_context or {})
        try:
            ctx["sc"] = advisor_events.scorecard(days=14)
            ctx["sc"]["late_bar"] = advisor_events.LATE_BAR_MIN
            ctx["eta"] = advisor_events.eta_summary(days=14)
        except Exception:
            logger.exception("advisor scorecard failed")
            ctx.setdefault("sc", {"rows": [], "totals": {}})
            ctx.setdefault("eta", {"readings": 0})
        return super().changelist_view(request, extra_context=ctx)

    @admin.display(description="Right?", boolean=True)
    def was_right(self, obj):
        """Did the trip this warning named really run more than 15 minutes
        late? None — a dash — when it could not be graded, which is a third
        answer and must not read as 'no'."""
        if obj.outcome_quality != "ok" or obj.outcome_late_min is None:
            return None
        return obj.outcome_late_min > 15


@admin.register(DispatchEtaSample)
class DispatchEtaSampleAdmin(_ReadOnly):
    list_display = ("sampled_at", "leg_id_ref", "eta_target", "eta_minutes",
                    "slack_minutes", "risk_status", "is_moving",
                    "stationary_minutes", "eta_carried")
    list_filter = ("eta_target", "risk_status", "eta_carried", "is_moving")
    search_fields = ("leg_id_ref", "vehicle_label", "origin_target")
    date_hierarchy = "sampled_at"
    ordering = ("-sampled_at",)
    #: This table takes ~3,400 rows a day. Counting them for the paginator on
    #: every page load is a full scan once it is large, so show an estimate and
    #: skip the "jump to last page" link that would force the count anyway.
    show_full_result_count = False
