from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.db.models import Sum, Count, Q
from django.utils.safestring import mark_safe
from decimal import Decimal
from .models import (
    UserProfile,
    PartnerForm,
    ContactUsForm,
    NewsLetter,
    TravelAgent,
    CommissionPayout,
    Agency
)

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
        "agency",
        "agency_name",
        "phone",
        "commission_rate",
        "unpaid_commission",
        "pending_commission",
        "total_paid",
        "total_reservations",
        "is_active",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["user__username", "agent_name", "agency_name", "user__email"]

    fieldsets = (
        (
            "Agent Information",
            {"fields": ("user", "agent_name", "agency_name", "phone", "is_active")},
        ),
        (
            "Payment Information",
            {"fields": ("payment_method", "payment_info", "commission_rate")},
        ),
        (
            "Commission Tracking",
            {
                "fields": (
                    "total_paid_commission",
                    "pending_commissions",
                    "unpaid_commissions",
                    "last_payment_date",
                ),
                "description": "Commission values reflect current reservation statuses and payment history.",
            },
        ),
        ("System Information", {"fields": ("created_at",), "classes": ("collapse",)}),
    )
    readonly_fields = ["created_at"]
    list_editable=["agency"]

    def total_reservations(self, obj):
        from reservations.models import Reservation

        count = Reservation.objects.filter(travel_agent=obj).count()
        url = (
            reverse("admin:reservations_reservation_changelist")
            + f"?travel_agent__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{}</a>', url, count)

    total_reservations.short_description = "Total Reservations"

    def total_paid(self, obj):
        return format_html("${}", f"{obj.total_paid_commission:,.2f}")

    total_paid.short_description = "Paid"

    def unpaid_commission(self, obj):
        if obj.unpaid_commissions > 0:
            return format_html(
                '<span style="color: red; font-weight: bold;">${}</span>',
                f"{obj.unpaid_commissions:,.2f}",
            )
        else:
            return format_html(
                '<span style="color: green;">${}</span>',
                f"{obj.unpaid_commissions:,.2f}",
            )

    unpaid_commission.short_description = "Unpaid"

    def pending_commission(self, obj):
        if obj.pending_commissions > 0:
            return format_html(
                '<span style="color: blue; font-weight: bold;">${}</span>',
                f"{obj.pending_commissions:,.2f}",
            )
        else:
            return format_html(
                '<span style="color: gray;">${}</span>',
                f"{obj.pending_commissions:,.2f}",
            )

    pending_commission.short_description = "Pending"

    actions = ["preview_commission_payments", "process_commissions"]

    def preview_commission_payments(self, request, queryset):
        """Show a preview of commission payments without actually processing them."""
        from django.db.models import Sum
        from django.contrib import messages
        from reservations.models import Reservation

        preview_data = []
        total_amount = 0

        for agent in queryset:
            # Get unpaid completed reservations
            unpaid_reservations = Reservation.objects.filter(
                travel_agent=agent, commission_paid=False, status="completed"
            )

            if unpaid_reservations.exists():
                # Calculate total
                commission_total = (
                    unpaid_reservations.aggregate(total=Sum("commission_amount"))[
                        "total"
                    ]
                    or 0
                )

                # Count reservations
                reservation_count = unpaid_reservations.count()

                # Get date range
                start_date = unpaid_reservations.earliest(
                    "created_at"
                ).created_at.date()
                end_date = unpaid_reservations.latest("created_at").created_at.date()

                # Add to preview data
                preview_data.append(
                    {
                        "agent": agent,
                        "amount": commission_total,
                        "count": reservation_count,
                        "period": f"{start_date.strftime('%b %d, %Y')} to {end_date.strftime('%b %d, %Y')}",
                    }
                )

                total_amount += commission_total

        if preview_data:
            # Create a message with the preview data
            message = "Commission Payment Preview:<br><br>"
            message += "<table style='border-collapse: collapse; width: 100%;'>"
            message += "<tr style='background-color: #f2f2f2;'><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Agent</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Reservations</th><th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Period</th><th style='padding: 8px; text-align: right; border: 1px solid #ddd;'>Amount</th></tr>"

            for item in preview_data:
                message += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>{item['agent']}</td><td style='padding: 8px; border: 1px solid #ddd;'>{item['count']}</td><td style='padding: 8px; border: 1px solid #ddd;'>{item['period']}</td><td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>${item['amount']:,.2f}</td></tr>"

            message += f"<tr style='background-color: #f2f2f2;'><td colspan='3' style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>Total:</strong></td><td style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>${total_amount:,.2f}</strong></td></tr>"
            message += "</table><br>"
            message += "To process these payments, select the agents again and use the 'Process unpaid commissions' action."

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request, "No unpaid commissions found for the selected agents."
            )

    preview_commission_payments.short_description = "Preview commission payments"

    def process_commissions(self, request, queryset):
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for agent in queryset:
            try:
                logger.info(f"Processing commissions for {agent}")
                payout, amount = agent.process_commission_payment()
                if payout:
                    processed_count += 1
                    total_amount += amount
                    logger.info(f"Created payout #{payout.id} for ${amount}")
                else:
                    logger.info(f"No unpaid commissions found for {agent}")
            except Exception as e:
                logger.error(f"Error processing commissions for {agent}: {e}")
                messages.error(request, f"Error processing {agent}: {e}")

        if processed_count:
            messages.success(
                request,
                f"Processed commissions for {processed_count} agents. Total: ${total_amount:,.2f}",
            )
        else:
            messages.info(request, "No unpaid commissions found for selected agents.")

    process_commissions.short_description = "Process unpaid commissions"


class ReservationInline(admin.TabularInline):
    model = CommissionPayout.reservations.through
    extra = 0
    readonly_fields = ["reservation_info"]

    def reservation_info(self, obj):
        reservation = obj.reservation
        return format_html(
            '<a href="{}">{} - {} - ${:.2f}</a>',
            reverse("admin:reservations_reservation_change", args=[reservation.id]),
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
            html += f'<td><a href="{reverse("admin:reservations_reservation_change", args=[res.id])}">{res.id}</a></td>'
            html += f"<td>{res.customer}</td>"
            html += f"<td>{res.created_at.strftime('%b %d, %Y')}</td>"
            html += f"<td>${res.total_price:.2f}</td>"
            html += f"<td>${res.commission_amount:.2f}</td>"
            html += f"</tr>"

        html += "</table>"
        return mark_safe(html)

    reservation_details.short_description = "Reservation Details"

    actions = ["recalculate_payout_amounts", "cancel_payouts"]

    def recalculate_payout_amounts(self, request, queryset):
        """
        Recalculate payout amounts based on included reservations.
        """
        from django.contrib import messages
        from django.db.models import Sum
        import logging

        logger = logging.getLogger(__name__)
        updated_count = 0

        for payout in queryset:
            # Calculate the correct amount based on reservations
            reservations = payout.reservations.all()
            new_total = reservations.aggregate(total=Sum("commission_amount"))[
                "total"
            ] or Decimal("0")

            if new_total != payout.total_amount:
                # Store old amount for reporting
                old_amount = payout.total_amount

                # Update agent totals
                agent = payout.agent

                logger.info(
                    f"Recalculating payout #{payout.id} - old: ${old_amount}, new: ${new_total}"
                )
                logger.info(
                    f"Agent {agent} paid commission before update: ${agent.total_paid_commission}"
                )

                # Update agent paid commission
                agent.total_paid_commission = (
                    agent.total_paid_commission - old_amount + new_total
                )
                agent.save(update_fields=["total_paid_commission"])

                logger.info(
                    f"Agent {agent} paid commission after update: ${agent.total_paid_commission}"
                )

                # Update payout
                payout.total_amount = new_total
                payout.save()

                updated_count += 1
                messages.success(
                    request,
                    f"Updated payout #{payout.id} from ${old_amount:.2f} to ${new_total:.2f}",
                )

        if not updated_count:
            messages.info(
                request,
                "All selected payouts already have correct amounts.",
            )

    recalculate_payout_amounts.short_description = "Recalculate selected payout amounts"

    def cancel_payouts(self, request, queryset):
        """
        Cancel selected payouts and return commissions to unpaid status.
        """
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        cancelled_count = 0
        total_cancelled = 0

        for payout in queryset:
            total_cancelled += payout.total_amount

            logger.info(f"Cancelling payout #{payout.id} for ${payout.total_amount}")
            logger.info(f"Reservations in payout: {payout.reservations.count()}")

            # The pre_delete signal will handle updating agent stats and marking reservations as unpaid
            payout.delete()
            cancelled_count += 1

        if cancelled_count:
            messages.success(
                request,
                f"Cancelled {cancelled_count} payout(s) totaling ${total_cancelled:.2f}. All related reservations have been marked as unpaid.",
            )

    cancel_payouts.short_description = "Cancel selected payouts"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("reservations", "agent")
    
admin.site.register(Agency)