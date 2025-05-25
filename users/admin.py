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
    Agency,
    AgencyCommissionPayout,
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
        "agency_handles_payment",
        "agency_name",
        "phone",
        "commission_rate",
        "unpaid_commission",
        "pending_commission",
        "total_paid",
        "total_reservations",
        "is_active",
    ]
    list_filter = ["is_active", "agency_handles_payment", "agency", "created_at"]
    search_fields = [
        "user__username",
        "agent_name",
        "agency_name",
        "user__email",
        "agency__name",
    ]

    fieldsets = (
        (
            "Agent Information",
            {
                "fields": (
                    "user",
                    "agent_name",
                    "agency",
                    "agency_name",
                    "phone",
                    "is_active",
                )
            },
        ),
        (
            "Payment Information",
            {
                "fields": (
                    "payment_method",
                    "payment_info",
                    "commission_rate",
                    "agency_handles_payment",
                )
            },
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
    list_editable = ["agency", "agency_handles_payment"]

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
        value = obj.total_paid_commission
        try:
            value = float(value)
            value_str = f"${value:,.2f}"
            return format_html("{}", value_str)
        except Exception:
            return value

    total_paid.short_description = "Paid"

    def unpaid_commission(self, obj):
        value = obj.unpaid_commissions
        try:
            value = float(value)
            value_str = f"${value:,.2f}"
            if value > 0:
                return format_html(
                    '<span style="color: red; font-weight: bold;">{}</span>', value_str
                )
            return format_html('<span style="color: green;">{}</span>', value_str)
        except Exception:
            return value

    unpaid_commission.short_description = "Unpaid"

    def pending_commission(self, obj):
        value = obj.pending_commissions
        try:
            value = float(value)
            value_str = f"${value:,.2f}"
            if value > 0:
                return format_html(
                    '<span style="color: blue; font-weight: bold;">{}</span>', value_str
                )
            return format_html('<span style="color: gray;">{}</span>', value_str)
        except Exception:
            return value

    pending_commission.short_description = "Pending"

    actions = [
        "preview_commission_payments",
        "process_commissions",
        "process_dual_commissions",
    ]

    def preview_commission_payments(self, request, queryset):
        """Show a preview of commission payments without actually processing them."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from django.contrib import messages
        from reservations.models import Reservation

        preview_data = []
        total_amount = 0

        for agent in queryset:
            # Calculate unpaid commissions using agent's commission rate
            unpaid_reservations = Reservation.objects.filter(
                travel_agent=agent, commission_paid=False, status="completed"
            ).annotate(
                calculated_commission=ExpressionWrapper(
                    F("total_price") * (agent.commission_rate / 100),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )

            if unpaid_reservations.exists():
                # Calculate total using the annotated commission
                commission_total = sum(
                    r.calculated_commission for r in unpaid_reservations
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
                        "agency_handles": agent.agency_handles_payment and agent.agency,
                    }
                )

                total_amount += commission_total

        if preview_data:
            # Create a message with the preview data
            message = "Commission Payment Preview:<br><br>"
            message += "<table style='border-collapse: collapse; width: 100%;'>"
            message += "<tr style='background-color: #f2f2f2;'>"
            message += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Agent</th>"
            message += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Reservations</th>"
            message += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Period</th>"
            message += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Amount</th>"
            message += "<th style='padding: 8px; text-align: left; border: 1px solid #ddd;'>Payment To</th></tr>"

            for item in preview_data:
                payment_to = "Agency" if item["agency_handles"] else "Agent"
                message += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>{item['agent']}</td>"
                message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{item['count']}</td>"
                message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{item['period']}</td>"
                message += f"<td style='padding: 8px; text-align: right; border: 1px solid #ddd;'>${item['amount']:,.2f}</td>"
                message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{payment_to}</td></tr>"

            message += f"<tr style='background-color: #f2f2f2;'><td colspan='3' style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>Total:</strong></td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd; text-align: right;'><strong>${total_amount:,.2f}</strong></td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'></td></tr>"
            message += "</table><br>"
            message += "To process these payments, select the agents again and use the 'Process unpaid commissions' or 'Process dual payouts' action."

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request, "No unpaid commissions found for the selected agents."
            )

    preview_commission_payments.short_description = "Preview commission payments"

    def process_commissions(self, request, queryset):
        """Process commissions for agents (standard single payout)"""
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0

        for agent in queryset:
            try:
                logger.info(f"Processing commissions for {agent}")
                payout, amount, _ = agent.process_commission_payment(
                    create_agency_payout=False
                )
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

    process_commissions.short_description = "Process unpaid commissions (Agent only)"

    def process_dual_commissions(self, request, queryset):
        """Process commissions with dual payouts (agent + agency)"""
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0
        agency_payouts_created = 0
        warnings = []

        for agent in queryset:
            try:
                if not agent.agency:
                    warnings.append(
                        f"{agent} has no agency assigned. Processing agent payout only."
                    )
                    create_agency = False
                elif not agent.agency_handles_payment:
                    warnings.append(
                        f"{agent} is not set for agency payment handling. Processing agent payout only."
                    )
                    create_agency = False
                else:
                    create_agency = True

                logger.info(
                    f"Processing commissions for {agent} (dual: {create_agency})"
                )
                payout, amount, agency_payout = agent.process_commission_payment(
                    create_agency_payout=create_agency
                )

                if payout:
                    processed_count += 1
                    total_amount += amount
                    if agency_payout:
                        agency_payouts_created += 1
                    logger.info(f"Created agent payout #{payout.id} for ${amount}")
                    if agency_payout:
                        logger.info(
                            f"Created agency payout #{agency_payout.id} for ${amount}"
                        )
                else:
                    logger.info(f"No unpaid commissions found for {agent}")
            except Exception as e:
                logger.error(f"Error processing dual commissions for {agent}: {e}")
                messages.error(request, f"Error processing {agent}: {e}")

        # Show warnings if any
        for warning in warnings:
            messages.warning(request, warning)

        if processed_count:
            message = f"Processed commissions for {processed_count} agents. Total: ${total_amount:,.2f}."
            if agency_payouts_created:
                message += f" Created {agency_payouts_created} agency payouts."
            messages.success(request, message)
        else:
            messages.info(request, "No unpaid commissions found for selected agents.")

    process_dual_commissions.short_description = "Process dual payouts (Agent + Agency)"


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
        "agency_link",
        "total_amount",
        "reservation_count",
        "payout_period",
        "paid_at",
        "payment_status",
    ]
    list_filter = ["paid_at", "payout_period_start", "agency"]
    search_fields = [
        "agent__agent_name",
        "agent__user__username",
        "agent__agency_name",
        "agency__name",
    ]
    inlines = [ReservationInline]
    readonly_fields = ["paid_at", "reservation_details"]

    fieldsets = (
        (
            "Payout Information",
            {
                "fields": (
                    "agent",
                    "agency",
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

    def agency_link(self, obj):
        if obj.agency:
            url = reverse("admin:users_agency_change", args=[obj.agency.id])
            return format_html('<a href="{}">{}</a>', url, obj.agency.name)
        return "-"

    agency_link.short_description = "Agency"

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

                # The signal handler will take care of updating agent totals
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
        return (
            super()
            .get_queryset(request)
            .prefetch_related("reservations", "agent", "agency")
        )


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "agent_count",
        "agents_with_agency_payment",
        "total_unpaid",
        "total_pending",
        "total_paid",
        "is_active",
    ]
    list_filter = ["is_active", "created_at"]
    search_fields = ["name"]
    filter_horizontal = ["heads"]

    fieldsets = (
        (
            "Agency Information",
            {"fields": ("name", "address", "phone", "website", "logo", "is_active")},
        ),
        (
            "Management",
            {
                "fields": ("heads",),
                "description": "Select agency heads who can manage this agency and its agents.",
            },
        ),
        (
            "Payment Information",
            {"fields": ("payment_method", "payment_info", "total_paid_commission")},
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    readonly_fields = ["created_at", "updated_at", "total_paid_commission"]

    def agent_count(self, obj):
        count = obj.agents.count()
        url = (
            reverse("admin:users_travelagent_changelist")
            + f"?agency__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{}</a>', url, count)

    agent_count.short_description = "Total Agents"

    def agents_with_agency_payment(self, obj):
        count = obj.agents.filter(agency_handles_payment=True).count()
        if count > 0:
            url = (
                reverse("admin:users_travelagent_changelist")
                + f"?agency__id__exact={obj.id}&agency_handles_payment__exact=1"
            )
            return format_html(
                '<a href="{}" style="color: green; font-weight: bold;">{}</a>',
                url,
                count,
            )
        return "0"

    agents_with_agency_payment.short_description = "Agency Payment"

    def total_unpaid(self, obj):
        amount = obj.get_total_unpaid_commissions()
        try:
            amount = float(amount)
            amount_str = f"${amount:,.2f}"
            if amount > 0:
                return format_html(
                    '<span style="color: red; font-weight: bold;">{}</span>', amount_str
                )
            return format_html('<span style="color: green;">{}</span>', amount_str)
        except Exception:
            return amount

    total_unpaid.short_description = "Unpaid"

    def total_pending(self, obj):
        amount = obj.get_total_pending_commissions()
        try:
            amount = float(amount)
            amount_str = f"${amount:,.2f}"
            if amount > 0:
                return format_html('<span style="color: blue;">{}</span>', amount_str)
            return format_html('<span style="color: gray;">{}</span>', amount_str)
        except Exception:
            return amount

    total_pending.short_description = "Pending"

    def total_paid(self, obj):
        # Sync the paid commission before displaying
        obj.sync_paid_commission()
        value = obj.total_paid_commission
        try:
            value = float(value)
            value_str = f"${value:,.2f}"
            return format_html("{}", value_str)
        except Exception:
            return value

    total_paid.short_description = "Total Paid"

    actions = [
        "process_agency_commissions",
        "preview_agency_commissions",
        "update_commission_stats",
        "view_agency_agents",
    ]

    def preview_agency_commissions(self, request, queryset):
        """Preview commission payments for agencies"""
        from django.contrib import messages

        preview_data = []
        grand_total = 0

        for agency in queryset:
            agents_with_unpaid = agency.agents.filter(
                unpaid_commissions__gt=0, agency_handles_payment=True
            )

            if agents_with_unpaid.exists():
                agency_total = sum(
                    agent.unpaid_commissions for agent in agents_with_unpaid
                )

                # Get agent details
                agent_details = []
                for agent in agents_with_unpaid:
                    agent_details.append(
                        {"name": str(agent), "unpaid": agent.unpaid_commissions}
                    )

                preview_data.append(
                    {
                        "agency": agency,
                        "agent_count": agents_with_unpaid.count(),
                        "total": agency_total,
                        "agents": agent_details,
                    }
                )
                grand_total += agency_total

        if preview_data:
            message = "Agency Commission Payment Preview:<br><br>"

            for item in preview_data:
                message += f"<h4>{item['agency']} - {item['agent_count']} agents - ${item['total']:,.2f}</h4>"
                message += (
                    "<table style='border-collapse: collapse; margin-bottom: 20px;'>"
                )
                message += (
                    "<tr><th style='padding: 8px; border: 1px solid #ddd;'>Agent</th>"
                )
                message += "<th style='padding: 8px; border: 1px solid #ddd;'>Unpaid Commission</th></tr>"

                for agent in item["agents"]:
                    message += f"<tr><td style='padding: 8px; border: 1px solid #ddd;'>{agent['name']}</td>"
                    message += f"<td style='padding: 8px; border: 1px solid #ddd;'>${agent['unpaid']:,.2f}</td></tr>"

                message += "</table>"

            message += f"<p><strong>Grand Total: ${grand_total:,.2f}</strong></p>"

            self.message_user(request, mark_safe(message))
        else:
            self.message_user(
                request,
                "No unpaid commissions found for agents with agency payment handling.",
            )

    preview_agency_commissions.short_description = "Preview agency commission payments"

    def process_agency_commissions(self, request, queryset):
        """Process commission payments for agencies"""
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        processed_count = 0
        total_amount = 0
        agents_processed = 0

        for agency in queryset:
            try:
                logger.info(f"Processing agency commissions for {agency}")
                payout, amount = agency.process_agency_commission_payment()
                if payout:
                    processed_count += 1
                    total_amount += amount
                    agents_processed += payout.agent_payouts.count()
                    logger.info(f"Created agency payout #{payout.id} for ${amount}")
            except Exception as e:
                logger.error(f"Error processing {agency}: {e}")
                messages.error(request, f"Error processing {agency}: {e}")

        if processed_count:
            total_str = f"${total_amount:,.2f}"
            messages.success(
                request,
                f"Processed commissions for {processed_count} agencies, "
                f"{agents_processed} agents. Total: {total_str}",
            )
        else:
            messages.info(request, "No unpaid commissions found for selected agencies.")

    process_agency_commissions.short_description = "Process agency commission payments"

    def update_commission_stats(self, request, queryset):
        """Update commission statistics for selected agencies"""
        from django.contrib import messages

        for agency in queryset:
            stats = agency.update_commission_stats()
            messages.info(
                request,
                f"{agency.name}: {stats['agents_count']} agents, "
                f"${stats['unpaid']:,.2f} unpaid, "
                f"${stats['pending']:,.2f} pending, "
                f"${stats['paid']:,.2f} paid",
            )

    update_commission_stats.short_description = "Update commission statistics"

    def view_agency_agents(self, request, queryset):
        """View and manage agents for selected agencies"""
        from django.contrib import messages

        if len(queryset) != 1:
            messages.error(
                request, "Please select exactly one agency to view its agents."
            )
            return

        agency = queryset[0]
        agents = agency.agents.all().select_related("user")

        if not agents:
            messages.info(request, f"No agents found for {agency.name}.")
            return

        # Create a detailed message with agent information
        message = f"<h3>Agents for {agency.name}</h3>"
        message += (
            "<table style='width: 100%; border-collapse: collapse; margin-top: 10px;'>"
        )
        message += "<tr style='background-color: #f0f0f0;'>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Agent</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Email</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Phone</th>"
        message += (
            "<th style='padding: 8px; border: 1px solid #ddd;'>Commission Rate</th>"
        )
        message += (
            "<th style='padding: 8px; border: 1px solid #ddd;'>Agency Payment</th>"
        )
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Status</th>"
        message += "<th style='padding: 8px; border: 1px solid #ddd;'>Actions</th>"
        message += "</tr>"

        for agent in agents:
            edit_url = reverse("admin:users_travelagent_change", args=[agent.id])
            message += "<tr>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{agent.agent_name}</td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{agent.user.email}</td>"
            message += (
                f"<td style='padding: 8px; border: 1px solid #ddd;'>{agent.phone}</td>"
            )
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>{agent.commission_rate}%</td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>"
            message += f"<span style='color: {'green' if agent.agency_handles_payment else 'gray'};'>"
            message += "✓" if agent.agency_handles_payment else "✗"
            message += "</span></td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>"
            message += f"<span style='color: {'green' if agent.is_active else 'red'};'>"
            message += "Active" if agent.is_active else "Inactive"
            message += "</span></td>"
            message += f"<td style='padding: 8px; border: 1px solid #ddd;'>"
            message += f"<a href='{edit_url}' class='button'>Edit</a>"
            message += "</td>"
            message += "</tr>"

        message += "</table>"
        message += "<p style='margin-top: 10px;'>"
        message += f"<a href='{reverse('admin:users_travelagent_add')}?agency={agency.id}' class='button'>Add New Agent</a>"
        message += "</p>"

        self.message_user(request, mark_safe(message))

    view_agency_agents.short_description = "View Agency Agents"


class AgentPayoutInline(admin.TabularInline):
    model = AgencyCommissionPayout.agent_payouts.through
    extra = 0
    readonly_fields = ["agent_payout_info"]

    def agent_payout_info(self, obj):
        payout = obj.commissionpayout
        amount_str = f"${payout.total_amount:.2f}"
        return format_html(
            '<a href="{}">#{} - {} - {}</a>',
            reverse("admin:users_commissionpayout_change", args=[payout.id]),
            payout.id,
            payout.agent,
            amount_str,
        )

    agent_payout_info.short_description = "Agent Payout"


@admin.register(AgencyCommissionPayout)
class AgencyCommissionPayoutAdmin(admin.ModelAdmin):
    list_display = [
        "payout_id",
        "agency_link",
        "total_amount",
        "agent_count",
        "payout_period",
        "paid_at",
        "payment_status",
    ]
    list_filter = ["paid_at", "payout_period_start", "agency"]
    search_fields = ["agency__name"]
    inlines = [AgentPayoutInline]
    readonly_fields = ["paid_at", "agent_payout_details"]

    fieldsets = (
        (
            "Payout Information",
            {
                "fields": (
                    "agency",
                    "total_amount",
                    "payout_period_start",
                    "payout_period_end",
                )
            },
        ),
        ("Payment Details", {"fields": ("paid_at", "notes")}),
        (
            "Agent Payouts",
            {"fields": ("agent_payout_details",), "classes": ("collapse",)},
        ),
    )

    def payout_id(self, obj):
        return f"#{obj.id}"

    payout_id.short_description = "Payout ID"

    def agency_link(self, obj):
        url = reverse("admin:users_agency_change", args=[obj.agency.id])
        return format_html('<a href="{}">{}</a>', url, obj.agency.name)

    agency_link.short_description = "Agency"

    def agent_count(self, obj):
        return obj.agent_payouts.count()

    agent_count.short_description = "Agents"

    def payout_period(self, obj):
        return format_html(
            "{} to {}",
            obj.payout_period_start.strftime("%b %d, %Y"),
            obj.payout_period_end.strftime("%b %d, %Y"),
        )

    payout_period.short_description = "Period"

    def payment_status(self, obj):
        if obj.paid_at:
            return format_html('<span style="color: green;">✓ Paid</span>')
        return format_html('<span style="color: orange;">Pending</span>')

    payment_status.short_description = "Status"

    def agent_payout_details(self, obj):
        payouts = obj.agent_payouts.all()
        if not payouts:
            return "No agent payouts"

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += '<tr style="background-color: #f0f0f0;"><th>Payout ID</th><th>Agent</th><th>Amount</th><th>Reservations</th><th>Period</th></tr>'

        for payout in payouts:
            try:
                amount = float(payout.total_amount)
            except Exception:
                amount = payout.total_amount
            html += f"<tr>"
            html += f'<td><a href="{reverse("admin:users_commissionpayout_change", args=[payout.id])}">#{payout.id}</a></td>'
            html += f"<td>{payout.agent}</td>"
            html += f"<td>${amount:,.2f}</td>"
            html += f"<td>{payout.reservations.count()}</td>"
            html += f"<td>{payout.payout_period_start.strftime('%b %d')} - {payout.payout_period_end.strftime('%b %d, %Y')}</td>"
            html += f"</tr>"

        html += "</table>"
        return mark_safe(html)

    agent_payout_details.short_description = "Agent Payout Details"

    actions = ["cancel_agency_payouts"]

    def cancel_agency_payouts(self, request, queryset):
        """
        Cancel selected agency payouts.
        """
        from django.contrib import messages
        import logging

        logger = logging.getLogger(__name__)
        cancelled_count = 0
        total_cancelled = 0

        for payout in queryset:
            total_cancelled += payout.total_amount

            logger.info(
                f"Cancelling agency payout #{payout.id} for ${payout.total_amount}"
            )

            # The pre_delete signal will handle updating agency stats
            payout.delete()
            cancelled_count += 1

        if cancelled_count:
            messages.success(
                request,
                f"Cancelled {cancelled_count} agency payout(s) totaling ${total_cancelled:.2f}.",
            )

    cancel_agency_payouts.short_description = "Cancel selected agency payouts"

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("agent_payouts", "agency")
