from django.contrib import admin
from django.utils import timezone
from .models import (
    OperationalTask,
    CommunicationAttempt,
    StaffActivity,
    EmailLog,
    TimeClockShift,
    TimeClockBreak,
    TimeClockRequest,
    StaffOnCall,
    ShiftSettings,
    ShiftChecklist,
    ShiftChecklistRow,
    ShiftException,
)


class CommunicationAttemptInline(admin.TabularInline):
    model = CommunicationAttempt
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(OperationalTask)
class OperationalTaskAdmin(admin.ModelAdmin):
    show_full_result_count = False
    list_display = (
        "id",
        "task_type",
        "priority",
        "status",
        "title",
        "assigned_to",
        "due_at",
        "attempts",
        "created_at",
    )
    list_filter = ("task_type", "status", "priority", "assigned_to")
    search_fields = ("title", "description")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("reservation", "leg", "lead", "assigned_to", "created_by", "resolved_by", "blocked_by")
    inlines = [CommunicationAttemptInline]
    date_hierarchy = "created_at"


@admin.register(CommunicationAttempt)
class CommunicationAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "channel", "outcome", "staff_user", "created_at")
    list_filter = ("channel", "outcome")
    readonly_fields = ("created_at",)
    raw_id_fields = ("task", "staff_user")


@admin.register(StaffActivity)
class StaffActivityAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "action_type", "path", "created_at")
    list_filter = ("action_type", "user")
    readonly_fields = ("created_at",)
    raw_id_fields = ("user", "task")
    date_hierarchy = "created_at"


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ("id", "email_type", "sent_by", "recipient_email", "reservation", "success", "sent_at")
    list_filter = ("email_type", "success", "sent_by")
    readonly_fields = ("sent_at",)
    raw_id_fields = ("sent_by", "reservation")
    date_hierarchy = "sent_at"


class TimeClockBreakInline(admin.TabularInline):
    model = TimeClockBreak
    extra = 0
    fields = ("break_start_at", "break_end_at", "auto_closed", "minutes")
    readonly_fields = ("minutes", "created_at")


@admin.register(TimeClockShift)
class TimeClockShiftAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "clock_in_at", "clock_out_at",
        "worked_minutes", "break_minutes", "is_open", "auto_closed",
        "approval_status",
    )
    list_filter = (
        "auto_closed",
        "approval_status",
        ("clock_out_at", admin.EmptyFieldListFilter),  # open vs closed
        "user",
    )
    readonly_fields = (
        "created_at", "updated_at", "edited_by", "edited_at",
        "gross_minutes", "break_minutes", "worked_minutes",
    )
    raw_id_fields = ("user",)
    date_hierarchy = "clock_in_at"
    inlines = [TimeClockBreakInline]

    def save_model(self, request, obj, form, change):
        """Stamp who corrected the times when an admin edits an existing shift."""
        if change:
            obj.edited_by = request.user
            obj.edited_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(TimeClockBreak)
class TimeClockBreakAdmin(admin.ModelAdmin):
    list_display = ("id", "shift", "break_start_at", "break_end_at", "minutes", "auto_closed")
    list_filter = ("auto_closed",)
    raw_id_fields = ("shift",)
    date_hierarchy = "break_start_at"


@admin.register(TimeClockRequest)
class TimeClockRequestAdmin(admin.ModelAdmin):
    """A staffer's ask to clock in outside their schedule. No time counts until
    it's approved AND they punch — decisions belong on the Manage Time Clock
    page; this is for inspection."""
    list_display = ("id", "user", "requested_at", "status", "decided_by", "decided_at", "shift")
    list_filter = ("status",)
    raw_id_fields = ("user", "decided_by", "shift")
    date_hierarchy = "requested_at"


@admin.register(StaffOnCall)
class StaffOnCallAdmin(admin.ModelAdmin):
    """Mark a dispatcher on-call for a date (default 12 AM–6 AM). Additive to their
    regular schedule; feeds the staffing board's overnight coverage."""
    list_display = ("date", "user", "start_time", "end_time", "note")
    list_filter = ("date", "user")
    search_fields = ("user__first_name", "user__last_name", "user__username", "note")
    autocomplete_fields = ("user", "created_by")
    date_hierarchy = "date"
    ordering = ("-date",)

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# ── Dispatch Shift System ─────────────────────────────────────────────
# Registered so an admin can inspect and repair checklist history directly.
# The floor never works here — they use the Open / Close pages.


class ShiftChecklistRowInline(admin.TabularInline):
    model = ShiftChecklistRow
    extra = 0
    fields = ("key", "kind", "state", "last_count", "confirmed_by", "confirmed_at")
    readonly_fields = ("confirmed_at",)
    ordering = ("position", "id")


class ShiftExceptionInline(admin.TabularInline):
    model = ShiftException
    extra = 0
    fields = ("what", "owner", "next_action", "resolved_at", "carried_from")
    raw_id_fields = ("owner", "task", "leg", "keoi", "carried_from")


@admin.register(ShiftChecklist)
class ShiftChecklistAdmin(admin.ModelAdmin):
    list_display = (
        "date", "kind", "opened_by", "board_safe_at", "completed_by",
        "completed_at", "reopen_count",
    )
    list_filter = ("kind", ("completed_at", admin.EmptyFieldListFilter), "date")
    date_hierarchy = "date"
    raw_id_fields = ("opened_by", "completed_by", "reopened_by")
    readonly_fields = ("created_at", "updated_at", "counts_snapshot", "targets")
    inlines = [ShiftChecklistRowInline, ShiftExceptionInline]


@admin.register(ShiftException)
class ShiftExceptionAdmin(admin.ModelAdmin):
    list_display = (
        "what_short", "owner", "next_action", "checklist", "resolved_at",
        "acknowledged_at",
    )
    list_filter = (("resolved_at", admin.EmptyFieldListFilter), "owner")
    search_fields = ("what", "next_action")
    raw_id_fields = (
        "checklist", "row", "owner", "task", "leg", "keoi", "carried_from",
        "created_by", "resolved_by", "acknowledged_by",
    )

    @admin.display(description="Outstanding")
    def what_short(self, obj):
        return obj.what[:60]


@admin.register(ShiftSettings)
class ShiftSettingsAdmin(admin.ModelAdmin):
    """Singleton — the gate times and which rows appear on which shift."""

    list_display = ("__str__", "board_safe_minutes", "board_safe_target",
                    "open_complete_minutes", "open_complete_target", "close_target")

    def has_add_permission(self, request):
        return not ShiftSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
