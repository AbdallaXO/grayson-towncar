from django.contrib import admin
from django.utils import timezone
from .models import (
    OperationalTask,
    CommunicationAttempt,
    StaffActivity,
    EmailLog,
    TimeClockShift,
    TimeClockBreak,
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
    )
    list_filter = (
        "auto_closed",
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
