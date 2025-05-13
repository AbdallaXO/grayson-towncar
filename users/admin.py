from django.contrib import admin
from .models import (
    UserProfile,
    PartnerForm,
    ContactUsForm,
    NewsLetter,
    TravelAgent,
    CommissionPayout,
)
from django.db.models import Sum, Count, Q
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe

# Register your models here.
admin.site.register(UserProfile)
admin.site.register(PartnerForm)
admin.site.register(ContactUsForm)
admin.site.register(NewsLetter)


@admin.register(TravelAgent)
class TravelAgentAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "agent_name",
        "agency_name",
        "phone",
        "commission_rate",
        "total_reservations",
        "total_earned",
        "total_paid",
        "unpaid_commission",
        "is_active",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["user__username", "agent_name", "agency_name", "user__email"]
    readonly_fields = ["total_earned_commission", "total_paid_commission", "created_at"]

    fieldsets = (
        (
            "Agent Information",
            {"fields": ("user", "agent_name", "agency_name", "phone", "is_active")},
        ),
        ("Payment Information", {"fields": ("payment_info", "commission_rate")}),
        (
            "Commission Tracking",
            {
                "fields": (
                    "total_earned_commission",
                    "total_paid_commission",
                    "last_payment_date",
                ),
                "classes": ("collapse",),
            },
        ),
        ("System Information", {"fields": ("created_at",), "classes": ("collapse",)}),
    )

    def total_reservations(self, obj):
        count = obj.reservation_set.count()
        url = f"/admin/reservations/reservation/?travel_agent__id__exact={obj.id}"
        return format_html('<a href="{}">{}</a>', url, count)

    total_reservations.short_description = "Total Reservations"

    def total_earned(self, obj):
        return format_html("${}", f"{obj.total_earned_commission:,.2f}")

    total_earned.short_description = "Total Earned"

    def total_paid(self, obj):
        return format_html("${}", f"{obj.total_paid_commission:,.2f}")

    total_paid.short_description = "Total Paid"

    def unpaid_commission(self, obj):
        unpaid = (
            obj.reservation_set.filter(
                commission_paid=False, status="completed"
            ).aggregate(total=Sum("commission_amount"))["total"]
            or 0
        )

        if unpaid > 0:
            return format_html(
                '<span style="color: red; font-weight: bold;">${}</span>',
                f"{unpaid:,.2f}",
            )
        else:
            return format_html(
                '<span style="color: green;">${}</span>', f"{unpaid:,.2f}"
            )

    unpaid_commission.short_description = "Unpaid Commission"

    actions = ["process_commissions", "mark_active", "mark_inactive"]

    def process_commissions(self, request, queryset):
        from django.contrib import messages
        from django.db import transaction
        from django.utils import timezone

        processed_count = 0
        total_amount = 0

        for agent in queryset:
            unpaid_reservations = agent.reservation_set.filter(
                commission_paid=False, status="completed"
            )

            if unpaid_reservations.exists():
                with transaction.atomic():
                    commission_total = (
                        unpaid_reservations.aggregate(total=Sum("commission_amount"))[
                            "total"
                        ]
                        or 0
                    )

                    # Create payout
                    payout = CommissionPayout.objects.create(
                        agent=agent,
                        total_amount=commission_total,
                        payout_period_start=unpaid_reservations.earliest(
                            "created_at"
                        ).created_at.date(),
                        payout_period_end=unpaid_reservations.latest(
                            "created_at"
                        ).created_at.date(),
                    )

                    # Add reservations to payout
                    payout.reservations.set(unpaid_reservations)

                    # Mark reservations as paid
                    unpaid_reservations.update(
                        commission_paid=True, commission_paid_at=timezone.now()
                    )

                    # Update agent totals
                    agent.total_earned_commission += commission_total
                    agent.total_paid_commission += commission_total
                    agent.last_payment_date = timezone.now()
                    agent.save()

                    processed_count += 1
                    total_amount += commission_total

        if processed_count:
            messages.success(
                request,
                f"Processed commissions for {processed_count} agents. Total: ${total_amount:,.2f}",
            )
        else:
            messages.info(request, "No unpaid commissions found for selected agents.")

    process_commissions.short_description = "Process unpaid commissions"

    def mark_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"{updated} agents marked as active.")

    mark_active.short_description = "Mark selected agents as active"

    def mark_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"{updated} agents marked as inactive.")

    mark_inactive.short_description = "Mark selected agents as inactive"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                reservation_count=Count("reservation"),
                unpaid_amount=Sum(
                    "reservation__commission_amount",
                    filter=Q(
                        reservation__commission_paid=False,
                        reservation__status="completed",
                    ),
                ),
            )
        )


class ReservationInline(admin.TabularInline):
    model = CommissionPayout.reservations.through
    extra = 0
    readonly_fields = ["reservation_info"]

    def reservation_info(self, obj):
        reservation = obj.reservation
        return format_html(
            '<a href="/admin/reservations/reservation/{}/change/">#{}</a> - {} - ${}',
            reservation.id,
            reservation.id,
            reservation.customer,
            reservation.total_price,
        )

    reservation_info.short_description = "Reservation"


@admin.register(CommissionPayout)
class CommissionPayoutAdmin(admin.ModelAdmin):
    list_display = [
        "payout_id",
        "agent_link",
        "total_amount",
        "reservation_count",
        "payout_period",
        "paid_at",
        "payment_status",
    ]
    list_filter = ["paid_at", "payout_period_start"]
    search_fields = ["agent__agent_name", "agent__user__username", "agent__agency_name"]
    inlines = [ReservationInline]
    readonly_fields = ["paid_at", "reservation_details"]

    fieldsets = (
        (
            "Payout Information",
            {
                "fields": (
                    "agent",
                    "total_amount",
                    "payout_period_start",
                    "payout_period_end",
                )
            },
        ),
        ("Payment Details", {"fields": ("paid_at", "notes")}),
        (
            "Reservations",
            {"fields": ("reservation_details",), "classes": ("collapse",)},
        ),
    )

    def payout_id(self, obj):
        return f"#{obj.id}"

    payout_id.short_description = "Payout ID"

    def agent_link(self, obj):
        url = reverse("admin:users_travelagent_change", args=[obj.agent.id])
        return format_html('<a href="{}">{}</a>', url, obj.agent)

    agent_link.short_description = "Agent"

    def payout_period(self, obj):
        return format_html(
            "{} to {}",
            obj.payout_period_start.strftime("%b %d, %Y"),
            obj.payout_period_end.strftime("%b %d, %Y"),
        )

    payout_period.short_description = "Period"

    def reservation_count(self, obj):
        return obj.reservations.count()

    reservation_count.short_description = "Reservations"

    def payment_status(self, obj):
        if obj.paid_at:
            return format_html('<span style="color: green;">✓ Paid</span>')
        return format_html('<span style="color: orange;">Pending</span>')

    payment_status.short_description = "Status"

    def reservation_details(self, obj):
        reservations = obj.reservations.all()
        if not reservations:
            return "No reservations"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>ID</th><th>Customer</th><th>Date</th><th>Amount</th><th>Commission</th></tr>'

        for res in reservations:
            html += f"<tr>"
            html += f'<td><a href="/admin/reservations/reservation/{res.id}/change/">#{res.id}</a></td>'
            html += f"<td>{res.customer}</td>"
            html += f"<td>{res.created_at.strftime('%b %d, %Y')}</td>"
            html += f"<td>${res.total_price:,.2f}</td>"
            html += f"<td>${res.commission_amount:,.2f}</td>"
            html += f"</tr>"

        html += "</table>"
        return mark_safe(html)

    reservation_details.short_description = "Reservation Details"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("reservations", "agent")
